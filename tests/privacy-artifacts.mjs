// Replay the bundled artifact reporter with synthetic payloads and a recording
// shared client. No artifact is published and no credential is sent anywhere.
import assert from 'node:assert/strict';
import { mock } from 'bun:test';
import { roleModule, exportOverrides } from '../libexec/termux-privacy-profile.mjs';

export async function artifactProbe(profile, isProtected) {
  const tracking = await roleModule(profile, 'artifactTracking');
  const client = await roleModule(profile, 'artifactClient');
  const facade = await roleModule(profile, 'artifactFacade');
  const publicFacade = await import(profile.modules.artifactFacade.path);
  const api = client.api;
  const originalPost = api.post;
  const originalGet = api.get;
  const entryPath = profile.modules.artifactEntrypoint.path;
  const entry = { ...await import(entryPath) };
  const credentials = Object.freeze({ marker: 'SYNTHETIC_ARTIFACT_CREDENTIAL_HANDLE' });
  const rows = [];
  let response = 'success', payloadReads = 0;
  const spy = async (path, body, options) => {
    rows.push({ path, body: structuredClone(body), refreshOAuth: options?.refreshOAuth,
      timeout: options?.timeout, credentialsPreserved: options?.credentials === credentials,
      headerNames: Object.keys(options?.headers ?? {}).sort() });
    if (response === 'throw') throw Error('Synthetic artifact request failure');
    if (response === 'blocked') return { ok: false, reason: 'no-auth' };
    return { ok: true, status: 204, data: undefined };
  };
  const invoke = async (fn, payload) => {
    const promise = fn('frame_surfaced', payload);
    assert.equal(typeof promise?.then, 'function');
    assert.equal(await promise, undefined);
  };
  try {
    api.post = spy;
    await invoke(tracking.track, { slug: 'synthetic-artifact', via: 'terminal_link',
      mode: 'auto_open_skipped_remote_control', credentials });
    await invoke(tracking.track, { credentials });
    response = 'blocked';
    await invoke(tracking.track, { slug: 'synthetic-blocked', credentials });
    response = 'throw';
    await invoke(tracking.track, { slug: 'synthetic-failure', credentials });
    response = 'success';
    await invoke(facade.track, { slug: 'synthetic-facade', credentials });
    const reporterPosts = rows.length;
    if (isProtected) {
      const unreadable = new Proxy({}, {
        get() { payloadReads++; throw Error('Artifact tracking payload inspected'); },
        ownKeys() { payloadReads++; throw Error('Artifact tracking payload enumerated'); },
      });
      await invoke(tracking.track, unreadable);
      await invoke(facade.track, unreadable);
      assert.equal(payloadReads, 0);
      assert.equal(rows.length, 0);
    } else {
      assert.equal(reporterPosts, 5);
      assert.ok(rows.every(row => row.path === '/api/frame/track'));
      assert.ok(rows.every(row => row.refreshOAuth === true && row.timeout === 5000 && row.credentialsPreserved));
      assert.deepEqual(rows[0].body, { event_name: 'frame_surfaced', slug: 'synthetic-artifact',
        via: 'terminal_link', mode: 'auto_open_skipped_remote_control' });
      assert.deepEqual(rows[1].body, { event_name: 'frame_surfaced' });
      assert.ok(rows[0].headerNames.includes('X-Frame-Session-Id'));
      assert.ok(rows[0].headerNames.includes('X-Frame-Client-Version'));
    }
    // The production policy controls the reporter, leaving this same shared
    // client's request interface available to publishing and reading paths.
    assert.equal(api.get, originalGet);
    assert.equal(api.post, spy);
    const control = await api.post('/api/frame/deploy/direct', { marker: 'SYNTHETIC_CONTROL' }, { credentials });
    assert.equal(control.ok, true);
    assert.equal(rows.length, reporterPosts + 1);
    const surfaces = [];
    for (const [platform, desktop, expected] of [
      ['claude-vscode', false, 'epitaxy_pane'],
      ['cli', true, 'desktop_pane'],
      ['cli', false, 'terminal_link'],
    ]) {
      mock.module(entryPath, () => ({ ...entry, ...exportOverrides(profile, 'artifactEntrypoint', {
        entrypoint: () => platform, desktop: () => desktop,
      }) }));
      assert.equal(tracking.surface(), expected);
      assert.equal(publicFacade.surfacedViaForEntrypoint(), expected);
      surfaces.push(expected);
    }
    return { reporterPosts, publicFacadeStopped: isProtected, payloadReads,
      asyncUndefinedContract: true, sharedClientControl: true, surfaces,
      fields: isProtected ? [] : Object.keys(rows[0].body).sort(),
      limitation: 'Component replay records md.post; it does not authenticate or publish a real artifact.' };
  } finally {
    api.post = originalPost;
    mock.module(entryPath, () => entry);
  }
}
