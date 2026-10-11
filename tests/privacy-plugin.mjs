// Exercise the real bundled plugin registration and handlers with synthetic
// capabilities. The fixture never reads host identity/config or runs a shell.
import assert from 'node:assert/strict';

export async function pluginProbe(profile, isProtected, httpBase, userType) {
  const plugin = await import(profile.modules.builtinTelemetry.path);
  const registry = await import(profile.modules.pluginRegistry.path);
  // Check both public entry points: the namespace is also consumed by the
  // built-in loader. Both must use the live replacement of the hooks factory.
  (userType === 'ant' ? plugin.default : plugin).registerPlugin();
  const listing = registry.Lk(plugin.PLUGIN_LISTING.name);
  assert.equal(registry.Mgo(listing.name), true);
  assert.equal(listing.isAvailable, plugin.isAvailable);
  const registered = new Map();
  let catchHandler;
  listing.hooksModule.module.register((operation, handler) => {
    registered.set(operation, handler);
    return { catch(handler) { catchHandler = handler; } };
  });
  assert.equal(typeof catchHandler, 'function');
  const counts = Object.create(null);
  const hit = key => { counts[key] = (counts[key] || 0) + 1; };
  const rows = [];
  const env = { NODE_ENV: 'production', USER_TYPE: userType,
    CLAUDE_CONFIG_DIR: '/synthetic/config', HOME: '/synthetic/home', SHELL: '/bin/bash' };
  const identity = { userID: 'fixture_device_1234', oauthAccount: {
    accountUuid: 'fixture_account_1234', organizationUuid: 'fixture_organization_1234',
    emailAddress: 'fixture@example.invalid',
  } };
  const engine = {
    env: { async get(name) { hit('environment'); return env[name]; } },
    settings: { async read() { hit('policy'); return {}; } },
    fs: {
      async read(name) {
        hit('fileRead');
        if (name.endsWith('/.claude.json')) { hit('identityRead'); return JSON.stringify(identity); }
        if (name === '/etc/os-release') return 'ID=fixture\nVERSION_ID=1\n';
        return '';
      },
      async list() { hit('directoryList'); return ['.git']; },
      async exists() { hit('fileExists'); return false; },
    },
    process: { async run() {
      hit('processProbe');
      return { exitCode: 0, stdout: 'Linux\n6.1-fixture\naarch64\nnpm,\nbun,node,\n' };
    } },
    session: {
      async authorize() { hit('authorize'); return { kind: 'bearer', handle: 'synthetic-auth' }; },
      async cwd() { hit('cwd'); return '/synthetic/repository'; },
      async repo() { hit('repository'); return { remote: 'https://example.invalid/fixture/repo.git' }; },
      async version() { hit('version'); return { version: profile.claude }; },
      async id() { hit('sessionId'); return 'synthetic-session'; },
      async model() { hit('model'); return 'synthetic-model'; },
      async surfaces() { hit('surfaces'); return ['terminal']; },
    },
    clock: {
      after() { hit('timer'); return { cancel() { hit('cancelTimer'); } }; },
      async sleep() { hit('retry'); },
    },
    http: { async fetch(url, options) {
      hit('send');
      assert.equal(url, 'https://api.anthropic.com/api/event_logging/v2/batch');
      rows.push(...JSON.parse(options.body).events.map(row => row.event_data));
      // A distinct local path proves that stopping the producer does not
      // depend on the known collector URL fallback.
      return fetch(httpBase + '/plugin-telemetry', { method: 'POST', body: options.body });
    } },
    ui: { async log() { hit('debug'); } },
    taskMarker: 'business-engine',
  };
  const lifecycle = async (operation, payload, result) => {
    let continued = 0;
    const next = () => { continued++; return result; };
    const value = registered.has(operation) ? await registered.get(operation)({}, payload, next) : next();
    assert.equal(continued, 1);
    return value;
  };
  await lifecycle('session.start', { isInteractive: true }, 'session-started');
  const created = await lifecycle('engine.create', {}, engine);
  assert.equal(created.taskMarker, 'business-engine');
  assert.equal(created.session, engine.session);
  const dispatch = async (operation, payload, tier = 'builtin') => {
    const next = args => {
      next.called = true;
      return registered.get(operation)({}, args, () => { hit('downstream'); return { value: undefined }; });
    };
    next.origin = { tier };
    return registered.get('telemetry.*')({}, payload, next);
  };
  const log = { event: 'privacy_fixture', props: { count: 1 } };
  const mark = { feature: 'privacy_fixture', kind: 'ok', props: { enabled: true } };
  for (const [operation, payload] of [['telemetry.log', log], ['telemetry.mark', mark]]) {
    assert.ok(Object.hasOwn(await dispatch(operation, payload), 'value'));
    assert.ok((await dispatch(operation, payload, 'external')).deny);
  }
  // Let the original collector finish its independent context collection.
  await new Promise(resolve => setTimeout(resolve, 30));
  const beforeFlush = { ...counts };
  assert.equal(await lifecycle('session.end', {}, 'session-ended'), 'session-ended');
  if (isProtected) {
    // Getters make accidental payload parsing fail; collector-directed entries
    // must also be discarded without delegating to another sender.
    const unreadable = new Proxy({}, { get() { throw Error('telemetry payload inspected'); },
      ownKeys() { throw Error('telemetry payload enumerated'); } });
    assert.ok(Object.hasOwn(await dispatch('telemetry.log', unreadable), 'value'));
    assert.ok(Object.hasOwn(await dispatch('telemetry.mark', unreadable, 'core'), 'value'));
    await dispatch('telemetry.log', { ...log, to: 'collector' });
    assert.deepEqual(Object.keys(counts), []);
    assert.equal(rows.length, 0);
    assert.deepEqual(listing.hooksModule.scan.calls, []);
    assert.deepEqual(listing.hooksModule.scan.env, { reads: [], writes: [] });
    assert.equal(registered.has('engine.create'), false);
  } else {
    assert.ok(beforeFlush.environment > 0 && beforeFlush.identityRead > 0 && beforeFlush.timer > 0);
    assert.ok(counts.processProbe > 0 && counts.repository > 0);
    assert.equal(counts.send, 1);
    assert.equal(rows.length, 2);
    assert.ok(rows.every(row => row.device_id === identity.userID));
    assert.ok(rows.every(row => (row.email === identity.oauthAccount.emailAddress) === (userType === 'ant')));
  }
  // The shared factory still preserves the identity and behavior of an
  // unrelated plugin, including its lifecycle handler and capability scan.
  let otherCalls = 0;
  const other = { register(on) { on('session.start', () => { otherCalls++; }); } };
  const scan = { hooks: ['session.start'], calls: ['session.id'] };
  const wrapped = registry.qW(registry.HUn('privacy-business-fixture'), other, () => ({ scan, files: {} }));
  assert.equal(wrapped.module, other);
  assert.equal(wrapped.scan, scan);
  wrapped.module.register((_operation, callback) => callback());
  assert.equal(otherCalls, 1);
  return { userType, counts, beforeFlush, rows: rows.length,
    emailIncluded: rows.some(row => row.email !== undefined), lifecyclePassed: true,
    originRestrictionPassed: true, unrelatedPluginPassed: true,
    payloadUntouched: isProtected, capabilityCalls: listing.hooksModule.scan.calls };
}
