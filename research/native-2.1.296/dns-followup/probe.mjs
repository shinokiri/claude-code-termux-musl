// Isolated native API fixtures; exits before the Claude application starts.
import { writeFileSync } from 'node:fs';

const kind = process.env.NATIVE_FOLLOWUP_CASE;
const hostname = `${kind}.native-followup.invalid`;
const report = { kind };
let resource;
try {
  if (kind.startsWith('lookup-')) {
    report.addresses = await Bun.dns.lookup(hostname, {
      backend: kind.slice('lookup-'.length), family: 4,
    });
    if (!report.addresses.length || report.addresses.some(row => row.address !== '127.0.0.1')) {
      throw new Error('Unexpected fixture address');
    }
  } else if (kind === 'tcp-listen') {
    resource = Bun.listen({ hostname, port: 0, socket: { data() {} } });
    report.port = resource.port;
    if (!(report.port > 0)) throw new Error('Listener did not acquire a local port');
  } else if (kind === 'udp-bind' || kind === 'udp-connect') {
    const options = { hostname: kind === 'udp-bind' ? hostname : '127.0.0.1', port: 0 };
    if (kind === 'udp-connect') {
      options.connect = { hostname, port: Number(process.env.NATIVE_FOLLOWUP_UDP_PORT) };
    }
    resource = await Bun.udpSocket(options);
    report.port = resource.port;
    const payload = `native-dns-fixture:${kind}`;
    report.sent = kind === 'udp-connect'
      ? resource.send(payload)
      : resource.send(payload, Number(process.env.NATIVE_FOLLOWUP_UDP_PORT), '127.0.0.1');
    if (!report.sent || !(report.port > 0)) throw new Error('Local UDP operation failed');
  } else if (kind === 'cluster-raw-bind') {
    const { default: cluster } = await import('node:cluster');
    const { EventEmitter } = await import('node:events');
    if (process.env.NATIVE_FOLLOWUP_CLUSTER_CHILD === '1') {
      if (!cluster.isWorker) throw new Error('Expected an isolated cluster worker');
      const handle = await new Promise((resolve, reject) => {
        cluster._getServer(new EventEmitter(), {
          address: hostname, addressType: 4, port: 0, fd: -1, flags: 0, sharedOnly: true,
        }, (error, received) => error ? reject(new Error(`cluster bind error ${error}`)) : resolve(received));
      });
      const handleReceived = Number.isInteger(handle?.sharedFd) && handle.sharedFd >= 0;
      await new Promise(resolve => handle.close(resolve));
      await new Promise((resolve, reject) => process.send({ nativeFixture: kind, handleReceived },
        error => error ? reject(error) : resolve()));
      process.disconnect();
      process.exit(handleReceived ? 0 : 1);
    }
    if (!cluster.isPrimary) throw new Error('Expected isolated cluster primary');
    cluster.schedulingPolicy = cluster.SCHED_NONE;
    cluster.setupPrimary({ exec: process.execPath, args: ['--version'], execArgv: [], silent: true });
    const worker = cluster.fork({ NATIVE_FOLLOWUP_CLUSTER_CHILD: '1' });
    report.workerStderr = '';
    worker.process.stderr?.on('data', data => { report.workerStderr += String(data); });
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { worker.kill(); reject(new Error('Fixture worker did not complete')); }, 10000);
      worker.on('message', message => {
        if (message?.nativeFixture === kind) report.handleReceived = message.handleReceived;
      });
      worker.once('error', error => { clearTimeout(timer); reject(error); });
      worker.once('exit', (code, signal) => {
        clearTimeout(timer);
        report.workerExit = code;
        report.workerSignal = signal;
        code === 0 && report.handleReceived ? resolve() : reject(new Error('Fixture worker failed'));
      });
    });
    report.remainingWorkers = Object.keys(cluster.workers).length;
    if (report.remainingWorkers !== 0) throw new Error('Fixture worker was not reclaimed');
  } else {
    throw new Error('Unknown fixture case');
  }
  report.passed = true;
} catch (error) {
  report.passed = false;
  report.error = String(error?.stack || error);
} finally {
  if (resource) {
    if (kind === 'tcp-listen') resource.stop(true);
    else resource.close();
  }
  writeFileSync(process.env.NATIVE_FOLLOWUP_REPORT, JSON.stringify(report, null, 2) + '\n');
}
process.exit(report.passed ? 0 : 1);
