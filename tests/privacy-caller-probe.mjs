// Auxiliary observation only: record payload field names already constructed
// before the disabled event bus is called. Delegate to the installed policy.
// No payload values, user identifiers, event names or source text are retained.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { AsyncLocalStorage } from 'node:async_hooks';
import { mock } from 'bun:test';

const state = globalThis[Symbol.for('claude-termux.privacy.v1')];
if (!state?.ready || state.mode !== 'optional-off') throw Error('Caller audit requires active optional-off');
const profile = JSON.parse(fs.readFileSync(process.env.DATAFLOW_PROFILE, 'utf8'));
const audit = JSON.parse(fs.readFileSync(new URL('./privacy-dataflow-profile.json', import.meta.url), 'utf8'));
if (audit.original_sha256 !== profile.original_sha256) throw Error('Unverified caller audit version');
const entry = profile.modules.events;
const bus = await import(entry.path);
const functions = [profile.bindings.events.emit, profile.bindings.events.emitAsync];
const observed = { calls: 0, callsWithFields: 0, fieldNames: new Set() };
let developmentIntentSignals = 0;
const replacements = {};
for (const name of functions) {
  const original = bus[name];
  replacements[name] = function (...args) {
    observed.calls++;
    if (args[1] && typeof args[1] === 'object') {
      const fields = Object.keys(args[1]);
      if (fields.length) observed.callsWithFields++;
      for (const field of fields) observed.fieldNames.add(field);
    }
    return Reflect.apply(original, this, args);
  };
}
mock.module(entry.path, () => ({ ...bus, ...replacements }));
for (const [name, replacement] of Object.entries(replacements)) {
  if (bus[name] !== replacement) throw Error('Caller audit binding was not replaced');
}
// This queue also carries task notifications. Observe only the development
// intent subtype and preserve every original queue operation.
const core = await import(profile.modules.core.path);
const queueExport = audit.system_queue_export;
const enqueue = core[queueExport];
if (typeof enqueue !== 'function') throw Error('Unsupported system-message queue');
const observeEnqueue = function (...args) {
  if (args[0]?.type === 'system' && args[0]?.subtype === 'dev_intent') developmentIntentSignals++;
  return Reflect.apply(enqueue, this, args);
};
mock.module(profile.modules.core.path, () => ({ ...core, [queueExport]: observeEnqueue }));
if (core[queueExport] !== observeEnqueue) throw Error('System-message observer binding was not replaced');
// Attribute reads to the real project-marker collector, independently of
// normal Read/Bash tools and concurrent startup activity. Read contents are
// neither inspected nor retained by the observer.
for (const entry of Object.values(audit.modules)) {
  if (crypto.createHash('sha256').update(fs.readFileSync(entry.path)).digest('hex') !== entry.sha256) {
    throw Error('Unverified caller audit module');
  }
}
const context = new AsyncLocalStorage();
const project = { calls: 0, boundedReads: [], resultFields: new Set() };
const readerEntry = audit.modules.boundedRead;
const reader = await import(readerEntry.path);
const read = reader[readerEntry.export];
const observedRead = async function (...args) {
  const value = await Reflect.apply(read, this, args);
  if (context.getStore() === 'project-markers') {
    const basename = path.basename(String(args[0]));
    project.boundedReads.push({ fixture: ['.npmrc', '.envrc'].includes(basename) ? basename : 'other',
      returnedText: typeof value === 'string', byteLimit: args[1] });
  }
  return value;
};
mock.module(readerEntry.path, () => ({ ...reader, [readerEntry.export]: observedRead }));
const markerEntry = audit.modules.projectMarkers;
const markers = await import(markerEntry.path);
const collect = markers[markerEntry.export];
const observedCollect = async function (...args) {
  project.calls++;
  if (process.env.DATAFLOW_STOP_PROJECT_MARKERS === '1') return null;
  const value = await context.run('project-markers', () => Reflect.apply(collect, this, args));
  if (value) for (const field of Object.keys(value)) project.resultFields.add(field);
  return value;
};
mock.module(markerEntry.path, () => ({ ...markers, [markerEntry.export]: observedCollect }));
if (reader[readerEntry.export] !== observedRead || markers[markerEntry.export] !== observedCollect) {
  throw Error('Project observer binding was not replaced');
}
process.on('exit', () => fs.writeFileSync(process.env.DATAFLOW_CALLER_REPORT, JSON.stringify({
  calls: observed.calls, callsWithFields: observed.callsWithFields, fieldNames: [...observed.fieldNames].sort(),
  developmentIntentSignals,
  projectMarkers: { calls: project.calls, stoppedInFixture: process.env.DATAFLOW_STOP_PROJECT_MARKERS === '1',
    boundedReads: project.boundedReads, resultFields: [...project.resultFields].sort() },
  scope: 'Top-level fields at the disabled event bus; does not attribute prior reads or inspect values.'
})));
