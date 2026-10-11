// Local HTTP/2 receiver; records paths and byte counts, never payloads/headers.
const http2 = require('node:http2');
const fs = require('node:fs');
const server = http2.createServer();
server.on('stream', (stream, headers) => {
  let bytes = 0;
  stream.on('error', () => {});
  stream.on('data', chunk => { bytes += chunk.length; });
  stream.on('end', () => {
    fs.appendFileSync(process.argv[2], JSON.stringify({ path: headers[':path'], bytes }) + '\n');
    if ((headers['content-type'] || '').startsWith('application/grpc')) {
      stream.respond({ ':status': 200, 'content-type': 'application/grpc' }, { waitForTrailers: true });
      stream.on('wantTrailers', () => stream.sendTrailers({ 'grpc-status': '0' }));
      stream.end(Buffer.alloc(5));
    } else { stream.respond({ ':status': 200 }); stream.end('{}'); }
  });
});
server.listen(0, '127.0.0.1', () => console.log(server.address().port));
process.stdin.resume();
process.stdin.on('end', () => { server.close(); setTimeout(() => process.exit(0), 100).unref(); });
