// Invoke actual bundled components and transports with synthetic local data.
import fs from 'node:fs';
import http from 'node:http';
import http2 from 'node:http2';
import { request as namedRequest } from 'node:http';
import { connect as namedConnect } from 'node:http2';
import { Worker as NodeWorker } from 'node:worker_threads';
import { Worker as BareNodeWorker } from 'worker_threads';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { pluginProbe } from './privacy-plugin.mjs';
import { roleModule } from '../libexec/termux-privacy-profile.mjs';

const stateKey = Symbol.for('claude-termux.privacy.v1');
const isProtected = process.env.PRIVACY_TEST_MODE === 'optional-off';
const report = { cases: [], mode: process.env.PRIVACY_TEST_MODE, ready: !!globalThis[stateKey]?.ready };
const profile = JSON.parse(fs.readFileSync(new URL('../libexec/privacy-profile.json', import.meta.url), 'utf8'));
const marker = 'SYNTHETIC_PRIVACY_CANARY';
const httpBase = 'http://127.0.0.1:' + process.env.PRIVACY_HTTP_PORT;
const h2Base = 'http://127.0.0.1:' + process.env.PRIVACY_H2_PORT;
const run = async (name, fn) => {
  try { report.cases.push({ name, ...(await fn()) }); }
  catch (e) { report.cases.push({ name, error: String(e) }); }
};
const moduleAt = key => roleModule(profile, key);
const resource = { attributes: { 'service.name': marker } };
const scope = { name: marker, version: '1' };
const log = { resource, instrumentationScope: scope, hrTime: [1700000000, 0], hrTimeObserved: [1700000000, 0],
  severityNumber: 9, severityText: 'INFO', body: marker, attributes: {}, droppedAttributesCount: 0 };
const metric = { resource, scopeMetrics: [{ scope, metrics: [{
  descriptor: { name: 'privacy_probe', type: 'COUNTER', unit: '1', description: marker, valueType: 1 },
  dataPointType: 3, aggregationTemporality: 1, isMonotonic: true,
  dataPoints: [{ attributes: {}, startTime: [1700000000, 0], endTime: [1700000001, 0], value: 1 }],
}] }] };
const span = { resource, instrumentationScope: scope, name: marker, kind: 0,
  spanContext: () => ({ traceId: '12345678901234567890123456789012', spanId: '1234567890123456', traceFlags: 1 }),
  startTime: [1700000000, 0], endTime: [1700000001, 0], duration: [1, 0], status: { code: 0 },
  attributes: {}, links: [], events: [], droppedAttributesCount: 0, droppedEventsCount: 0, droppedLinksCount: 0, ended: true };

process.env.no_grpc_proxy = '127.0.0.1,localhost';
for (const signal of ['log', 'metric', 'trace']) for (const transport of ['Json', 'Proto', 'Grpc']) {
  await run(signal + transport, async () => {
    const module = await moduleAt(signal + transport);
    const name = { log: 'OTLPLogExporter', metric: 'OTLPMetricExporter', trace: 'OTLPTraceExporter' }[signal];
    const Class = module[name] || module.default?.[name];
    const exporter = new Class({ url: transport === 'Grpc' ? h2Base : httpBase + '/' + signal + transport, timeoutMillis: 2500 });
    try {
      const result = await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(Error('export timeout')), 5000);
        exporter.export(signal === 'metric' ? metric : [signal === 'log' ? log : span], value => { clearTimeout(timer); resolve(value); });
      });
      if (result.code !== 0) throw result.error || Error('export code ' + result.code);
      return { code: result.code };
    } finally { await exporter.shutdown(); }
  });
}

for (const path of ['/api/event_logging/v2/batch', '/api/claude_code/metrics', '/api/claude_cli_feedback', '/v1/messages', '/api/eval/fixture']) {
  await run('fetch:' + path, async () => {
    const r = await fetch('http://api.anthropic.com' + path, { method: 'POST', body: marker, signal: AbortSignal.timeout(5000) });
    await r.text(); return { status: r.status };
  });
  await run('http:' + path, () => new Promise((resolve, reject) => {
    const r = http.request(httpBase, { method: 'POST', path: 'http://api.anthropic.com' + path }, response => {
      response.resume(); response.on('end', () => resolve({ status: response.statusCode }));
    });
    r.on('error', reject); r.end(marker);
  }));
  await run('http2:' + path, () => new Promise((resolve, reject) => {
    const client = http2.connect(h2Base);
    const timer = setTimeout(() => { client.destroy(); reject(Error('HTTP/2 timeout')); }, 5000);
    client.on('error', reject);
    const request = client.request({ ':method': 'POST', ':authority': 'api.anthropic.com', ':path': path });
    let status;
    request.on('response', headers => { status = headers[':status']; });
    request.on('data', () => {}); request.on('error', reject);
    request.on('end', () => { clearTimeout(timer); client.close(); resolve({ status }); });
    request.end(marker);
  }));
}
for (const path of ['/api/event_logging/v2/batch', '/v1/messages']) {
  await run('named-http:' + path, () => new Promise((resolve, reject) => {
    const r = namedRequest(httpBase, { method: 'POST', path: 'http://api.anthropic.com' + path }, response => {
      response.resume(); response.on('end', () => resolve({ status: response.statusCode }));
    });
    r.on('error', reject); r.end(marker);
  }));
  await run('named-http2:' + path, () => new Promise((resolve, reject) => {
    const client = namedConnect(h2Base);
    const timer = setTimeout(() => { client.destroy(); reject(Error('named HTTP/2 timeout')); }, 5000);
    client.on('error', reject);
    const r = client.request({ ':method': 'POST', ':authority': 'api.anthropic.com', ':path': path });
    let status;
    r.on('response', headers => { status = headers[':status']; });
    r.on('data', () => {}); r.on('error', reject);
    r.on('end', () => { clearTimeout(timer); client.close(); resolve({ status }); }); r.end(marker);
  }));
}

await run('event-buffer', async () => {
  const bus = await moduleAt('events');
  let received = 0;
  bus.emit(marker, {});
  bus.registerSink({ logEvent() { received++; }, async logEventAsync() { received++; } });
  await Promise.resolve(); bus.emit(marker, {}); await bus.emitAsync(marker, {});
  if (received !== (isProtected ? 0 : 3)) throw Error('unexpected event sink count: ' + received);
  return { received };
});
await run('logger-producer', async () => {
  const core = await moduleAt('core');
  let received = 0;
  const provider = new core.LoggerProvider({ processors: [{ onEmit() { received++; }, async forceFlush() {}, async shutdown() {} }] });
  provider.getLogger('privacy-test').emit(log);
  await provider.shutdown();
  if (received !== (isProtected ? 0 : 1)) throw Error('unexpected log producer count: ' + received);
  return { received };
});
await run('datadog-queue', async () => {
  const dd = await moduleAt('datadog'); let received = 0;
  const sender = dd.createSender({ maxBatchSize: 100, getFlushIntervalMs: () => 60000, post: async b => { received += b.length; } });
  sender.enqueue({ marker }); await sender.shutdown();
  if (received !== (isProtected ? 0 : 1)) throw Error('unexpected datadog count: ' + received);
  return { received };
});
await run('feature-gates', async () => {
  const core = await moduleAt('core');
  const entry = profile.test_bindings.traffic;
  const ADt = (await import(entry.path))[entry.export];
  if (!core.featureFlags() || ADt() !== 'default') throw Error('functional traffic gate was disabled');
  return { featureFlags: core.featureFlags(), trafficMode: ADt() };
});
for (const userType of ['external', 'ant']) {
  await run('builtin-plugin-' + userType, () => pluginProbe(profile, isProtected, httpBase, userType));
}
await run('old-queue-replay', async () => {
  const core = await moduleAt('core');
  const entry = profile.test_bindings.identity;
  const V = (await import(entry.path))[entry.export];
  const folder = process.env.CLAUDE_CONFIG_DIR + '/telemetry';
  fs.mkdirSync(folder, { recursive: true });
  const queue = folder + '/1p_failed_events.' + V() + '.synthetic.json';
  fs.writeFileSync(queue, '[]');
  core.initialize();
  await new Promise(resolve => setTimeout(resolve, 150));
  const untouched = fs.existsSync(queue) && fs.readFileSync(queue, 'utf8') === '[]';
  await core.drain();
  if (untouched !== isProtected) throw Error('unexpected old queue replay behavior: ' + untouched);
  return { untouched };
});

if (isProtected) {
  await run('initialization-and-replay', async () => {
    const core = await moduleAt('core');
    const init = await moduleAt('telemetry');
    // Any access to the storage interface is an error. The real initializer
    // would construct a replaying exporter; replacement must not touch it.
    const storage = new Proxy({}, { get() { throw Error('telemetry storage was accessed'); } });
    core.initialize(storage); await core.reinitialize();
    const value = await init.initializeTelemetry(storage);
    value.meter.createCounter('test').add(1);
    if (value.metricsExporterKinds.length) throw Error('telemetry exporters still initialized');
    return { storageUntouched: true };
  });
  await run('feedback', async () => {
    const feedback = await moduleAt('feedback');
    const value = await feedback.submit(new Proxy({}, { get() { throw Error('feedback payload inspected'); } }));
    if (value.success !== false || value.failureReason !== 'policy_blocked') throw Error('feedback was not disabled');
    return value;
  });
  await run('runner-diagnostics', async () => {
    const module = await moduleAt('runnerApi');
    const api = module.create({});
    await api.forwardDiagnostics('http://api.anthropic.com', 'synthetic', 'fake-token', 0,
      new Proxy([], { get() { throw Error('diagnostic payload inspected'); } }));
    if (typeof api.heartbeat !== 'function' || typeof api.registerRunner !== 'function') throw Error('runner business methods changed');
    return { payloadUntouched: true };
  });
  const childFile = new URL('./privacy-worker.mjs', import.meta.url);
  for (const kind of ['bun', 'node', 'node-bare']) for (const explicitEnv of [false, true]) {
    await run('worker-' + kind + '-' + explicitEnv, () => new Promise((resolve, reject) => {
      const Worker = kind === 'bun' ? globalThis.Worker : kind === 'node' ? NodeWorker : BareNodeWorker;
      const worker = new Worker(childFile, { ...(explicitEnv ? { env: {} } : {}) });
      const timer = setTimeout(() => { worker.terminate(); reject(Error('worker timeout')); }, 7000);
      const done = value => { clearTimeout(timer); worker.terminate(); value.ready ? resolve(value) : reject(Error('worker unprotected')); };
      if (kind === 'bun') { worker.onmessage = event => done(event.data); worker.onerror = reject; }
      else { worker.on('message', done); worker.on('error', reject); }
    }));
  }
  const link = path.join(process.env.TMPDIR, 'claude-alias');
  fs.symlinkSync(process.execPath, link);
  for (const [label, target] of [['absolute', process.execPath], ['relative', './claude'], ['symlink', link], ['proc', '/proc/self/exe']]) {
    await run('clean-environment-self-spawn-' + label, async () => {
    const result = spawnSync(target, ['--version'], { cwd: path.dirname(process.execPath), env: {
      BUN_OPTIONS: '--preload ' + new URL('./privacy-child.mjs', import.meta.url).pathname,
      CLAUDE_CONFIG_DIR: process.env.CLAUDE_CONFIG_DIR, TMPDIR: process.env.TMPDIR,
      HOME: process.env.HOME,
    }, encoding: 'utf8', timeout: 15000 });
    if (result.status !== 0) throw Error('child failed: ' + result.stderr);
    const value = JSON.parse(result.stdout);
    if (!value.ready) throw Error('child unprotected');
    return value;
  });
  }
  report.counts = globalThis[stateKey]?.counts;
}
fs.writeFileSync(process.env.PRIVACY_REPORT, JSON.stringify(report, null, 2) + '\n');
process.exit(report.cases.some(c => c.error) || report.ready !== isProtected ? 1 : 0);
