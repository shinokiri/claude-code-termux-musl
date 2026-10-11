// A fresh process per API avoids interpreting another client's DNS cache hit.
import fs from 'node:fs';
import https from 'node:https';
const kind = process.env.NATIVE_NETWORK_CASE;
const port = process.env.DNS_TLS_PORT;
const good = 'https://claude-network.invalid:' + port + '/';
const report = {kind};
try {
  if (kind === 'fetch') {
    const response = await fetch(good, {proxy: false, signal: AbortSignal.timeout(5000)});
    await response.text(); report.status = response.status;
  } else if (kind === 'node-https') {
    report.status = await new Promise((resolve, reject) => {
      const request = https.get(good, response => {
        response.resume(); response.on('end', () => resolve(response.statusCode));
      });
      request.on('error', reject);
    });
  } else if (kind === 'wss') {
    report.connected = await new Promise((resolve, reject) => {
      const socket = new WebSocket('wss://claude-network.invalid:' + port + '/');
      const timer = setTimeout(() => reject(Error('WSS timeout')), 5000);
      socket.onopen = () => { clearTimeout(timer); socket.close(); resolve(true); };
      socket.onerror = event => { clearTimeout(timer); reject(Error(event.message)); };
    });
  } else if (kind === 'wrong-certificate') {
    try {
      await fetch('https://wrong-certificate.dns-audit.invalid:' + port + '/', {proxy: false, signal: AbortSignal.timeout(5000)});
      report.rejected = false;
    } catch (error) {
      report.rejected = error.code === 'ERR_TLS_CERT_ALTNAME_INVALID' || /certificate|altname|hostname/i.test(String(error));
    }
  } else throw Error('Explicit synthetic API case required');
  report.proxyEnvironment = Object.keys(process.env).filter(key => /^(https?|all)_proxy$/i.test(key));
  fs.writeFileSync(process.env.DNS_TEST_REPORT, JSON.stringify(report));
  process.exit(0);
} catch (error) {
  process.stderr.write(String(error) + '\n'); process.exit(1);
}
