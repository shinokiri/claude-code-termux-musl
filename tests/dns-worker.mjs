import {parentPort} from 'node:worker_threads';
import {promises as dns} from 'node:dns';
import {dns as bunDNS} from 'bun';
const txt = await dns.resolveTxt('worker-txt.dns-test.invalid');
const records = await new dns.Resolver().resolveMx('worker-mx.dns-test.invalid');
const addresses = await bunDNS.lookup('worker-lookup.dns-test.invalid');
const result = {txt: txt[0][0] === 'first', resolver: records[0].exchange === 'mail.example.invalid',
  lookup: addresses.some(item => item.address === '203.0.113.7')};
if (parentPort) parentPort.postMessage(result);
else postMessage(result);
