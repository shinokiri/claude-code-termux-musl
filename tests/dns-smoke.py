#!/usr/bin/env python3
"""Exercise native c-ares APIs through a controlled Android DNS socket fixture."""
import argparse
import base64
import collections
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time

APP = Path(__file__).resolve().parents[1]


def name(value):
    return b''.join(bytes([len(label)]) + label.encode('ascii') for label in value.rstrip('.').split('.')) + b'\0'


def txt(*values):
    return b''.join(bytes([len(value)]) + value for value in values)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=APP / 'build/runtime')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--binary', type=Path, help='Raw binary, for isolated library development')
    parser.add_argument('--library', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    state = {'records': collections.Counter(), 'names': collections.Counter(), 'custom_udp': 0,
             'active_parallel': 0, 'peak_parallel': 0}
    lock = threading.Lock()
    records = {
        1: socket.inet_aton('203.0.113.7'), 28: socket.inet_pton(socket.AF_INET6, '2001:db8::7'),
        2: name('ns.example.invalid'), 5: name('target.example.invalid'), 12: name('ptr.example.invalid'),
        15: struct.pack('!H', 10) + name('mail.example.invalid'), 16: txt(b'first', b'second'),
        6: name('ns.example.invalid') + name('hostmaster.example.invalid') + struct.pack('!IIIII', 7, 3600, 600, 86400, 60),
        33: struct.pack('!HHH', 10, 20, 8443) + name('service.example.invalid'),
        35: struct.pack('!HH', 100, 10) + txt(b'S', b'SIP+D2U', b'') + name('_sip._udp.example.invalid'),
        257: bytes([0, 5]) + b'issueca.example.invalid',
    }

    def response(packet, custom=False):
        index = 12
        labels = []
        while packet[index]:
            size = packet[index]
            labels.append(packet[index + 1:index + 1 + size].decode('ascii').lower())
            index += size + 1
        index += 1
        kind, cls = struct.unpack('!HH', packet[index:index + 4])
        host = '.'.join(labels)
        with lock:
            state['records'][str(kind)] += 1
            state['names'][host] += 1
            if custom:
                state['custom_udp'] += 1
        if host.startswith('parallel-'):
            with lock:
                state['active_parallel'] += 1
                state['peak_parallel'] = max(state['peak_parallel'], state['active_parallel'])
            time.sleep(0.06)
            with lock:
                state['active_parallel'] -= 1
        if host.startswith('timeout.'):
            time.sleep(8)
        elif host.startswith('cancel.'):
            time.sleep(0.5)
        rcode = next((code for prefix, code in [('missing.', 3), ('failure.', 2), ('refused.', 5)] if host.startswith(prefix)), 0)
        answer_records = []
        if not rcode and not host.startswith('empty.'):
            if custom and host.startswith('custom'):
                answer_records = [(1, socket.inet_aton('192.0.2.55'))]
            elif host.startswith('large.'):
                answer_records = [(16, txt(b'x' * 240))] * 48
            elif kind == 255:
                answer_records = [(1, records[1]), (28, records[28]), (15, records[15]), (16, records[16]), (6, records[6])]
            elif kind in records:
                answer_records = [(kind, records[kind])]
        answer = b''.join(b'\xc0\x0c' + struct.pack('!HHIH', rrtype, 1, 73, len(data)) + data for rrtype, data in answer_records)
        wire = packet[:2] + struct.pack('!HHHHH', 0x8180 | rcode, 1, len(answer_records), 0, 0) + packet[12:index + 4] + answer
        return host, rcode, wire

    class Android(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            try:
                command = bytearray()
                while not command.endswith(b'\0'):
                    chunk = self.request.recv(4096)
                    if not chunk:
                        return
                    command.extend(chunk)
                    if len(command) > 4096:
                        return
                operation, network, flags, encoded = command[:-1].decode('ascii').split()
                if operation != 'resnsend' or network != '0' or flags != '0':
                    raise ValueError('Unexpected Android DNS command')
                host, rcode, packet = response(base64.b64decode(encoded, validate=True))
                data = struct.pack('!II', rcode, len(packet)) + packet
                if host.startswith('fragmented.'):
                    for index in range(0, len(data), 3):
                        self.request.sendall(data[index:index + 3])
                        time.sleep(0.0003)
                else:
                    self.request.sendall(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

    class UDP(socketserver.BaseRequestHandler):
        def handle(self):
            packet, sock = self.request
            _, _, wire = response(packet, custom=True)
            sock.sendto(wire, self.client_address)

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
        request_queue_size = 256

    with tempfile.TemporaryDirectory(prefix='claude-dns-test-') as directory:
        run = Path(directory)
        server = Server(str(run / 'dns.sock'), Android)
        udp = socketserver.ThreadingUDPServer(('127.0.0.1', 0), UDP)
        for item in (server, udp):
            threading.Thread(target=item.serve_forever, daemon=True).start()
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(('ANTHROPIC_', 'CLAUDE_', 'OTEL_')) or key.lower().endswith('_proxy') or key in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'BUN_OPTIONS', 'NODE_OPTIONS'):
                env.pop(key)
        env.update(CLAUDE_CONFIG_DIR=str(run / 'config'), TMPDIR=str(run), CLAUDE_CODE_TMPDIR=str(run),
                   CLAUDE_TERMUX_ROOT=str(root), CLAUDE_TERMUX_AUTO_UPDATE='0',
                   CLAUDE_TERMUX_DNS_SOCKET=str(run / 'dns.sock'), DNS_TEST_CUSTOM_PORT=str(udp.server_address[1]),
                   BUN_OPTIONS='--preload ' + str(APP / 'tests/dns-probe.mjs'), DNS_TEST_REPORT=str(run / 'report.json'))
        command = [str(APP / 'bin/claude-termux'), '--version']
        if args.binary:
            command[0] = str(args.binary.resolve())
        if args.library:
            env['LD_PRELOAD'] = str(args.library.resolve())
        try:
            result = subprocess.run(command, env=env, cwd=run, capture_output=True, text=True, timeout=35)
            if result.returncode:
                raise RuntimeError('Native DNS test failed: ' + result.stdout[-1000:] + result.stderr[-4000:])
            report = json.loads((run / 'report.json').read_text())
            assert state['peak_parallel'] >= 8, 'DNS bridge serialized parallel queries'
            assert state['names']['large.dns-test.invalid'] >= 2, 'Large answer did not exercise TCP fallback'
            assert state['custom_udp'] == 7, 'Explicit DNS servers did not use their own transport'
            report['observed'] = state
            if args.report:
                args.report.write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps({'record_types': len(report['records']), 'api_variants': 5, 'workers': len(report['workers']),
                              'errors': report['errors'], 'concurrent_peak': state['peak_parallel'],
                              'tcp_large_records': report['largeRecords'], 'explicit_servers_preserved': True}))
        finally:
            for item in (server, udp):
                item.shutdown()
                item.server_close()


if __name__ == '__main__':
    main()
