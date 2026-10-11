// Execute hash-pinned original source fragments with explicit test dependencies.
// This is a component replay, not a running authenticated enterprise gateway.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { writeFileSync } from 'node:fs';

const report = { mode: process.env.CLAUDE_TERMUX_PRIVACY, cases: [] };
const hash = value => createHash('sha256').update(value).digest('hex');
try {
  assert.equal(process.env.GATEWAY_RESEARCH_FIXTURE, 'loopback-only-v1');
  const profile = await Bun.file(new URL('./gateway-profile.json', import.meta.url)).json();
  assert.equal(Bun.version, profile.bun);
  const source = await Bun.file(profile.module).text();
  assert.equal(hash(source), profile.module_sha256);
  const fragments = {};
  for (const [name, row] of Object.entries(profile.fragments)) {
    fragments[name] = source.slice(row.start_utf16, row.end_utf16);
    assert.equal(hash(fragments[name]), row.sha256, name);
  }
  report.module_sha256 = hash(source);
  const privacy = globalThis[Symbol.for('claude-termux.privacy.v1')];
  report.privacy_ready = privacy?.ready === true;
  assert.equal(report.privacy_ready, report.mode === 'optional-off');
  const base = `http://127.0.0.1:${Number(process.env.GATEWAY_RESEARCH_PORT)}`;
  let label = '', calls = [], warnings = [], googleFactories = 0;
  const send = async (address, options) => {
    const url = new URL(address);
    assert.equal(url.origin, base);
    assert.equal(options.honorNoProxy, true);
    const { honorNoProxy, ...init } = options;
    calls.push({ path: url.pathname, method: init.method, body_sha256: hash(Buffer.from(init.body)),
      headers: Object.fromEntries(new Headers(init.headers)) });
    return fetch(url, init);
  };
  const factory = new Function('Is', 'gr', 'nN', [fragments.constants, fragments.Gu,
    fragments.fanout, fragments.ew, fragments.Uhe,
    `return {Fanout:gw,settings:Uhe,route:async function(Zt,he,r,P){${fragments.relay_route};return null;}};`].join('\n'));
  const components = factory(send, (...args) => warnings.push(args), () => {
    googleFactories++;
    return async () => ({ authorization: 'Bearer SYNTHETIC_GOOGLE_TOKEN', 'x-goog-user-project': 'synthetic-default' });
  });
  const body = Buffer.from('synthetic gateway relay body\n');
  const target = (suffix, extra = {}) => ({ url: `${base}/${label}/${suffix}`,
    headers: { 'x-fixture-destination': suffix }, metrics: true, logs: false, traces: false, ...extra });
  const run = async (name, fn) => {
    label = name; calls = []; warnings = []; googleFactories = 0;
    const facts = await fn();
    report.cases.push({ name, passed: true, calls: structuredClone(calls), warning_count: warnings.length, ...facts });
  };
  const drained = async relay => {
    const end = Date.now() + 12000;
    while (relay.inFlight && Date.now() < end) await new Promise(resolve => setTimeout(resolve, 5));
    assert.equal(relay.inFlight, 0);
  };
  await run('empty-fanout', async () => {
    const relay = new components.Fanout();
    await relay.fanout('/v1/metrics', body, 'application/x-protobuf', null, []);
    assert.equal(calls.length, 0);
  });
  await run('unconfigured-route', async () => {
    let reads = 0;
    const request = { method: 'POST', arrayBuffer() { reads++; throw Error('Body should not be read'); } };
    const response = await components.route('/v1/metrics', request, { telemetry: { forward_to: [] } }, new components.Fanout());
    assert.equal(response.status, 200); assert.equal(reads, 0); assert.equal(calls.length, 0);
    return { body_reads: reads };
  });
  for (const signal of ['metrics', 'logs', 'traces']) {
    await run(`route-${signal}`, async () => {
      const relay = new components.Fanout();
      const destinations = [target('a', { metrics: true, traces: true }), target('b', { metrics: false, logs: true, traces: true })];
      const response = await components.route(`/v1/${signal}`, new Request(`${base}/v1/${signal}`, {
        method: 'POST', body, headers: { 'Content-Type': 'application/x-protobuf', 'Content-Encoding': 'fixture-encoding' },
      }), { telemetry: { forward_to: destinations } }, relay);
      assert.equal(response.status, 200); await drained(relay);
      assert.deepEqual(calls.map(row => row.path.split('/')[2]).sort(), signal === 'metrics' ? ['a'] : signal === 'logs' ? ['b'] : ['a', 'b']);
      assert.ok(calls.every(row => row.body_sha256 === hash(body) && row.headers['content-encoding'] === 'fixture-encoding'));
      return { forwarded_body_unchanged: true };
    });
  }
  await run('non-post-route', async () => {
    const response = await components.route('/v1/metrics', new Request(`${base}/v1/metrics`),
      { telemetry: { forward_to: [target('a')] } }, new components.Fanout());
    assert.equal(response, null); assert.equal(calls.length, 0);
  });
  await run('saturation', async () => {
    const relay = new components.Fanout(); relay.inFlight = 128;
    await relay.fanout('/v1/metrics', body, 'application/x-protobuf', null, [target('a')]);
    assert.equal(calls.length, 0); assert.equal(relay.dropped, 1);
  });
  await run('failure-circuit', async () => {
    const relay = new components.Fanout(), destination = target('fail');
    for (let i = 0; i < 6; i++) await relay.fanout('/v1/logs', body, 'application/x-protobuf', null, [destination]);
    assert.equal(calls.length, 5); assert.equal(relay.circuitBreakers.get(destination.url).fails, 5);
    assert.ok(relay.circuitBreakers.get(destination.url).openUntil > Date.now());
    return { sixth_attempt_skipped: true };
  });
  await run('payload-refusal', async () => {
    const relay = new components.Fanout(), destination = target('refuse');
    for (let i = 0; i < 6; i++) await relay.fanout('/v1/logs', body, 'application/x-protobuf', null, [destination]);
    assert.equal(calls.length, 6); assert.equal(relay.refused.get(destination.url), 6);
    assert.equal(relay.circuitBreakers.size, 0);
  });
  await run('independent-destinations', async () => {
    const relay = new components.Fanout(), good = target('good'), bad = target('fail');
    await relay.fanout('/v1/traces', body, 'application/x-protobuf', null, [good, bad]);
    assert.equal(calls.length, 2); assert.equal(relay.circuitBreakers.has(good.url), false);
    assert.equal(relay.circuitBreakers.get(bad.url).fails, 1);
  });
  await run('synthetic-google-headers', async () => {
    const relay = new components.Fanout();
    const destination = target('a', { auth: { google: { fixture: true } },
      headers: { Authorization: 'SYNTHETIC_OLD_TOKEN', 'x-goog-user-project': 'synthetic-explicit' } });
    for (let i = 0; i < 2; i++) await relay.fanout('/v1/metrics', body, 'application/x-protobuf', null, [destination]);
    assert.equal(googleFactories, 1); assert.equal(calls.length, 2);
    assert.ok(calls.every(row => row.headers.authorization === 'Bearer SYNTHETIC_GOOGLE_TOKEN'
      && row.headers['x-goog-user-project'] === 'synthetic-explicit'));
    return { google_auth_provider_replaced_with_synthetic_fixture: true };
  });
  await run('no-forwarding-settings', async () => {
    assert.equal(components.settings({ telemetry: { forward_to: [] }, listen: {} }), null);
    assert.equal(calls.length, 0);
  });
  await run('no-public-url-settings', async () => {
    assert.equal(components.settings({ telemetry: { forward_to: [target('a')] }, listen: {} }), null);
    assert.equal(warnings.length, 1); assert.equal(calls.length, 0);
  });
  await run('explicit-forwarding-settings', async () => {
    const settings = components.settings({ telemetry: { forward_to: [target('a')],
      resource_attributes: { fixture: 'synthetic' } }, listen: { public_url: base } });
    assert.deepEqual(settings, { CLAUDE_CODE_ENABLE_TELEMETRY: '1', OTEL_METRICS_EXPORTER: 'otlp',
      OTEL_LOGS_EXPORTER: 'none', OTEL_TRACES_EXPORTER: 'none', OTEL_EXPORTER_OTLP_ENDPOINT: base,
      OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf', OTEL_RESOURCE_ATTRIBUTES: 'fixture=synthetic' });
    assert.equal(calls.length, 0);
    return { output_environment_keys: Object.keys(settings).sort(), configured_metrics_only: true };
  });
  report.passed = true;
} catch (error) {
  report.passed = false;
  report.error = String(error?.stack || error);
}
writeFileSync(process.env.GATEWAY_RESEARCH_REPORT, JSON.stringify(report, null, 2) + '\n');
process.exit(report.passed ? 0 : 1);
