// Observe only dedicated analytics scopes. Record counts and field names, not
// paths, command arguments, setting values or remote URLs. Business calls
// outside those scopes are deliberately excluded from collection counts.
import fs from 'node:fs';
import promises from 'node:fs/promises';
import crypto from 'node:crypto';
import path from 'node:path';
import assert from 'node:assert/strict';
import { AsyncLocalStorage } from 'node:async_hooks';
import { mock } from 'bun:test';
import { roleModule } from '../libexec/termux-privacy-profile.mjs';

const collectors = [
  ['git', 'projectRepository', 'analytics'], ['files', 'fileAnalytics', 'collect'],
  ['permissions', 'permissionAnalytics', 'collect'], ['accessibility', 'accessibilityAnalytics', 'collect'],
];

export async function observeCollectors(profile) {
  const context = new AsyncLocalStorage();
  const rows = Object.fromEntries(collectors.map(([name]) => [name, {
    calls: 0, completed: 0, resultFields: new Set(), resultTypes: new Set(),
    fileReads: 0, gitConfigReads: 0, fileEnumerations: 0, gitChecks: 0, remoteLookups: 0,
  }]));
  const modules = new Map();
  for (const [, key] of collectors) {
    const entry = profile.modules[key];
    if (crypto.createHash('sha256').update(fs.readFileSync(entry.path)).digest('hex') !== entry.sha256) throw Error('Unsupported collector probe module: ' + key);
    const module = await import(entry.path);
    modules.set(key, { module, original: { ...module }, replacements: {} });
  }
  const active = () => rows[context.getStore()];
  const originalFs = { ...fs }, originalPromises = { ...promises };
  for (const [object, original, operations] of [[fs, originalFs, ['openSync', 'readFileSync', 'open', 'readFile']],
      [promises, originalPromises, ['open', 'readFile']]]) {
    for (const operation of operations) object[operation] = function (...args) {
      const row = active();
      if (row) {
        row.fileReads++;
        if (String(args[0]).replaceAll('\\', '/').endsWith('/.git/config')) row.gitConfigReads++;
      }
      return Reflect.apply(original[operation], this, args);
    };
  }
  const publishFs = () => {
    for (const name of ['fs', 'node:fs']) mock.module(name, () => ({ ...fs, default: fs }));
    for (const name of ['fs/promises', 'node:fs/promises']) mock.module(name, () => ({ ...promises, default: promises }));
  };
  publishFs();
  const originalSpawn = Bun.spawn;
  Bun.spawn = function (...args) {
    const row = active();
    const argv = Array.isArray(args[0]) ? args[0] : args[0]?.cmd;
    if (row && Array.isArray(argv) && argv.includes('--files')) row.fileEnumerations++;
    return Reflect.apply(originalSpawn, this, args);
  };
  const repository = modules.get('projectRepository');
  for (const [role, counter] of [['isGit', 'gitChecks'], ['remote', 'remoteLookups']]) {
    const name = profile.bindings.projectRepository[role];
    repository.replacements[name] = function (...args) {
      const row = active();
      if (row) row[counter]++;
      return Reflect.apply(repository.original[name], this, args);
    };
  }
  for (const [name, key, role] of collectors) {
    const item = modules.get(key), binding = profile.bindings[key][role], row = rows[name];
    const finished = value => {
      row.completed++;
      row.resultTypes.add(value === null ? 'null' : typeof value);
      if (value && typeof value === 'object') for (const field of Object.keys(value)) row.resultFields.add(field);
      return value;
    };
    item.replacements[binding] = function (...args) {
      row.calls++;
      return context.run(name, () => {
        const result = Reflect.apply(item.original[binding], this, args);
        return result && typeof result.then === 'function' ? result.then(finished) : finished(result);
      });
    };
  }
  for (const [key, { module, original, replacements }] of modules) {
    mock.module(profile.modules[key].path, () => ({ ...original, ...replacements }));
    for (const [name, value] of Object.entries(replacements)) if (module[name] !== value) throw Error('Collector observer binding failed');
  }
  return {
    report() { return Object.fromEntries(Object.entries(rows).map(([name, row]) => [name, {
      ...row, resultFields: [...row.resultFields].sort(), resultTypes: [...row.resultTypes].sort(),
    }])); },
    restore() {
      for (const name of ['openSync', 'readFileSync', 'open', 'readFile']) fs[name] = originalFs[name];
      for (const name of ['open', 'readFile']) promises[name] = originalPromises[name];
      publishFs(); Bun.spawn = originalSpawn;
      for (const [key, { original }] of modules) mock.module(profile.modules[key].path, () => original);
    },
  };
}

export async function collectorProbe(profile, protectedMode) {
  const config = profile.test_bindings.config;
  await (await import(config.path))[config.export]();
  const root = await promises.mkdtemp(path.join(process.env.TMPDIR, 'collectors-'));
  const context = await roleModule(profile, 'projectContext');
  const repository = await roleModule(profile, 'projectRepository');
  const fileAnalytics = await roleModule(profile, 'fileAnalytics');
  const permissions = await roleModule(profile, 'permissionAnalytics');
  const accessibility = await roleModule(profile, 'accessibilityAnalytics');
  const facade = await import(profile.modules.repositoryFacade.path);
  const permissionFacade = await import(profile.modules.permissionFacade.path);
  // Minimal valid Git repository, independent of global user configuration.
  for (const directory of ['.git/objects', '.git/refs/heads']) await promises.mkdir(path.join(root, directory), { recursive: true });
  await promises.writeFile(path.join(root, '.git/HEAD'), 'ref: refs/heads/main\n');
  await promises.writeFile(path.join(root, '.git/config'), '[core]\nrepositoryformatversion = 0\nbare = false\n[remote "origin"]\nurl = https://git.fixture.invalid/group/project.git\n');
  for (let i = 0; i < 3; i++) await promises.writeFile(path.join(root, 'fixture-' + i + '.txt'), 'COLLECTOR_FIXTURE\n');
  const observer = await observeCollectors(profile);
  try {
    const value = await context.withDirectory(root, () => repository.analytics());
    const aliasValue = await context.withDirectory(root, () => facade.getGitPresenceForAnalytics());
    if (protectedMode) { assert.equal(value, null); assert.equal(aliasValue, null); }
    else {
      assert.deepEqual(value, { is_git: true, has_remote: true, remote_host_class: 'other' });
      assert.deepEqual(aliasValue, value);
    }
    const count = await context.withDirectory(root, () => fileAnalytics.collect(root, AbortSignal.timeout(5000), ['.git']));
    assert.equal(protectedMode ? count === undefined : typeof count === 'number' && count >= 3, true, 'file-count control');
    const permissionFields = permissions.collect();
    assert.equal(Object.keys(permissionFields).length > 0, !protectedMode);
    assert.deepEqual(permissionFacade.getAutoModeAvailabilityForAnalytics(), permissionFields);
    const accessibilityFields = accessibility.collect();
    assert.deepEqual(Object.keys(accessibilityFields).sort(), protectedMode ? [] : ['screenReaderActivationSource', 'screenReaderMode']);
    // Exercise the same shared capabilities outside the analytics scopes.
    const business = await context.withDirectory(root, async () => ({
      isGit: await repository.isGit(), remoteMatches: await repository.remote() === 'https://git.fixture.invalid/group/project.git',
      permissionState: permissions.enabledState(), permissionAvailabilityType: typeof permissionFacade.isAutoModeGateEnabled(),
      screenReaderEnabled: accessibility.enabled(), screenReaderText: typeof accessibility.modeText() === 'string',
      screenReaderChild: accessibility.childEnvironment().CLAUDE_AX_SCREEN_READER === '1',
    }));
    assert.equal(business.isGit, true); assert.equal(business.remoteMatches, true);
    assert.equal(business.permissionAvailabilityType, 'boolean');
    assert.ok(['enabled', 'disabled', 'opt-in'].includes(business.permissionState));
    assert.equal(business.screenReaderEnabled, true); assert.equal(business.screenReaderText, true); assert.equal(business.screenReaderChild, true);
    const observed = observer.report();
    if (protectedMode) {
      for (const row of Object.values(observed)) {
        assert.equal(row.calls, row.completed);
        for (const field of ['fileReads', 'gitConfigReads', 'fileEnumerations', 'gitChecks', 'remoteLookups']) assert.equal(row[field], 0, field + ' still collected');
        assert.deepEqual(row.resultFields, []);
      }
    } else {
      assert.ok(observed.git.gitChecks > 0 && observed.git.remoteLookups > 0 && observed.git.gitConfigReads > 0, 'Git positive control was not observed');
      assert.ok(observed.files.fileEnumerations > 0, 'File enumeration positive control was not observed');
    }
    return { passed: true, collectors: observed, business };
  } finally { observer.restore(); await promises.rm(root, { recursive: true, force: true }); }
}
