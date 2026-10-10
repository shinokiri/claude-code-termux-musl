import fs from 'node:fs';
import dns from 'node:dns';
import {promises as pdns, resolveTxt as namedResolveTxt} from 'node:dns';
import {Worker as NodeWorker, isMainThread} from 'node:worker_threads';
import {dns as bunDNS} from 'bun';

if (isMainThread) {
  const report = {records: {}, errors: {}, workers: []};
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const callback = fn => new Promise((resolve, reject) => fn((error, value) => error ? reject(error) : resolve(value)));
  const types = ['A', 'AAAA', 'ANY', 'CAA', 'CNAME', 'MX', 'NAPTR', 'NS', 'PTR', 'SOA', 'SRV', 'TXT'];
  const names = {A: 'resolve4', AAAA: 'resolve6', ANY: 'resolveAny', CAA: 'resolveCaa', CNAME: 'resolveCname', MX: 'resolveMx', NAPTR: 'resolveNaptr', NS: 'resolveNs', PTR: 'resolvePtr', SOA: 'resolveSoa', SRV: 'resolveSrv', TXT: 'resolveTxt'};
  const expected = {
    A: ['203.0.113.7'], AAAA: ['2001:db8::7'],
    CAA: [{critical: 0, issue: 'ca.example.invalid'}],
    CNAME: ['target.example.invalid'], MX: [{exchange: 'mail.example.invalid', priority: 10}],
    NAPTR: [{flags: 'S', service: 'SIP+D2U', regexp: '', replacement: '_sip._udp.example.invalid', order: 100, preference: 10}],
    NS: ['ns.example.invalid'], PTR: ['ptr.example.invalid'],
    SOA: {nsname: 'ns.example.invalid', hostmaster: 'hostmaster.example.invalid', serial: 7, refresh: 3600, retry: 600, expire: 86400, minttl: 60},
    SRV: [{name: 'service.example.invalid', port: 8443, priority: 10, weight: 20}],
    TXT: [['first', 'second']],
  };
  const canonical = value => JSON.stringify(value, (key, item) => item && !Array.isArray(item) && typeof item === 'object'
    ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a.localeCompare(b))) : item);
  const callbackResolver = new dns.Resolver({timeout: 1000, tries: 1});
  const promiseResolver = new pdns.Resolver({timeout: 1000, tries: 1});
  // Compare TXT chunk grouping to this executable's unmodified parser through
  // an explicit UDP resolver; Bun versions differ from Node in this detail.
  const nativeControl = new pdns.Resolver({timeout: 100, tries: 1});
  nativeControl.setServers(['127.0.0.1:' + process.env.DNS_TEST_CUSTOM_PORT]);
  expected.TXT = await nativeControl.resolveTxt('baseline-native-txt.dns-test.invalid');
  report.nativeTxtControl = expected.TXT;
  for (const type of types) {
    const method = names[type];
    const variants = {
      callback: () => callback(done => dns[method]('callback-' + type + '.dns-test.invalid', done)),
      promise: () => pdns[method]('promise-' + type + '.dns-test.invalid'),
      resolverCallback: () => callback(done => callbackResolver[method]('resolver-cb-' + type + '.dns-test.invalid', done)),
      resolverPromise: () => promiseResolver[method]('resolver-p-' + type + '.dns-test.invalid'),
      generic: () => pdns.resolve('generic-' + type + '.dns-test.invalid', type),
    };
    report.records[type] = {};
    for (const [variant, run] of Object.entries(variants)) {
      const value = await run();
      if (type === 'ANY') {
        check(Array.isArray(value) && value.some(item => item.type === 'A') && value.some(item => item.type === 'TXT'), 'ANY shape ' + variant);
      } else check(canonical(value) === canonical(expected[type]), type + '/' + variant + ': ' + JSON.stringify(value));
      report.records[type][variant] = true;
    }
  }
  const ttl = await pdns.resolve4('ttl.dns-test.invalid', {ttl: true});
  check(ttl[0].address === '203.0.113.7' && ttl[0].ttl === 73, 'TTL was not preserved');
  report.ttl = ttl;
  report.namedImport = await callback(done => namedResolveTxt('named.dns-test.invalid', done));
  report.reverse = await pdns.reverse('203.0.113.7');
  check(report.reverse[0] === 'ptr.example.invalid', 'reverse DNS');
  report.lookupService = await pdns.lookupService('203.0.113.7', 443);
  check(report.lookupService.hostname === 'ptr.example.invalid' && ['https', '443'].includes(report.lookupService.service), 'lookupService: ' + JSON.stringify(report.lookupService));
  report.bunLookup = await bunDNS.lookup('bun-lookup.dns-test.invalid');
  check(report.bunLookup.some(item => item.address === '203.0.113.7'), 'Bun.dns.lookup');
  report.bunTxt = await bunDNS.resolveTxt('bun-txt.dns-test.invalid');
  check(canonical(report.bunTxt) === canonical(expected.TXT), 'Bun.dns.resolveTxt');
  for (const [name, codes] of [['missing', ['ENOTFOUND']], ['empty', ['ENODATA', 'ENOTFOUND']], ['failure', ['ESERVFAIL']], ['refused', ['EREFUSED']]]) {
    const control = await nativeControl.resolveTxt(name + '.dns-test.invalid').catch(error => error);
    check(codes.includes(control.code), 'native error control: ' + control);
    try { await new pdns.Resolver({timeout: 100, tries: 1}).resolveTxt(name + '.dns-test.invalid'); throw new Error('Unexpected success'); }
    catch (error) { check(error.code === control.code, name + ': ' + error); report.errors[name] = error.code; }
  }
  const timed = new pdns.Resolver({timeout: 80, tries: 1});
  try { await timed.resolveTxt('timeout.dns-test.invalid'); throw new Error('Unexpected timeout success'); }
  catch (error) { check(error.code === 'ETIMEOUT', 'timeout: ' + error); report.errors.timeout = error.code; }
  const cancelled = new pdns.Resolver({timeout: 5000, tries: 1});
  const pending = cancelled.resolveTxt('cancel.dns-test.invalid').catch(error => error);
  cancelled.cancel();
  const error = await pending;
  check(error.code === 'ECANCELLED', 'cancel: ' + error);
  report.errors.cancel = error.code;
  const start = performance.now();
  await Promise.all(Array.from({length: 64}, (_, i) => pdns.resolveTxt('parallel-' + i + '.dns-test.invalid')));
  report.concurrentQueries = 64;
  report.concurrentMilliseconds = Math.round(performance.now() - start);
  const large = await pdns.resolveTxt('large.dns-test.invalid');
  check(large.length === 48 && large.every(item => item[0].length === 240), 'large TCP response');
  report.largeRecords = large.length;
  const largeBatch = await Promise.all(Array.from({length: 24}, () => pdns.resolveTxt('large.dns-test.invalid')));
  check(largeBatch.every(items => items.length === 48 && items.every(item => item[0].length === 240)), 'concurrent large TCP responses');
  report.concurrentLargeResponses = largeBatch.length;
  report.fragmented = await pdns.resolveTxt('fragmented.dns-test.invalid');
  check(canonical(report.fragmented) === canonical(expected.TXT), 'fragmented IPC');
  const custom = new pdns.Resolver();
  custom.setLocalAddress('127.0.0.1');
  custom.setServers(['127.0.0.1:' + process.env.DNS_TEST_CUSTOM_PORT]);
  report.customResolver = await custom.resolve4('custom.dns-test.invalid');
  check(report.customResolver[0] === '192.0.2.55', 'explicit Resolver server changed');
  const servers = dns.getServers();
  dns.setServers(['127.0.0.1:' + process.env.DNS_TEST_CUSTOM_PORT]);
  report.customGlobal = await pdns.resolve4('custom-global.dns-test.invalid');
  check(report.customGlobal[0] === '192.0.2.55', 'explicit global servers changed');
  dns.setServers(servers);
  for (const Constructor of [Worker, NodeWorker]) {
    for (const explicit of [false, true]) {
      report.workers.push(await new Promise((resolve, reject) => {
        const worker = new Constructor(new URL('./dns-worker.mjs', import.meta.url), explicit ? {env: {}} : {});
        const timer = setTimeout(() => {worker.terminate(); reject(new Error('Worker DNS timeout'));}, 5000);
        const finish = data => {clearTimeout(timer); worker.terminate(); resolve({type: Constructor === Worker ? 'Bun' : 'Node', explicitEnv: explicit, ...data});};
        if (Constructor === NodeWorker) {worker.once('message', finish); worker.once('error', reject);}
        else {worker.onmessage = event => finish(event.data); worker.onerror = event => reject(new Error(event.message));}
      }));
    }
  }
  check(report.workers.every(item => item.txt && item.resolver && item.lookup), 'worker record resolver coverage');
  check(!process.env.LD_PRELOAD, 'musl preload leaked into child environment');
  report.loaderEnvironmentCleared = true;
  fs.writeFileSync(process.env.DNS_TEST_REPORT, JSON.stringify(report, null, 2));
  process.exit(0);
}
