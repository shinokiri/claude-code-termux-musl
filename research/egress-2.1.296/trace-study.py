#!/usr/bin/env python3
"""Trace synthetic egress challenges and real ARM64 CLI fixtures on Termux."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
PATCHED = '0563350c19d156e3c4e1595f9524185de7d948543ac5edbd5c6b9140e376a06d'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def trace_run(strace, libraries, directory, command, *, env, cwd, timeout=90, poll_hook=None, guard=None, **_):
    directory.mkdir(parents=True, exist_ok=True)
    argv = [str(strace), '-ff', '-qq', '-ttt', '-i', '-yy', '-s', '1048576',
            '-e', 'abbrev=none', '-e', 'raw=read,pread64,readv,preadv,preadv2,getdents64', '--kill-on-exit',
            '-o', str(directory / 'syscalls')]
    trace_env = dict(env)
    if libraries:
        # Only the tracer loads these extracted Termux libraries. Its child gets
        # precisely the fixture's original LD_LIBRARY_PATH (including absence).
        trace_env['LD_LIBRARY_PATH'] = str(libraries)
        argv += ['-E', 'LD_LIBRARY_PATH=' + env['LD_LIBRARY_PATH'] if 'LD_LIBRARY_PATH' in env else 'LD_LIBRARY_PATH']
    traced_command = [str(guard), str(directory / 'guard.jsonl'), '--', *command] if guard else command
    argv += traced_command
    process = subprocess.Popen(argv, env=trace_env, cwd=cwd, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, start_new_session=True)
    try:
        timeout = max(timeout, 180) if guard else timeout
        deadline = time.monotonic() + timeout
        while True:
            try:
                stdout, stderr = process.communicate(timeout=min(0.5, max(0.01, deadline-time.monotonic())) if poll_hook else timeout)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() >= deadline:
                    raise
                if poll_hook:
                    poll_hook()
                else:
                    raise
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        (directory / 'timeout.stdout').write_text(stdout)
        (directory / 'timeout.stderr').write_text(stderr)
        raise
    (directory / 'stdout.txt').write_text(stdout)
    (directory / 'stderr.txt').write_text(stderr)
    (directory / 'invocation.json').write_text(json.dumps({
        'tracer': str(strace), 'tracer_sha256': sha(strace), 'options': argv[1:argv.index(traced_command[0])],
        'command': command, 'traced_command': traced_command, 'guard_sha256': sha(guard) if guard else None,
        'exit': process.returncode,
        'scope': 'All syscall names, separate files per traced TID, follow forks/exec; owned synthetic processes only.'
    }, indent=2) + '\n')
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def clean_env():
    return {k: os.environ[k] for k in ('PATH', 'PREFIX', 'TERMUX_VERSION', 'ANDROID_ROOT', 'ANDROID_DATA', 'LD_PRELOAD', 'LD_LIBRARY_PATH') if k in os.environ}


def background_runner(strace, libraries, directory, command, **kwargs):
    """Follow the actual daemon/session, then terminate only recorded owned PIDs."""
    records = directory / 'owned-processes.jsonl'
    probe = directory / 'process-probe.mjs'
    probe.write_text("import fs from 'node:fs';\n"
        + "const s=globalThis[Symbol.for('claude-termux.privacy.v1')];\n"
        + "fs.appendFileSync(" + json.dumps(str(records)) + ",JSON.stringify({pid:process.pid,"
        + "start:fs.readFileSync('/proc/self/stat','utf8').split(')')[1].trim().split(/\\s+/)[19],"
        + "kind:process.argv.includes('daemon')?['daemon']:process.argv.includes('--session-id')?['--session-id']:['entry'],"
        + "privacyReady:s?.ready===true,privacyMode:s?.mode})+'\\n');\n")
    kwargs['env'] = dict(kwargs['env'])
    kwargs['env']['BUN_OPTIONS'] += ' --preload ' + str(probe)
    spec = importlib.util.spec_from_file_location('device_smoke', REPO / 'tests/device-smoke.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    stopped = False

    def poll():
        nonlocal stopped
        if not stopped and any('DATAFLOW_DONE' in path.read_text()
                               for path in (directory / 'config/projects').rglob('*.jsonl')):
            helper.clean_test_processes(records)
            stopped = True

    try:
        result = trace_run(strace, libraries, directory / 'trace', command, poll_hook=poll, **kwargs)
    finally:
        helper.clean_test_processes(records)
    rows = [json.loads(line) for line in records.read_text().splitlines()] if records.exists() else []
    kinds = [row['kind'] for row in rows]
    summary = {'records': rows, 'completed_before_cleanup': stopped,
               'cleanup_verified': True, 'daemon_and_session_observed': ['daemon'] in kinds and ['--session-id'] in kinds}
    (directory / 'background.json').write_text(json.dumps(summary, indent=2) + '\n')
    if not stopped or not summary['daemon_and_session_observed']:
        raise RuntimeError('Actual background daemon/session did not complete under observation')
    return result


def guard_negative(strace, libraries, output, guard):
    result = trace_run(strace, libraries, output / 'guard-negative',
                       [str(output / 'challenge/challenge'), '--negative-guard'],
                       env=clean_env(), cwd=output, timeout=20, guard=guard)
    rows = [json.loads(line) for line in (output / 'guard-negative/guard.jsonl').read_text().splitlines()]
    denied = [row for row in rows if not row['allowed']]
    summary = {'exit': result.returncode, 'denied': denied,
               'passed': result.returncode == 0 and 'GUARD_NEGATIVE_DONE' in result.stdout
                         and {row['syscall'] for row in denied} == {203, 206, 211, 269, 221}}
    if not summary['passed']:
        raise RuntimeError('Guard failed its direct-connect/UDP/exec negative controls')
    return summary


def challenge(strace, libraries, output):
    output.mkdir()
    binary = output / 'challenge'
    subprocess.run(['clang', '-Wall', '-Wextra', '-Werror', '-O1', '-g', str(HERE / 'trace-challenge.c'), '-o', str(binary)], check=True)
    received = {'tcp': '', 'udp': [], 'errors': []}
    tcp, udp = socket.socket(), socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tcp.bind(('127.0.0.1', 0))
    udp.bind(('127.0.0.1', 0))
    tcp.listen(1)
    tcp.settimeout(15)
    udp.settimeout(15)
    tcp_port, udp_port = tcp.getsockname()[1], udp.getsockname()[1]

    def listen_tcp():
        try:
            with tcp.accept()[0] as conn:
                conn.settimeout(15)
                received['tcp_peer'] = list(conn.getpeername())
                chunks = []
                while chunk := conn.recv(65536):
                    chunks.append(chunk)
                received['tcp'] = b''.join(chunks).decode()
        except Exception as error:
            received['errors'].append(repr(error))

    def listen_udp():
        try:
            for _ in range(3):
                data, peer = udp.recvfrom(4096)
                received['udp'].append({'data': data.decode(), 'peer': list(peer)})
        except Exception as error:
            received['errors'].append(repr(error))

    threads = [threading.Thread(target=f, daemon=True) for f in (listen_tcp, listen_udp)]
    for thread in threads:
        thread.start()
    result = trace_run(strace, libraries, output / 'trace', [str(binary), str(tcp_port), str(udp_port)],
                       env=clean_env(), cwd=output, timeout=30)
    for thread in threads:
        thread.join(16)
    tcp.close()
    udp.close()
    expected = 'LIBC_WRITE\nINLINE_SVC_WRITE\nVECTOR_WRITE\nSENDFILE\nSCM_RIGHTS_CHILD\nEXEC_INHERITED\n'
    received.update(exit=result.returncode, tcp_port=tcp_port, udp_port=udp_port,
                    binary_sha256=sha(binary), source_sha256=sha(HERE / 'trace-challenge.c'))
    received['receiver_passed'] = (result.returncode == 0 and not received['errors'] and received['tcp'] == expected
                                   and [row['data'] for row in received['udp']] == ['UDP_SENDTO', 'MMSG_1', 'MMSG_2'])
    (output / 'receiver.json').write_text(json.dumps(received, indent=2) + '\n')
    assembly = subprocess.run(['llvm-objdump', '-d', '--disassemble-symbols=inline_write', str(binary)], capture_output=True, text=True, check=True)
    (output / 'inline-write.asm').write_text(assembly.stdout)
    if not received['receiver_passed']:
        raise RuntimeError('Trace challenge failed; inspect its receiver/trace records')
    print(json.dumps({'case': 'challenge', 'receiver_passed': True, 'tcp_bytes': len(expected), 'udp_bytes': 22}), flush=True)
    return received


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.home() / '.local/share/claude-termux/current/runtime')
    parser.add_argument('--strace', type=Path, required=True)
    parser.add_argument('--tracer-libraries', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--challenge-only', action='store_true')
    parser.add_argument('--background-only', action='store_true')
    args = parser.parse_args()
    root, output, strace = args.root.resolve(), args.output.resolve(), args.strace.resolve()
    libraries = args.tracer_libraries.resolve() if args.tracer_libraries else None
    if output.exists() or output.is_relative_to(root.parent) or sha(root / 'claude') != PATCHED:
        raise RuntimeError('Requires pinned installation and a new output directory outside the release')
    output.mkdir(parents=True, mode=0o700)
    report = {'schema': 1, 'started_utc': datetime.now(timezone.utc).isoformat(), 'cases': {},
              'patched_sha256': PATCHED, 'fixture_sha256': sha(REPO / 'tests/privacy-dataflow.py'),
              'tracer_sha256': sha(strace), 'production_modified': False}
    report['challenge'] = challenge(strace, libraries, output / 'challenge')
    if not args.challenge_only:
        guard = output / 'fixture-guard'
        subprocess.run(['clang', '-O2', '-Wall', '-Wextra', '-Werror', str(HERE / 'fixture-guard.c'), '-o', str(guard)], check=True)
        report['guard_negative'] = guard_negative(strace, libraries, output, guard)
        spec = importlib.util.spec_from_file_location('dataflow', REPO / 'tests/privacy-dataflow.py')
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        helper = output / 'tls-cert'
        subprocess.run(['clang', '-O2', str(REPO / 'tests/tls-cert.c'), '-lcrypto', '-o', str(helper)], check=True)
        cert, key = output / 'cert.pem', output / 'key.pem'
        hosts = ('api.anthropic.com', 'api-staging.anthropic.com', 'statsig.anthropic.com', 'claude.ai',
                 'http-intake.logs.us5.datadoghq.com', 'browser-intake-us5-datadoghq.com',
                 'raw.githubusercontent.com', 'downloads.claude.ai', 'github.com')
        subprocess.run([str(helper), str(key), str(cert), ','.join('DNS:' + host for host in hosts)], check=True)
        key.chmod(0o600)
        scenarios = [('standard', 'standard', False, False), ('optional-off', 'optional-off', False, False),
                     ('optional-off-recovery', 'optional-off', True, False),
                     ('background-standard', 'standard', False, True), ('background-optional-off', 'optional-off', False, True)]
        if args.background_only:
            scenarios = [row for row in scenarios if row[3]]
        for name, mode, failure, background in scenarios:
            def runner(command, **kwargs):
                kwargs['guard'] = guard
                kwargs['env']['GIT_SSL_CAINFO'] = str(cert)
                if background:
                    return background_runner(strace, libraries, output / name, command, **kwargs)
                return trace_run(strace, libraries, output / name / 'trace', command, **kwargs)
            result = fixture.run_case(root, output / name, cert, key, mode, False,
                                      request_check=not background, stream_failure=failure, process_runner=runner,
                                      observe_transport=True, background=background)
            report['cases'][name] = result
            print(json.dumps({'case': name, 'passed': result['passed'], 'failures': result['failures'],
                              'model_requests': len(result['model']), 'event_batches': result['event_batches']}), flush=True)
    report.update(finished_utc=datetime.now(timezone.utc).isoformat(),
                  receiver_checks_passed=all(row['passed'] for row in report['cases'].values()))
    if sha(root / 'claude') != PATCHED:
        raise RuntimeError('Production ELF changed')
    (output / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    if not report['receiver_checks_passed']:
        raise SystemExit('Fixture checks failed; inspect results')


if __name__ == '__main__':
    main()
