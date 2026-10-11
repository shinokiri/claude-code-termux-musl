// Exercise the original complete-response query entry using the normal CLI
// request. Its implementation still consumes the same model/tool conversation.
import fs from 'node:fs';
import crypto from 'node:crypto';
import { mock } from 'bun:test';
const profile = JSON.parse(fs.readFileSync(new URL('../libexec/privacy-profile.json', import.meta.url)));
const entry = profile.modules.contextMetrics, bindings = profile.bindings.contextMetrics;
if (crypto.createHash('sha256').update(fs.readFileSync(entry.path)).digest('hex') !== entry.sha256) throw Error('Unverified query probe');
const module = await import(entry.path), once = module[bindings.queryOnce];
let calls = 0;
if (process.env.DATAFLOW_QUERY_ONCE === '1') {
  const replacement = async function* (...args) { calls++; yield await Reflect.apply(once, this, args); };
  mock.module(entry.path, () => ({ ...module, [bindings.queryStream]: replacement }));
  if (module[bindings.queryStream] !== replacement) throw Error('Query entry replacement failed');
}
process.on('exit', () => fs.writeFileSync(process.env.DATAFLOW_QUERY_REPORT, JSON.stringify({ onceCalls: calls })));
