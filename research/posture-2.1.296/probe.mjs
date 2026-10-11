// Research-only candidate controls with known defects; never install as a privacy policy.
// Run through study.py. All callers must be isolated synthetic CLI fixtures.
import fs from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';
import { AsyncLocalStorage } from 'node:async_hooks';
import { mock } from 'bun:test';

// Refuse accidental use with a real account or working directory. The harness
// also routes every CONNECT to its local TLS receiver and rejects external DNS.
// These checks are fixture guards, not a network sandbox.
const report = process.env.POSTURE_RESEARCH_REPORT;
const directory = report && path.dirname(path.resolve(report));
const proxy = process.env.HTTPS_PROXY;
if (process.env.POSTURE_RESEARCH_FIXTURE !== 'posture-local-fixture-v1'
    || process.env.ANTHROPIC_API_KEY !== 'synthetic-dataflow-key-no-account'
    || process.env.ANTHROPIC_BASE_URL !== 'https://api.anthropic.com'
    || !/^http:\/\/127\.0\.0\.1:[0-9]+$/.test(proxy ?? '')
    || ['HTTP_PROXY', 'http_proxy', 'https_proxy'].some(k => process.env[k] !== proxy)
    || !directory || path.resolve(report) !== path.join(directory, 'posture.json')
    || process.env.HOME !== path.join(directory, 'home')
    || process.env.CLAUDE_CONFIG_DIR !== path.join(directory, 'config')
    || process.cwd() !== path.join(directory, 'workspace')) {
  throw Error('Posture study requires its isolated synthetic local fixture');
}

const mode = process.env.POSTURE_RESEARCH_MODE;
if (!['observe', 'producer', 'constructor'].includes(mode)) throw Error('Explicit research mode required');
const entries = {
  request: { path: '/$bunfs/root/chunk-dj5rhvvh.js', sha256: 'b9d31cb5ef4ec3d55b087441ee962df71c432ee373ca773553b6908cd050290a' },
  git: { path: '/$bunfs/root/chunk-z4p55bfk.js', sha256: 'f788e135a425bf9156abf46ad1a664ed26e7490f0bfc4bfa4f6f30f1e5877656' },
};
for (const entry of Object.values(entries)) {
  if (crypto.createHash('sha256').update(fs.readFileSync(entry.path)).digest('hex') !== entry.sha256)
    throw Error('Unverified posture research module');
}
const request = await import(entries.request.path), git = await import(entries.git.path);
const create = request.eU, Client = request.rbe, remote = git.Lut;
if (![create, Client, remote].every(x => typeof x === 'function')) throw Error('Missing research binding');
const scope = new AsyncLocalStorage();
const rows = { mode, clientCalls: 0, constructors: 0, scopedRemoteCalls: 0,
  originalScopedRemoteCalls: 0, outsideRemoteCalls: 0, stoppedRemoteCalls: 0,
  callerFrames: {}, headerRows: [], controlRemotePresent: false, scopedStateRestored: false };

// The pinned serializer emits a Structured Fields dictionary. Split commas
// outside quoted values so a comma in a synthetic Git path is never mistaken
// for a field boundary. This experiment rejects unsupported jy shapes.
export function stripOrigin(value) {
  const members = []; let start = 0, quoted = false, escaped = false;
  for (let i = 0; i < value.length; i++) {
    const ch = value[i];
    if (quoted) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === '"') quoted = false;
    } else if (ch === '"') quoted = true;
    else if (ch === ',') { members.push(value.slice(start, i)); start = i + 1; }
  }
  if (quoted || escaped) throw Error('Unterminated research header');
  members.push(value.slice(start));
  let removed = 0;
  const kept = members.filter(member => {
    if (!/^\s*jy(?:\s*=|\s*;|\s*$)/.test(member)) return true;
    if (!/^\s*jy="(?:[^"\\]|\\["\\])*"\s*$/.test(member)) throw Error('Unsupported origin field shape');
    removed++; return false;
  });
  return { value: kept.join(','), removed };
}

const wrappedRemote = function (...args) {
  if (scope.getStore() === 'client-construction') {
    rows.scopedRemoteCalls++;
    const frame = (new Error().stack ?? '').split('\n').find(s => s.includes('chunk-dj5rhvvh.js')) ?? 'unknown';
    const location = frame.replace(/.*\bat\s+/, '').replace(/\s+/g, ' ').trim();
    rows.callerFrames[location] = (rows.callerFrames[location] ?? 0) + 1;
    if (mode === 'producer') { rows.stoppedRemoteCalls++; return null; }
    rows.originalScopedRemoteCalls++;
  } else rows.outsideRemoteCalls++;
  return Reflect.apply(remote, this, args);
};
const wrappedCreate = function (...args) {
  rows.clientCalls++;
  if (process.env.POSTURE_RESEARCH_SHARED_GETTER === '1') {
    const original = args[0];
    const descriptors = Object.getOwnPropertyDescriptors(original);
    descriptors.apiKey = { configurable: true, enumerable: true, get() {
      rows.sharedGetterCalls = (rows.sharedGetterCalls ?? 0) + 1;
      rows.sharedGetterOriginPresent ??= [];
      rows.sharedGetterOriginPresent.push(git.Lut(process.cwd()) === 'git.fixture.invalid/group/project');
      return Reflect.get(original, 'apiKey', original);
    } };
    args = [Object.defineProperties(Object.create(Object.getPrototypeOf(original)), descriptors), ...args.slice(1)];
  }
  // Intentionally broad candidate: the getter case demonstrates its overreach.
  // This does not establish that a current production caller uses such a getter.
  return scope.run('client-construction', () => Reflect.apply(create, this, args));
};
// This candidate runs after origin collection and the original debug logger.
// It only recognizes the canonical name, not the unknown digest alias.
const wrappedClient = new Proxy(Client, {
  construct(Target, args, NewTarget) {
    rows.constructors++;
    const options = args[0], headers = options?.defaultHeaders;
    const key = headers && Object.keys(headers).find(k => k.toLowerCase() === 'x-claude-code-wiggly-dove');
    const value = key && headers[key];
    if (typeof value === 'string') {
      const filtered = stripOrigin(value);
      rows.headerRows.push({ hadOrigin: filtered.removed > 0,
        keptHeaderSha256: crypto.createHash('sha256').update(filtered.value).digest('hex'),
        otherFields: [...filtered.value.matchAll(/(?:^|,)\s*([a-z][a-z0-9_-]*)=/g)].map(x => x[1]).sort(),
        removed: mode === 'constructor' ? filtered.removed : 0 });
      if (mode === 'constructor' && filtered.removed) {
        args = [{ ...options, defaultHeaders: { ...headers, [key]: filtered.value } }, ...args.slice(1)];
      }
    }
    return Reflect.construct(Target, args, NewTarget);
  },
});
mock.module(entries.git.path, () => ({ ...git, Lut: wrappedRemote }));
mock.module(entries.request.path, () => ({ ...request, eU: wrappedCreate, rbe: wrappedClient }));
if (git.Lut !== wrappedRemote || request.eU !== wrappedCreate || request.rbe !== wrappedClient)
  throw Error('Research live binding replacement failed');
process.on('exit', () => {
  rows.scopedStateRestored = scope.getStore() === undefined;
  // A shared Git lookup outside the client scope must keep its real behavior.
  // Only a synthetic marker equality is retained, never the actual result.
  rows.controlRemotePresent = git.Lut(process.cwd()) === 'git.fixture.invalid/group/project';
  fs.writeFileSync(process.env.POSTURE_RESEARCH_REPORT, JSON.stringify(rows));
});
