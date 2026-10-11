#!/usr/bin/env python3
"""Exercise DNS call sites identified after the complete native analysis.

First run ../dns-callsite/study.py. This study reuses its verified observer,
starts each API in a fresh process, and serves every lookup and UDP packet
locally. The installation and the ordinary privacy configuration are untouched.
"""
from pathlib import Path
from datetime import datetime, timezone
import argparse
import hashlib
import importlib.util
import json
import os
import signal
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('prior_dns_study', HERE.parent / 'dns-callsite/study.py')
prior = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prior)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observer', required=True, type=Path, help='Successful prior dns-callsite study directory')
    parser.add_argument('--output', required=True, type=Path, help='New output directory')
    parser.add_argument('--root', type=Path, default=Path.home() / '.local/share/claude-termux/current/runtime')
    args = parser.parse_args()
    observer, output, root = args.observer.resolve(), args.output.resolve(), args.root.resolve()
    prior.require(not output.exists() and not output.is_relative_to(root.parent), 'Use a new research output directory')
    prior.require(not any(c.isspace() for c in str(HERE)), 'Bun preload path must not contain whitespace')
    baseline = json.loads((observer / 'results.json').read_text())
    library = observer / 'libtermux-musl-compat.so'
    runtime = json.loads((root / 'runtime.json').read_text())
    prior.require(baseline['passed'] and baseline['original_sha256'] == prior.ORIGINAL_SHA256,
                  'Observer study did not pass for the pinned original')
    prior.require(prior.digest(library) == baseline['library_sha256'], 'Observer library changed')
    prior.require(runtime['version'] == '2.1.296' and runtime['original_sha256'] == prior.ORIGINAL_SHA256,
                  'Unexpected installed runtime')
    patched = prior.digest(root / 'claude')
    prior.require(patched == baseline['patched_sha256'] == runtime['patched_sha256'], 'Installed ELF changed')
    prior.require(json.loads((root.parent / 'adapter.json').read_text())['version'] == '1.7.2', 'Unexpected adapter')
    for name, expected in baseline['source_hashes'].items():
        prior.require(prior.digest(prior.REPO / name) == expected, 'Pinned source changed: ' + name)
    output.mkdir(parents=True, mode=0o700)
    expected_sites = {
        'lookup-system': ['0x1dea84c'], 'lookup-libc': ['0x1dea84c'], 'lookup-getaddrinfo': ['0x1dea84c'],
        'tcp-listen': ['0x23b0f30'], 'udp-bind': ['0x23b1834'],
        'udp-connect': ['0x23b1834', '0x23b1cd8'], 'cluster-raw-bind': ['0x19bd41c'],
    }
    cases = list(expected_sites)
    observed = {'lookups': [], 'datagrams': [], 'errors': []}
    lock = threading.Lock()

    class DNS(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                self.request.settimeout(5)
                command = bytearray()
                while not command.endswith(b'\0'):
                    data = self.request.recv(4096)
                    if not data:
                        return
                    command.extend(data)
                    if len(command) > 4096:
                        raise ValueError('Oversized fixture command')
                op, host, service, flags, family, kind, protocol, network = command[:-1].decode('ascii').split()
                if op != 'getaddrinfo' or network != '0' or not host.endswith('.native-followup.invalid'):
                    raise ValueError('Unexpected fixture DNS command')
                flags, family, kind, protocol = map(int, (flags, family, kind, protocol))
                port = 0 if service == '^' else int(service)
                kind = kind or socket.SOCK_STREAM
                protocol = protocol or (socket.IPPROTO_UDP if kind == socket.SOCK_DGRAM else socket.IPPROTO_TCP)
                address = struct.pack('=H', socket.AF_INET) + struct.pack('!H', port) + socket.inet_aton('127.0.0.1') + b'\0' * 8
                with lock:
                    observed['lookups'].append({'host': host, 'port': port, 'flags': flags, 'family': family,
                                                'kind': kind, 'protocol': protocol})
                self.request.sendall(b'222\0' + struct.pack('!IIIIII', 1, flags, socket.AF_INET, kind, protocol, len(address))
                                     + address + struct.pack('!II', 0, 0))
            except Exception as error:
                with lock:
                    observed['errors'].append(str(error))

    class UDP(socketserver.BaseRequestHandler):
        def handle(self):
            packet, _ = self.request
            with lock:
                observed['datagrams'].append({'payload': packet.decode('ascii'), 'peer': self.client_address[0]})

    class UnixServer(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True

    started = datetime.now(timezone.utc).isoformat()
    results = {}
    with tempfile.TemporaryDirectory(prefix='claude-dns-followup-') as temporary:
        run = Path(temporary)
        fixture_home = run / 'home'
        fixture_home.mkdir()
        dns = UnixServer(str(run / 'dns.sock'), DNS)
        udp = socketserver.ThreadingUDPServer(('127.0.0.1', 0), UDP)
        for server in (dns, udp):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for kind in cases:
                trace = output / (kind + '-trace.jsonl')
                report = output / (kind + '-report.json')
                env = {k: os.environ[k] for k in ('PATH', 'PREFIX', 'ANDROID_ROOT', 'ANDROID_DATA') if k in os.environ}
                env.update(HOME=str(fixture_home), CLAUDE_CONFIG_DIR=str(run / 'config'), CLAUDE_CODE_TMPDIR=str(run), TMPDIR=str(run),
                           CLAUDE_TERMUX_AUTO_UPDATE='0', CLAUDE_TERMUX_ROOT=str(root),
                           CLAUDE_TERMUX_PRIVACY='standard',
                           CLAUDE_TERMUX_DNS_SOCKET=str(run / 'dns.sock'), LD_PRELOAD=str(library),
                           BUN_OPTIONS='--preload ' + str(HERE / 'probe.mjs'),
                           NATIVE_DNS_TRACE_FIXTURE='isolated-dns-callsite-v1', NATIVE_DNS_TRACE_FILE=str(trace),
                           NATIVE_FOLLOWUP_CASE=kind, NATIVE_FOLLOWUP_REPORT=str(report),
                           NATIVE_FOLLOWUP_UDP_PORT=str(udp.server_address[1]))
                process = subprocess.Popen([str(root / 'claude'), '--version'], cwd=run, env=env,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                           start_new_session=True)
                try:
                    stdout, stderr = process.communicate(timeout=25)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    stdout, stderr = process.communicate()
                    stderr += '\nIsolated fixture process group timed out and was reaped.\n'
                finally:
                    # Only the newly created fixture session; never other CLI processes.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                (output / (kind + '-stderr.txt')).write_text(stderr)
                rows = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
                result = json.loads(report.read_text()) if report.exists() else {'passed': False, 'error': stderr[-2000:]}
                result.update(exit_code=process.returncode, trace=rows,
                              getaddrinfo_call_sites=sorted({hex(int(row['return_address'], 16) - 4)
                                                            for row in rows if row['api'] == 'getaddrinfo'}))
                results[kind] = result
                print(json.dumps({'case': kind, 'passed': result['passed'], 'call_sites': result['getaddrinfo_call_sites'],
                                  'error': result.get('error')}), flush=True)
            # The loopback receiver can run just after a successful send returns.
            deadline = time.monotonic() + 2
            while len(observed['datagrams']) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        finally:
            for server in (dns, udp):
                server.shutdown()
                server.server_close()
    passed = (all(row['passed'] and row['exit_code'] == 0 and row['trace'] for row in results.values())
              and all(row['getaddrinfo_call_sites'] == expected_sites[kind] for kind, row in results.items())
              and all(row['main_binary'] for case in results.values() for row in case['trace'])
              and {row['host'].split('.')[0] for row in observed['lookups']} == set(cases)
              and {row['payload'] for row in observed['datagrams']} ==
                  {'native-dns-fixture:udp-bind', 'native-dns-fixture:udp-connect'}
              and all(row['peer'] == '127.0.0.1' for row in observed['datagrams'])
              and not observed['errors'])
    result = {'schema': 1, 'started_utc': started, 'finished_utc': datetime.now(timezone.utc).isoformat(),
              'passed': passed, 'original_sha256': prior.ORIGINAL_SHA256, 'patched_sha256': patched,
              'observer_sha256': prior.digest(library), 'production_modified': False,
              'cases': results, 'observed': observed,
              'limits': ['Only these isolated APIs and this exact binary were exercised.',
                         'The observer changes timing and is absent from the production installation.',
                         'This does not establish every control-flow branch, DNS case, TLS fingerprint or privacy path.']}
    (output / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    prior.require(prior.digest(root / 'claude') == patched, 'Installed ELF changed during study')
    prior.require(passed, 'At least one isolated API fixture failed; inspect the saved results')


if __name__ == '__main__':
    main()
