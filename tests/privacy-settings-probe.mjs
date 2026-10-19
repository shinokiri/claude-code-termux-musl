// Deterministically exercise an official settings reload after SDK creation,
// across the await between query-entry cleanup and request-body construction.
// Only the fixture schedule changes; the original settings applier does the work.
import fs from 'node:fs';
import crypto from 'node:crypto';
import { mock } from 'bun:test';
import { roleModule } from '../libexec/termux-privacy-profile.mjs';
const profile = JSON.parse(fs.readFileSync(new URL('../libexec/privacy-profile.json', import.meta.url)));
for (const role of ['requestHeaders', 'requestEnvironment']) {
  const entry = profile.modules[role];
  if (crypto.createHash('sha256').update(fs.readFileSync(entry.path)).digest('hex') !== entry.sha256) {
    throw Error('Unverified settings probe: ' + role);
  }
}
const binding = profile.test_bindings.requestClient;
if (binding.path !== profile.modules.requestHeaders.path) throw Error('Unexpected client probe module');
const clientModule = await import(binding.path), create = clientModule[binding.export];
const settings = await roleModule(profile, 'requestEnvironment');
const apply = process.env.DATAFLOW_SETTINGS_RELOAD === 'safe' ? settings.applySafe : settings.apply;
if (typeof create !== 'function' || typeof apply !== 'function') throw Error('Missing settings probe binding');
const observed = [];
const replacement = async function (...args) {
  const result = await Reflect.apply(create, this, args);
  Reflect.apply(apply, settings, []);
  observed.push({
    diagnostics: Object.hasOwn(JSON.parse(process.env.CLAUDE_CODE_EXTRA_BODY || '{}'), 'diagnostics'),
    rawBodyCapture: process.env.OTEL_LOG_RAW_API_BODIES === 'true',
    promptCapture: process.env.OTEL_LOG_USER_PROMPTS === 'true',
  });
  return result;
};
mock.module(binding.path, () => ({ ...clientModule, [binding.export]: replacement }));
if (clientModule[binding.export] !== replacement) throw Error('Settings probe replacement failed');
process.on('exit', () => fs.writeFileSync(process.env.DATAFLOW_SETTINGS_REPORT, JSON.stringify({
  kind: process.env.DATAFLOW_SETTINGS_RELOAD, reloads: observed.length, observed,
})));
