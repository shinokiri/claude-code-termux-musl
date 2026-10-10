const require = (await import('node:module')).createRequire(import.meta.url);
const fs = require('node:fs');
const https = require('node:https');
const port = process.env.DNS_TLS_PORT;
await (async () => {
  const report = {};
  const response = await fetch('https://claude-network.invalid:' + port + '/', {proxy: false, signal: AbortSignal.timeout(5000)});
  await response.text();
  report.fetchStatus = response.status;
  report.nodeHttpsStatus = await new Promise((resolve, reject) => {
    const request = https.get('https://claude-network.invalid:' + port + '/', response => {
      response.resume(); response.on('end', () => resolve(response.statusCode));
    });
    request.on('error', reject);
  });
  try {
    await fetch('https://wrong-certificate.dns-audit.invalid:' + port + '/', {proxy: false, signal: AbortSignal.timeout(5000)});
    report.wrongCertificateRejected = false;
  } catch (error) {
    report.wrongCertificateRejected = /certificate|altname|hostname/i.test(String(error)) || error.code === 'ERR_TLS_CERT_ALTNAME_INVALID';
    report.wrongCertificateError = {code: error.code, message: error.message};
  }
  report.wss = await new Promise((resolve, reject) => {
    const socket = new WebSocket('wss://claude-network.invalid:' + port + '/');
    const timer = setTimeout(() => reject(new Error('WSS timeout')), 5000);
    socket.onopen = () => { clearTimeout(timer); socket.close(); resolve('connected'); };
    socket.onerror = event => { clearTimeout(timer); reject(new Error(event.message)); };
  });
  report.proxyEnvironment = Object.keys(process.env).filter(key => /^(https?|all)_proxy$/i.test(key));
  fs.writeFileSync(process.env.DNS_TEST_REPORT, JSON.stringify(report, null, 2));
  process.exit(0);
})().catch(error => {process.stderr.write(String(error) + '\n'); process.exit(1);});
