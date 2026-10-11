// SPDX-License-Identifier: MIT
// Functional project detection only. This replaces a shared collector whose
// other results were used exclusively by the disabled initialization event.
import fs from 'node:fs/promises';
import path from 'node:path';

const gradleFiles = ['settings.gradle', 'settings.gradle.kts', 'build.gradle', 'build.gradle.kts'];
const appleFolders = ['ios', 'iosapp', 'macos', 'apple'];
const androidPlugin = /\bcom\.android\.(?:application|library|test|dynamic-feature|tools\.build:gradle)\b/;

export function createProjectClassifier({ context, repository, markers }) {
  async function list(directory, top = false) {
    try {
      const entries = await fs.readdir(directory, { withFileTypes: true });
      return { directory, files: entries.filter(e => e.isFile()).map(e => e.name),
        folders: entries.filter(e => e.isDirectory()).map(e => e.name) };
    } catch (error) {
      // A disappearing child is empty; unreadable directories are unknown.
      // Failure to enumerate the root invalidates the entire scan.
      if (top) throw error;
      return repository.isMissing(error) ? { directory, files: [], folders: [] } : null;
    }
  }

  async function androidAt(view) {
    if (view.files.includes('AndroidManifest.xml')) return true;
    async function matches(name) {
      const text = await repository.readBounded(path.join(view.directory, name), 65536, { noFollow: true });
      if (typeof text !== 'string') return false;
      return androidPlugin.test(text.replace(/^[^\S\n]*(?:#|\/\/).*$/gm, ''));
    }
    for (const name of gradleFiles) {
      if (view.files.includes(name) && await matches(name)) return true;
    }
    if (view.folders.includes('gradle')) {
      const catalog = 'gradle/libs.versions.toml';
      if (await fs.lstat(path.join(view.directory, catalog)).then(s => s.isFile(), () => false)) return matches(catalog);
    }
    return false;
  }

  async function android(root) {
    if (await androidAt(root)) return true;
    const nested = root.folders.find(name => name.toLowerCase() === 'android');
    if (!nested) return false;
    const view = await list(path.join(root.directory, nested));
    return view !== null && await androidAt(view);
  }

  async function ios(root) {
    const children = appleFolders.flatMap(label => {
      const folder = root.folders.find(name => name.toLowerCase() === label);
      return folder ? [path.join(root.directory, folder)] : [];
    });
    const views = [...await Promise.all(children.map(directory => list(directory))), root];
    let unknown = views.includes(null);
    const projects = views.filter(Boolean).flatMap(view => view.folders
      .filter(name => name.toLowerCase().endsWith('.xcodeproj')).sort()
      .map(name => path.join(view.directory, name))).slice(0, 5);
    for (const directory of projects) {
      const view = await list(directory);
      if (view === null) { unknown = true; continue; }
      const project = view.files.find(name => name.toLowerCase() === 'project.pbxproj');
      if (!project) continue;
      const text = await repository.readBounded(path.join(directory, project), 1048576, { noFollow: true, fromTail: true });
      if (text === 'unreadable') { unknown = true; continue; }
      if (text === null) continue;
      // Reuse the verified application's project-settings evidence parser.
      // This envelope selects its pure text branch: no tool executes, no
      // message is queued, and no conversation or telemetry state is used.
      const evidence = markers.newEvidence();
      markers.observeEvidence(evidence, [{ type: 'user', message: {
        content: [{ type: 'tool_result', content: text.content }],
      } }]);
      if (markers.getEvidence(evidence) !== null) return true;
    }
    return unknown ? null : false;
  }

  async function scan(directory) {
    try {
      const root = await list(directory, true);
      const [has_ios_app_project, has_android_app_project] = await Promise.all([ios(root), android(root)]);
      // No registry, secret-manager, CI, lockfile or toolchain fields are
      // constructed, even when the disabled event's caller asks for them.
      return { has_ios_app_project, has_android_app_project };
    } catch { return null; }
  }

  return async function classify(options) {
    try {
      if (context.isRemote()) return null;
      const directory = repository.root(context.cwd());
      if (directory === null || await repository.isHome(directory)) return null;
      // Retain the original host-scoped LRU and its invalidation lifecycle.
      // In particular, an older failed scan must not evict a newer fresh one.
      const cache = repository.state().markersByRoot;
      if (options?.fresh === true) cache.delete(directory);
      const pending = repository.cached(cache, directory, scan);
      const value = await pending;
      if ((value === null || value.has_ios_app_project === null) && cache.peek(directory) === pending) cache.delete(directory);
      return value;
    } catch { return null; }
  };
}
