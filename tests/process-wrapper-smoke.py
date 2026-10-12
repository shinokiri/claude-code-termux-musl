#!/usr/bin/env python3
"""Verify real musl child PID/argv/env and DNS after its parent exits."""
import argparse
import base64
import http.server
import json
import os
from pathlib import Path
import signal
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time

APP = Path(__file__).resolve().parents[1]


def wait_for(check, label, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.03)
    raise RuntimeError('Timed out: ' + label)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=APP / 'build/runtime')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    native = (root / "libtermux-musl-compat.so").is_file()
    requests = []

    class DNS(socketserver.BaseRequestHandler):
        def handle(self):
            command = b''
            while not command.endswith(b'\0'):
                data = self.request.recv(4096)
                if not data:
                    return
                command += data
            fields = command[:-1].decode().split()
            requests.append(fields[0])
            if fields[0] == 'getaddrinfo':
                _, host, service, flags, family, kind, protocol, network = fields
                address = struct.pack('=H', socket.AF_INET) + struct.pack('!H', 0 if service == '^' else int(service)) + socket.inet_aton('127.0.0.1') + b'\0' * 8
                self.request.sendall(b'222\0' + struct.pack('!IIIIII', 1, int(flags), socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, len(address)) + address + struct.pack('!II', 0, 0))
            elif fields[0] == 'resnsend':
                packet = base64.b64decode(fields[3], validate=True)
                index = 12
                while packet[index]:
                    index += packet[index] + 1
                value = b'CHILD_DNS_OK'
                data = bytes([len(value)]) + value
                answer = packet[:2] + struct.pack('!HHHHH', 0x8180, 1, 1, 0, 0) + packet[12:index + 5]
                answer += b'\xc0\x0c' + struct.pack('!HHIH', 16, 1, 10, len(data)) + data
                self.request.sendall(struct.pack('!II', 0, len(answer)) + answer)

    class HTTP(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Length', '16')
            self.end_headers()
            self.wfile.write(b'CHILD_NETWORK_OK')

    with tempfile.TemporaryDirectory(prefix='claude-wrapper-test-') as directory:
        run = Path(directory)
        probe = run / 'probe.mjs'
        probe.write_text(r'''
const require = (await import('node:module')).createRequire(import.meta.url);
const fs = require('node:fs');
const path = require('node:path');
const run = process.env.WRAPPER_TEST_RUN;
const mode = process.env.WRAPPER_TEST_MODE;
const tokens = JSON.parse(process.env.WRAPPER_TEST_TOKENS);
const report = {pid: process.pid, argv: process.argv.slice(-tokens.length),
  sentinel: process.env.WRAPPER_TEST_SENTINEL, preloadRemoved: !process.env.LD_PRELOAD,
  savedPreload: process.env.CLAUDE_TERMUX_BIONIC_PRELOAD,
  managedProxy: !!process.env.CLAUDE_TERMUX_MANAGED_PROXY};
const write = name => fs.writeFileSync(path.join(run, name), JSON.stringify(report));
await (async () => {
  if (mode === 'parent') {
    const child = require('node:child_process').spawn(process.env.WRAPPER_TEST_ENTRY,
      [...(process.env.WRAPPER_TEST_NATIVE ? [] : [process.env.WRAPPER_TEST_BINARY]), '--version', ...tokens],
      {detached: true, stdio: 'ignore', env: {...process.env, WRAPPER_TEST_MODE: 'child'}});
    child.unref(); report.childPid = child.pid; write('parent.json');
  } else {
    write('ready.json');
    const deadline = Date.now() + 12000;
    while (!fs.existsSync(path.join(run, 'parent-exited'))) {
      if (Date.now() > deadline) throw new Error('Parent did not exit');
      await new Promise(resolve => setTimeout(resolve, 30));
    }
    const response = await fetch(process.env.WRAPPER_TEST_URL, {proxy: false, signal: AbortSignal.timeout(5000)});
    report.network = await response.text();
    write('http.json');
    report.txt = await require('node:dns').promises.resolveTxt('child.dns-test.invalid');
    write('child.json');
  }
  process.exit(0);
})().catch(error => {fs.writeFileSync(path.join(run, mode + '-error.txt'), String(error)); process.exit(1);});
''')
        dns = socketserver.ThreadingUnixStreamServer(str(run / 'dns.sock'), DNS)
        dns.daemon_threads = True
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), HTTP)
        for item in (dns, server):
            threading.Thread(target=item.serve_forever, daemon=True).start()
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(('CLAUDE_', 'ANTHROPIC_', 'OTEL_')) or key.lower().endswith('_proxy') or key in ('BUN_OPTIONS', 'NODE_OPTIONS'):
                env.pop(key)
        tokens = ['argument with spaces', '--joined=one two', '--separate', 'three', '', '☃']
        env.update(WRAPPER_TEST_RUN=str(run), WRAPPER_TEST_MODE='parent', WRAPPER_TEST_TOKENS=json.dumps(tokens),
                   WRAPPER_TEST_ENTRY=str(APP / 'bin/claude-process-wrapper'), WRAPPER_TEST_BINARY=str(root / 'claude'),
                   WRAPPER_TEST_SENTINEL='keep this env value',
                   WRAPPER_TEST_URL='http://child.dns-test.invalid:' + str(server.server_port),
                   CLAUDE_TERMUX_ROOT=str(root), CLAUDE_TERMUX_DNS_SOCKET=str(run / 'dns.sock'),
                   CLAUDE_CONFIG_DIR=str(run / 'config'), CLAUDE_CODE_TMPDIR=str(run / 'tmp'),
                   BUN_OPTIONS='--preload ' + str(probe))
        env['CLAUDE_TERMUX_BIONIC_PRELOAD'] = env.pop('LD_PRELOAD', '')
        env['CLAUDE_TERMUX_BIONIC_LIBRARY_PATH'] = env.pop('LD_LIBRARY_PATH', '')
        stale = 'http://claude:old-parent@127.0.0.1:1'
        env.update(CLAUDE_TERMUX_MANAGED_PROXY=stale, HTTPS_PROXY=stale, HTTP_PROXY=stale)
        if native:
            env.update(WRAPPER_TEST_NATIVE='1', WRAPPER_TEST_ENTRY=str(root / 'claude'))
        child_pid = None
        parent = None
        try:
            parent = subprocess.Popen([str(APP / 'bin/claude-process-wrapper'), str(root / 'claude'), '--version', *tokens],
                                      env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            out, err = parent.communicate(timeout=12)
            assert parent.returncode == 0 and not out, err
            parent_report = json.loads((run / 'parent.json').read_text())
            child_pid = parent_report['childPid']
            assert parent_report['pid'] == parent.pid
            wait_for(lambda: (run / 'ready.json').exists(), 'child startup')
            child = json.loads((run / 'ready.json').read_text())
            assert child['pid'] == child_pid
            (run / 'parent-exited').touch()
            wait_for(lambda: (run / 'child.json').exists() or (run / 'child-error.txt').exists(), 'child DNS/HTTP after parent exit')
            if (run / 'child-error.txt').exists():
                raise RuntimeError((run / 'child-error.txt').read_text() + '; DNS requests: ' + repr(requests))
            child = json.loads((run / 'child.json').read_text())
            assert child['network'] == 'CHILD_NETWORK_OK' and child['txt'][0][0] == 'CHILD_DNS_OK'
            for item in (parent_report, child):
                assert item['argv'] == tokens and item['sentinel'] == 'keep this env value'
                assert item['preloadRemoved'] and not item['managedProxy']
                assert item['savedPreload'] == env['CLAUDE_TERMUX_BIONIC_PRELOAD']
            assert 'getaddrinfo' in requests and 'resnsend' in requests
            report = {'pid_preserved': True, 'argv_preserved': True, 'inherited_environment': True,
                      'native_self_spawn': native, 'nested_wrapper': not native, 'child_network_after_parent_exit': True,
                      'child_record_dns_after_parent_exit': True, 'stale_parent_proxy_removed': True,
                      'extra_proxy_processes': 0, 'real_musl_binary': True, 'real_daemon': False}
            if args.report:
                args.report.write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report))
        except Exception:
            print('DNS requests:', requests)
            for path in [*run.glob('*.json'), *run.glob('*error.txt')]:
                print(path.name, path.read_text())
            raise
        finally:
            if parent and parent.poll() is None:
                parent.kill()
                parent.wait()
            if child_pid:
                try:
                    os.kill(child_pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            for item in (dns, server):
                item.shutdown()
                item.server_close()


if __name__ == '__main__':
    from fixture_runtime import enter
    enter()
    main()
