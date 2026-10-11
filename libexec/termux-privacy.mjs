// SPDX-License-Identifier: MIT
// Optional reporting controls for a verified Claude artifact. This is not a
// network sandbox and cannot prevent server-side collection of business traffic.
import fs from 'node:fs';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
import workerThreads from 'node:worker_threads';
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
  await replace('events', { i: drop('event'), js: asyncDrop('event-async'), rCo: yes });
  // cUe constructs the first-party exporter, whose constructor replays old
  // queues. Replacing initialization is essential: export() alone is too late.
  await replace('core', { gIt: drop('first-party-event'), jT: asyncDrop('first-party-event-async'),
    i_o: yes, cUe: drop('first-party-initialize'), bGs: asyncDrop('first-party-reinitialize'), JOt: LoggerProvider });
  await replace('datadog', { ePt: asyncDrop('datadog-event'), sbr: emptySender,
    yfo: drop('datadog-reset'), JFe: async () => {} });
  await replace('errorGate', { Bfo: no, Qbs: no });
  await replace('errors', { hfo: drop('error-report'), Czs: async () => {} });
  await replace('tracing', { JC: no, Vro: no });
  await replace('telemetry', {
    initializeTelemetry: async () => { stopContentCapture(); count('telemetry-initialize'); return { meter, metricsExporterKinds: [] }; },
    getOtlpLogExporters: async () => [], isTelemetryEnabled: no, flushTelemetry: async () => {},
  });
  await replace('feedback', { _Lt: async () => {
    count('feedback'); return { success: false, failureReason: 'policy_blocked' };
  } });
  const runnerApi = await import(profile.modules.runnerApi.path);
  const createRunnerApi = runnerApi.uin;
  await replace('runnerApi', { uin(options) {
    const api = createRunnerApi(options);
    api.forwardDiagnostics = asyncDrop('runner-diagnostics');
    return api;
  } });
  const sdkPath = profile.modules.metricSdk.path;
  const sdk = await import(sdkPath);
  const originalMetrics = sdk.NIe();
  // Cover consumers of the same SDK that do not use initializeTelemetry().
  originalMetrics.MeterProvider.prototype.getMeter = () => meter;
  mock.module(sdkPath, () => ({ ...sdk, NIe: () => ({ ...originalMetrics, MeterProvider }) }));
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
