#!/usr/bin/env python3
from pathlib import Path
import argparse
import base64
import hashlib
import http.server
import json
import os
import socket
import socketserver
import ssl
import struct
import subprocess
import tempfile
import threading

APP = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description='Verify native HTTPS, WSS, SNI and certificate validation')
parser.add_argument('--root', type=Path, default=APP / '.runtime')
parser.add_argument('--report', type=Path)
args = parser.parse_args()
seen = {'dns': [], 'sni': [], 'http': []}


class DNS(socketserver.BaseRequestHandler):
    def handle(self):
        command = b''
        while not command.endswith(b'\0'):
            data = self.request.recv(1)
            if not data:
                return
            command += data
        _, host, service, flags, family, kind, protocol, network = command[:-1].decode().split()
        seen['dns'].append(host)
        address = struct.pack('=H', socket.AF_INET) + struct.pack('!H', 0 if service == '^' else int(service)) + socket.inet_aton('127.0.0.1') + b'\0' * 8
        self.request.sendall(b'222\0' + struct.pack('!IIIIII', 1, int(flags), socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, len(address)) + address + struct.pack('!II', 0, 0))


class HTTP(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_GET(self):
        seen['http'].append({'host': self.headers.get('Host'), 'upgrade': self.headers.get('Upgrade')})
        if self.headers.get('Upgrade', '').lower() == 'websocket':
            key = self.headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            self.connection.settimeout(2)
            try:
                self.connection.recv(100)
            except OSError:
                pass
        else:
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'{}')


with tempfile.TemporaryDirectory(prefix='claude-dns-tls-') as directory:
    root = Path(directory)
    subprocess.run(['clang', '-O2', str(APP / 'tests/tls-cert.c'), '-lcrypto', '-o', str(root / 'tls-cert')], check=True)
    subprocess.run([str(root / 'tls-cert'), str(root / 'test-key.pem'), str(root / 'test-cert.pem')], check=True)
    dns = socketserver.ThreadingUnixStreamServer(str(root / 'netd.sock'), DNS)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), HTTP)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / 'test-cert.pem', root / 'test-key.pem')
    context.set_servername_callback(lambda connection, hostname, ctx: seen['sni'].append(hostname))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    for item in [dns, server]:
        threading.Thread(target=item.serve_forever, daemon=True).start()
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(('ANTHROPIC_', 'CLAUDE_', 'OTEL_')) or key.lower().endswith('_proxy') or key in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'BUN_OPTIONS', 'NODE_OPTIONS', 'NODE_TLS_REJECT_UNAUTHORIZED'):
            env.pop(key)
    env.update(CLAUDE_TERMUX_ROOT=str(args.root.resolve()), CLAUDE_TERMUX_AUTO_UPDATE='0', CLAUDE_CODE_TMPDIR=str(root), CLAUDE_CONFIG_DIR=str(root / 'config'), TMPDIR=str(root),
               BUN_OPTIONS='--preload ' + str(APP / 'tests/network-probe.mjs'),
               CLAUDE_TERMUX_DNS_SOCKET=str(root / 'netd.sock'),
               NODE_EXTRA_CA_CERTS=str(root / 'test-cert.pem'),
               DNS_TEST_REPORT=str(root / 'report.json'), DNS_TLS_PORT=str(server.server_port))
    try:
        process = subprocess.run([str(APP / 'bin/claude-termux'), '--version'],
                                 env=env, cwd=root, capture_output=True, text=True, timeout=25)
        if process.returncode:
            raise RuntimeError(process.stderr[-2000:])
        report = json.loads((root / 'report.json').read_text())
        assert report['fetchStatus'] == report['nodeHttpsStatus'] == 200
        assert report['wrongCertificateRejected'] and report['wss'] == 'connected'
        assert report['proxyEnvironment'] == []
        assert all(item['host'] == 'claude-network.invalid:' + str(server.server_port) for item in seen['http'])
        assert 'claude-network.invalid' in seen['sni']
        assert 'wrong-certificate.dns-audit.invalid' in seen['dns']
        report['observed'] = seen
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
    finally:
        for item in [dns, server]:
            item.shutdown()
            item.server_close()
