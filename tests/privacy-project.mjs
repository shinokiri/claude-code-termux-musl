// Compare the real bundled project collector with the functional replacement.
// Only synthetic files are read; the filesystem observer counts attempts,
// independently of the collector's bounded-reader interface.
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import { mock } from 'bun:test';
import { roleModule } from '../libexec/termux-privacy-profile.mjs';

export async function projectProbe(profile, protectedMode) {
  const context = await roleModule(profile, 'projectContext');
  const repository = await roleModule(profile, 'projectRepository');
  const markers = await roleModule(profile, 'projectMarkers');
  const rootModule = await import(profile.modules.projectRepository.path);
  const rootExport = profile.bindings.projectRepository.root;
  const homeExport = profile.bindings.projectRepository.isHome;
  const originalRoot = rootModule[rootExport];
  const originalHome = rootModule[homeExport];
  const directory = await fs.mkdtemp(path.join(process.env.TMPDIR, 'project-'));
  const originalFs = { ...fs };
  const originalRemote = context.isRemote;
  let active, fault, remote = false;
  const observations = [];
  for (const operation of ['open', 'readFile', 'readdir', 'lstat']) {
    fs[operation] = async function (filename, ...args) {
      const name = String(filename);
      if (active && (name === active.root || name.startsWith(active.root + '/'))) {
        const relative = path.relative(active.root, name);
        active.operations.push({ operation, path: relative });
        if (fault?.operation === operation && relative === fault.path) {
          throw Object.assign(Error('Synthetic project fixture failure'), { code: fault.code });
        }
      }
      return originalFs[operation](filename, ...args);
    };
  }
  const publishFs = () => {
    for (const name of ['node:fs/promises', 'fs/promises']) mock.module(name, () => ({ ...fs, default: fs }));
  };
  publishFs();
  const remoteModule = await import(profile.modules.projectContext.path);
  const remoteExport = profile.bindings.projectContext.isRemote;
  mock.module(profile.modules.projectContext.path, () => ({ ...remoteModule, [remoteExport]: () => remote || originalRemote() }));
  const privateFixtures = {
    '.npmrc': 'registry=https://registry.fixture.invalid/\n',
    '.envrc': 'export VAULT_ADDR=https://fixture.invalid/\n',
    'pipeline.yaml': 'run: op read op://fixture/item\n',
    'CMakeLists.txt': 'enable_language(CUDA)\n',
    '.github/workflows/ci.yml': 'name: fixture\n',
  };
  let serial = 0;
  async function fixture(files = {}, links = {}) {
    const root = path.join(directory, String(++serial));
    await fs.mkdir(path.join(root, '.git'), { recursive: true });
    for (const [name, content] of Object.entries({ ...privateFixtures, ...files })) {
      await fs.mkdir(path.dirname(path.join(root, name)), { recursive: true });
      await fs.writeFile(path.join(root, name), content);
    }
    for (const [name, target] of Object.entries(links)) {
      await fs.mkdir(path.dirname(path.join(root, name)), { recursive: true });
      await fs.symlink(target, path.join(root, name));
    }
    return root;
  }
  async function scan(name, root, expected, options = { fresh: true }, cwd = root) {
    active = { name, root, operations: [] };
    let value;
    try { value = await context.withDirectory(cwd, () => markers.collect(options)); }
    finally { observations.push(active); active = null; }
    if (expected === null) assert.equal(value, null, name);
    else {
      assert.ok(value, name + ' returned no result');
      assert.deepEqual([value.has_ios_app_project, value.has_android_app_project], expected, name);
      if (protectedMode) assert.deepEqual(Object.keys(value).sort(), ['has_android_app_project', 'has_ios_app_project'], name + ' collected extra fields');
      else assert.equal(Object.keys(value).length, 16, name + ' positive control lost original fields');
    }
    const operations = observations.at(-1).operations;
    if (protectedMode) {
      for (const operation of operations) {
        assert.ok(!Object.keys(privateFixtures).includes(operation.path), name + ' touched telemetry-only file: ' + operation.path);
        assert.ok(!operation.path.startsWith('.github'), name + ' inspected CI directory');
      }
      for (const { operation, path: name } of operations) {
        if (operation !== 'open' && operation !== 'readFile') continue;
        assert.ok(/(?:^|\/)(?:(?:settings|build)\.gradle(?:\.kts)?|libs\.versions\.toml|project\.pbxproj)$/i.test(name), 'unexpected project content read: ' + name);
      }
    }
    return value;
  }
  try {
    const empty = await fixture();
    await scan('telemetry-only', empty, [false, false]);
    const positive = observations.at(-1).operations;
    for (const name of ['.npmrc', '.envrc', 'CMakeLists.txt']) {
      assert.equal(positive.some(op => op.operation === 'open' && op.path === name), !protectedMode, name + ' read control');
    }
    await scan('android-manifest', await fixture({ 'AndroidManifest.xml': '<manifest/>' }), [false, true]);
    for (const filename of ['settings.gradle', 'settings.gradle.kts', 'build.gradle', 'build.gradle.kts', 'gradle/libs.versions.toml']) {
      await scan('android-' + filename, await fixture({ [filename]: 'id("com.android.application")' }), [false, true]);
    }
    for (const plugin of ['library', 'test', 'dynamic-feature', 'tools.build:gradle']) {
      await scan('android-plugin-' + plugin, await fixture({ 'build.gradle': 'com.android.' + plugin }), [false, true]);
    }
    await scan('android-comment', await fixture({ 'build.gradle': '// com.android.application\n # com.android.library\norg.jetbrains.kotlin' }), [false, false]);
    await scan('android-line-separator', await fixture({ 'build.gradle': '// comment\u2028com.android.application' }), [false, true]);
    await scan('android-nested', await fixture({ 'AnDrOiD/build.gradle.kts': 'id("com.android.application")' }), [false, true]);
    await scan('android-directory-link', await fixture({ 'ignored/AndroidManifest.xml': '<manifest/>' }, { android: 'ignored' }), [false, false]);
    await scan('android-file-link', await fixture({ 'ignored.gradle': 'com.android.application' }, { 'build.gradle': 'ignored.gradle' }), [false, false]);
    await scan('android-catalog-link', await fixture({ 'ignored.toml': 'com.android.application' }, { 'gradle/libs.versions.toml': '../ignored.toml' }), [false, false]);
    for (const [label, text, expected] of [
      ['sdk', 'SDKROOT = iphoneos;', true], ['simulator', 'SDKROOT = iphonesimulator17.4;', true],
      ['conditional', '"SDKROOT[sdk=iphoneos*]" = "iphoneos";', true],
      ['devices', 'TARGETED_DEVICE_FAMILY = "1,2";', true],
      ['platforms', 'SUPPORTED_PLATFORMS = "iphoneos iphonesimulator";', true],
      ['mac', 'SDKROOT = macosx;', false], ['watch', 'SDKROOT = watchos; TARGETED_DEVICE_FAMILY = 4;', false],
      ['loose', 'IPHONEOS_DEPLOYMENT_TARGET = 17;', false], ['swift-text', 'import UIKit', false],
    ]) {
      await scan('ios-' + label, await fixture({ 'App.xcodeproj/project.pbxproj': text }), [expected, false]);
    }
    for (const folder of ['iOS', 'iosapp', 'macos', 'apple']) {
      await scan('ios-folder-' + folder, await fixture({ [folder + '/App.XCODEPROJ/PROJECT.PBXPROJ']: 'SDKROOT = iphoneos;' }), [true, false]);
    }
    await scan('ios-file-link', await fixture({ 'ignored': 'SDKROOT = iphoneos;' }, { 'App.xcodeproj/project.pbxproj': '../ignored' }), [false, false]);
    const fiveProjects = Object.fromEntries(Array.from({ length: 6 }, (_, i) => [`${i}.xcodeproj/project.pbxproj`, i === 5 ? 'SDKROOT = iphoneos;' : 'SDKROOT = macosx;']));
    await scan('ios-project-limit', await fixture(fiveProjects), [false, false]);
    await scan('ios-tail-read', await fixture({ 'App.xcodeproj/project.pbxproj': 'x'.repeat(1100000) + '\nSDKROOT = iphoneos;\n' }), [true, false]);
    await scan('android-read-limit', await fixture({ 'build.gradle': 'x'.repeat(66000) + '\ncom.android.application' }), [false, false]);
    const changing = await fixture();
    await scan('cache-initial', changing, [false, false]);
    await fs.writeFile(path.join(changing, 'AndroidManifest.xml'), '<manifest/>');
    await scan('cache-reuse', changing, [false, false], {});
    assert.equal(observations.at(-1).operations.length, 0, 'cached result still scanned files');
    await scan('cache-fresh', changing, [false, true]);
    await scan('directory-switch', empty, [false, false], {});
    await fs.mkdir(path.join(changing, 'src'));
    await scan('nested-current-directory', changing, [false, true], {}, path.join(changing, 'src'));
    for (const [label, files, operation, filename, expected] of [
      ['root-failure', {}, 'readdir', '', null],
      ['ios-directory-failure', { 'ios/App.xcodeproj/project.pbxproj': 'SDKROOT = iphoneos;' }, 'readdir', 'ios', [null, false]],
      ['ios-read-failure', { 'App.xcodeproj/project.pbxproj': 'SDKROOT = iphoneos;' }, 'open', 'App.xcodeproj/project.pbxproj', [null, false]],
    ]) {
      const root = await fixture(files);
      fault = { operation, path: filename, code: 'EACCES' };
      await scan(label, root, expected);
      fault = null;
      await scan(label + '-retry', root, [label !== 'root-failure', false], {});
      assert.ok(observations.at(-1).operations.length, 'unknown result was cached');
    }
    const noRepository = path.join(directory, 'no-repository');
    await fs.mkdir(noRepository);
    // The test workspace may itself live inside the adapter's repository.
    mock.module(profile.modules.projectRepository.path, () => ({ ...rootModule, [rootExport]: () => null }));
    await scan('no-repository', noRepository, null);
    mock.module(profile.modules.projectRepository.path, () => ({ ...rootModule, [rootExport]: originalRoot }));
    remote = true;
    await scan('remote-mode', changing, null);
    assert.equal(observations.at(-1).operations.length, 0, 'remote mode scanned local files');
    remote = false;
    mock.module(profile.modules.projectRepository.path, () => ({ ...rootModule, [homeExport]: async () => true }));
    await scan('home-repository', changing, null);
    assert.equal(observations.at(-1).operations.length, 0, 'home repository scanned files');
    mock.module(profile.modules.projectRepository.path, () => ({ ...rootModule, [homeExport]: originalHome }));
    return { fixtures: observations.length, passed: true,
      results: observations.map(item => ({ name: item.name, contentReads: item.operations.filter(op => ['open', 'readFile'].includes(op.operation)).map(op => op.path) })) };
  } finally {
    active = null;
    for (const operation of ['open', 'readFile', 'readdir', 'lstat']) fs[operation] = originalFs[operation];
    publishFs();
    mock.module(profile.modules.projectContext.path, () => ({ ...remoteModule, [remoteExport]: originalRemote }));
    mock.module(profile.modules.projectRepository.path, () => ({ ...rootModule, [rootExport]: originalRoot, [homeExport]: originalHome }));
    repository.state().markersByRoot.clear();
    await fs.rm(directory, { recursive: true, force: true });
  }
}
