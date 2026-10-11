// SPDX-License-Identifier: MIT
// Optional reporting controls for a verified Claude artifact. This is not a
// network sandbox and cannot prevent server-side collection of business traffic.
import fs from 'node:fs';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import workerThreads from 'node:worker_threads';
import { roleModule, exportOverrides } from './termux-privacy-profile.mjs';
import { syncBuiltinESMExports } from 'node:module';

const filename = fileURLToPath(import.meta.url);
const stateKey = Symbol.for('claude-termux.privacy.v1');

function isTarget() {
  if (process.env.CLAUDE_TERMUX_PRIVACY_ACTIVE !== 'optional-off') return false;
  if (process.env.CLAUDE_TERMUX_PRIVACY_PRELOAD !== filename) return false;
  return fs.realpathSync(process.execPath) === fs.realpathSync(process.env.CLAUDE_TERMUX_PRIVACY_TARGET);
}

async function install() {
  if (globalThis[stateKey]) return;
  const profile = JSON.parse(fs.readFileSync(new URL('./privacy-profile.json', import.meta.url), 'utf8'));
  if (process.versions.bun !== profile.bun) throw Error('Unsupported Bun runtime');
  // Check every source before executing any application module or claiming
  // protection. Artifact integrity is also checked by the candidate installer.
  for (const [name, entry] of Object.entries(profile.modules)) {
    const actual = crypto.createHash('sha256').update(fs.readFileSync(entry.path)).digest('hex');
    if (actual !== entry.sha256) throw Error('Unsupported Claude privacy module: ' + name);
  }
  process.env.DISABLE_ERROR_REPORTING = '1';
  process.env.DISABLE_FEEDBACK_COMMAND = '1';
  process.env.CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY = '1';
  const stopContentCapture = () => {
    // The raw-body file mode is independent of the ordinary exporter toggle.
    process.env.OTEL_LOG_RAW_API_BODIES = 'false';
    for (const name of ['OTEL_LOG_USER_PROMPTS', 'OTEL_LOG_ASSISTANT_RESPONSES',
      'OTEL_LOG_TOOL_CONTENT', 'OTEL_LOG_TOOL_DETAILS', 'OTEL_LOG_MANAGED_SETTINGS']) process.env[name] = 'false';
  };
  stopContentCapture();
  const { mock } = await import('bun:test');
  if (typeof mock.module !== 'function') throw Error('Module replacement is unavailable');
  const state = { mode: 'optional-off', version: profile.claude, ready: false, counts: Object.create(null) };
  const count = key => { state.counts[key] = (state.counts[key] || 0) + 1; };
  const drop = key => () => { count(key); };
  const asyncDrop = key => async () => { count(key); };
  const yes = () => true;
  const no = () => false;
  const instrument = Object.freeze({ add() {}, record() {}, addCallback() {}, removeCallback() {} });
  const meter = Object.freeze({
    createCounter: () => instrument, createUpDownCounter: () => instrument,
    createHistogram: () => instrument, createGauge: () => instrument,
    createObservableCounter: () => instrument, createObservableUpDownCounter: () => instrument,
    createObservableGauge: () => instrument, addBatchObservableCallback() {}, removeBatchObservableCallback() {},
  });
  const logger = Object.freeze({ emit: drop('log-record') });
  class LoggerProvider {
    getLogger() { return logger; }
    async forceFlush() {}
    async shutdown() {}
  }
  class MeterProvider {
    getMeter() { return meter; }
    async forceFlush() {}
    async shutdown() {}
  }
  class Exporter {
    export(_data, callback) { count('otlp-export'); queueMicrotask(() => callback({ code: 0 })); }
    async forceFlush() {}
    async shutdown() {}
    selectAggregationTemporality() { return 1; }
  }
  const emptySender = () => ({ enqueue: drop('datadog-queue'), flush: asyncDrop('datadog-flush'),
    shutdown: async () => {}, drainForTesting: () => [] });

  // Keep shared feature-availability gates untouched. These are not equivalent
  // to DISABLE_TELEMETRY or DISABLE_NONESSENTIAL_TRAFFIC.
  const { installNetworkPolicy } = await import('./termux-privacy-network.cjs');
  installNetworkPolicy(count, (path, factory) => mock.module(path, factory));

  // Workers create separate JS globals and do not inherit BUN_OPTIONS preloads.
  const wrap = Worker => new Proxy(Worker, { construct(Target, args, NewTarget) {
    const options = { ...(args[1] || {}) };
    const preloads = Array.isArray(options.preload) ? options.preload : options.preload ? [options.preload] : [];
    options.preload = [filename, ...preloads.filter(path => path !== filename)];
    if (options.env && typeof options.env === 'object') {
      options.env = { ...options.env, CLAUDE_TERMUX_PRIVACY_ACTIVE: 'optional-off',
        CLAUDE_TERMUX_PRIVACY_PRELOAD: filename, CLAUDE_TERMUX_PRIVACY_TARGET: process.execPath };
    }
    return Reflect.construct(Target, [args[0], options], NewTarget);
  } });
  if (typeof globalThis.Worker === 'function') globalThis.Worker = wrap(globalThis.Worker);
  if (typeof workerThreads.Worker === 'function') {
    workerThreads.Worker = wrap(workerThreads.Worker);
    // Bun's syncBuiltinESMExports alone does not refresh a statically linked
    // named Worker import. Update the live module binding as well.
    for (const name of ['worker_threads', 'node:worker_threads']) {
      mock.module(name, () => ({ ...workerThreads, default: workerThreads }));
    }
  }
  syncBuiltinESMExports();

  async function replace(key, overrides) {
    const path = profile.modules[key].path;
    const original = await import(path);
    overrides = exportOverrides(profile, key, overrides);
    for (const name of Object.keys(overrides)) {
      if (typeof original[name] !== 'function') throw Error('Unsupported privacy export: ' + key + '.' + name);
    }
    mock.module(path, () => ({ ...original, ...overrides }));
    for (const [name, replacement] of Object.entries(overrides)) {
      if (original[name] !== replacement) throw Error('Privacy replacement failed: ' + key + '.' + name);
    }
  }

  // Stop the initial event bus before importing the modules that register its
  // sinks. This prevents both pre-init buffering and later event collection.
  await replace('events', { emit: drop('event'), emitAsync: asyncDrop('event-async'), registerSink: yes });
  // cUe constructs the first-party exporter, whose constructor replays old
  // queues. Replacing initialization is essential: export() alone is too late.
  await replace('core', { emit: drop('first-party-event'), emitAsync: asyncDrop('first-party-event-async'),
    reportingOff: yes, initialize: drop('first-party-initialize'), reinitialize: asyncDrop('first-party-reinitialize'), LoggerProvider });
  // The project collector also feeds functional development-intent messages.
  // Replace collection by purpose, keeping the two results those consumers
  // need. Hash-pinned consumers are reviewed alongside this contract.
  const { createProjectClassifier } = await import('./termux-privacy-project.mjs');
  await replace('projectMarkers', { collect: createProjectClassifier({
    context: await roleModule(profile, 'projectContext'),
    repository: await roleModule(profile, 'projectRepository'),
    markers: await roleModule(profile, 'projectMarkers'),
  }) });
  await replace('datadog', { emit: asyncDrop('datadog-event'), createSender: emptySender,
    reset: drop('datadog-reset'), shutdown: async () => {} });
  await replace('errorGate', { enabled: no, initializeAllowed: no });
  await replace('errors', { report: drop('error-report'), initialize: async () => {} });
  await replace('tracing', { enabled: no, detailed: no });
  await replace('telemetry', {
    initializeTelemetry: async () => { stopContentCapture(); count('telemetry-initialize'); return { meter, metricsExporterKinds: [] }; },
    getOtlpLogExporters: async () => [], isTelemetryEnabled: no, flushTelemetry: async () => {},
  });
  await replace('feedback', { submit: async () => {
    count('feedback'); return { success: false, failureReason: 'policy_blocked' };
  } });
  const runnerApi = await roleModule(profile, 'runnerApi');
  const createRunnerApi = runnerApi.create;
  await replace('runnerApi', { create(options) {
    const api = createRunnerApi(options);
    api.forwardDiagnostics = asyncDrop('runner-diagnostics');
    return api;
  } });

  // Built-in plugins have a separate collector that probes identity, the
  // environment and repository before any request reaches the network layer.
  // Replace only its hooks module, keeping the original origin guard and the
  // plugin registry/availability behavior. Other plugins use the factory as-is.
  const registry = await roleModule(profile, 'pluginRegistry');
  const builtinTelemetry = await import(profile.modules.builtinTelemetry.path);
  const telemetryFolder = registry.normalizeFolder(builtinTelemetry.MOD_FOLDER);
  const hooksFactory = registry.hooksFactory;
  await replace('pluginRegistry', { hooksFactory(folder, hooks, scan) {
    if (folder !== telemetryFolder) return hooksFactory(folder, hooks, scan);
    if (builtinTelemetry.PLUGIN_LISTING.name !== 'cc-plugin-telemetry' ||
        typeof hooks.register !== 'function' || typeof hooks.telemetryOf !== 'function') {
      throw Error('Unsupported built-in telemetry hooks');
    }
    const quiet = { register(on) {
      hooks.register((operation, handler) => {
        if (operation === 'telemetry.*') return on(operation, handler);
        if (operation === 'telemetry.log' || operation === 'telemetry.mark') {
          return on(operation, () => {
            count('builtin-' + operation);
            return { value: undefined };
          });
        }
        if (['engine.create', 'session.start', 'session.end'].includes(operation)) return;
        throw Error('Unsupported built-in telemetry hook: ' + operation);
      });
    } };
    // The replacement has no clock, environment, file, process or network
    // capabilities. It also needs no session/engine lifecycle interceptors.
    return hooksFactory(folder, quiet, () => ({
      scan: { hooks: ['telemetry.*', 'telemetry.log', 'telemetry.mark'], calls: [],
        telemetry: [], env: { reads: [], writes: [] } }, files: {},
    }));
  } });
  const sdkPath = profile.modules.metricSdk.path;
  const sdk = await import(sdkPath);
  const sdkName = profile.bindings.metricSdk.sdk;
  const originalMetrics = sdk[sdkName]();
  // Cover consumers of the same SDK that do not use initializeTelemetry().
  originalMetrics.MeterProvider.prototype.getMeter = () => meter;
  mock.module(sdkPath, () => ({ ...sdk, [sdkName]: () => ({ ...originalMetrics, MeterProvider }) }));
  for (const signal of ['log', 'metric', 'trace']) {
    const name = { log: 'OTLPLogExporter', metric: 'OTLPMetricExporter', trace: 'OTLPTraceExporter' }[signal];
    for (const transport of ['Json', 'Proto', 'Grpc']) {
      const entry = profile.modules[signal + transport];
      const original = await import(entry.path);
      const exports = original[name] ? original : original.default;
      if (typeof exports?.[name] !== 'function') throw Error('Unsupported ' + signal + transport + ' exporter');
      // Also cover already-created references before changing module bindings.
      exports[name].prototype.export = Exporter.prototype.export;
      mock.module(entry.path, () => ({ ...original, [name]: Exporter,
        ...('default' in original ? { default: { ...original.default, [name]: Exporter } } : {}) }));
    }
  }

  state.ready = true;
  globalThis[stateKey] = state;
}

try {
  if (isTarget()) await install();
} catch (error) {
  // Fail before the application runs; never silently fall back to unprotected
  // behavior when optional-off was requested. No credentials in this message.
  console.error('claude-termux: optional-off privacy initialization failed: ' + error.message);
  process.exit(78);
}
