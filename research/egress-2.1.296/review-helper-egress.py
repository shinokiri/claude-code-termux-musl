#!/usr/bin/env python3
"""Review the unexpected SSH probes without reading or publishing key contents."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, action='append', required=True)
    parser.add_argument('--artifact', type=Path, required=True, help='Extracted $bunfs/root directory')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Use a new output file')
    spec = importlib.util.spec_from_file_location('trace_analysis', HERE / 'trace-analyze.py')
    analysis = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(analysis)
    findings = []
    for run in args.run:
        for case in sorted(run.glob('background-*')):
            data = analysis.base_analysis(case / 'trace')
            for execution in data['lifecycle']:
                if execution['syscall'] != 'execve' or execution['return'] != 0:
                    continue
                program = analysis.STRING.match(execution['args'])
                if not program or not program[0].endswith('/ssh"') or '"git@github.com"' not in execution['args']:
                    continue
                tid = execution['tid']
                rows = [row for row in data['rows'] if row['tid'] == tid]
                sends = [row for row in data['writes'] if row['tid'] == tid and row['descriptor']['kind'] == 'internet']
                payload = b''.join(analysis.write_buffer(row) for row in sends)
                identification, packets = payload.split(b'\r\n', 1) if payload else (b'',b'')
                decoded, cursor = [], 0
                while cursor + 6 <= len(packets):
                    size = int.from_bytes(packets[cursor:cursor+4], 'big')
                    padding = packets[cursor+4]
                    if size < padding + 2 or cursor + size + 4 > len(packets):
                        raise RuntimeError('SSH stream is not fully framed cleartext; do not infer authentication phase')
                    decoded.append({'packet_bytes': size+4, 'padding_bytes': padding, 'message_number': packets[cursor+5]})
                    cursor += size + 4
                complete = cursor == len(packets)
                accesses = []
                for row in rows:
                    if row['syscall'] not in ('openat','open') or row['return'] is None or row['return'] < 0:
                        continue
                    match = re.search(r'"([^"\n]*/\.ssh/[^"\n]*)"', row['args'])
                    if match:
                        accesses.append({'basename': Path(match[1]).name, 'tid': tid, 'line': row['line'], 'fd': row['return']})
                destinations = [conn['target'] for item in data['sockets'].values() for conn in item['connects']
                                if conn['tid']==tid and conn['descriptor']['kind']=='internet']
                findings.append({'run': run.name, 'case': case.name, 'tid': tid,
                    'trace_sha256': sha(case / 'trace' / ('syscalls.'+str(tid))),
                    'argv_purpose': 'authentication-probe' if '"-T"' in execution['args'] else 'git-transport-for-official-marketplace',
                    'destination_addresses': destinations, 'internet_write_calls': len(sends), 'accepted_bytes': len(payload),
                    'identification': identification.decode(), 'packets': decoded, 'all_accepted_bytes_framed': complete,
                    'only_kexinit_and_kex_method_init_or_no_send': complete and (not payload or [row['message_number'] for row in decoded]==[20,30]),
                    'credential_path_opens': accesses, 'successful_clone_calls': [analysis.compact(row) for row in rows
                        if row['syscall'] in ('clone','clone3','fork','vfork') and (row['return'] or 0)>0]})
    module = args.artifact / 'chunk-a7h7w6pe.js'
    source = module.read_bytes()
    start = source.index(b'async function bgt(){')
    end = source.index(b'import{createHash as fbn}', start)
    daemon = args.artifact / 'chunk-vt80wwxe.js'
    report = {'schema': 1, 'reviewed_utc': datetime.now(timezone.utc).isoformat(), 'findings': findings,
        'source': {'module': module.name, 'sha256': sha(module), 'probe_function': 'bgt',
                   'range': [start,end], 'fragment_sha256': hashlib.sha256(source[start:end]).hexdigest(),
                   'interpretation': 'Tests GitHub SSH authentication; code 1 plus successfully authenticated marks configured. It is used by Git transport selection; not identified as a telemetry sender.',
                   'daemon_path_module': daemon.name, 'daemon_path_module_sha256': sha(daemon),
                   'daemon_path_condition': 'Uses PREFIX/tmp only when the real TERMUX_VERSION and PREFIX are present; otherwise /tmp.'},
        'all_observed_ssh_probes_stopped_before_user_auth': bool(findings) and all(row['only_kexinit_and_kex_method_init_or_no_send'] and not row['successful_clone_calls'] for row in findings),
        'production_changes': False,
        'corrections': ['A temporary HOME and HTTP proxy are not process-tree isolation.',
                        'The unguarded background discovery runs made non-loopback SSH connections and opened real default identity paths. They must not be described as loopback-only/no-credential-access tests.',
                        'The captured complete SSH streams contain identification and KEXINIT/method-init packets only; no outgoing NEWKEYS, encrypted user-authentication traffic or private-key upload is evidenced in these runs.',
                        'No private-key contents are read by this review script or included in its output.'],
        'protocol_reference': 'https://github.com/openssh/openssh-portable/blob/master/ssh2.h'}
    args.output.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps({'ssh_probes':len(findings), 'all_before_user_auth': report['all_observed_ssh_probes_stopped_before_user_auth']}))
    if not report['all_observed_ssh_probes_stopped_before_user_auth']:
        raise SystemExit('Unresolved SSH stream requires further investigation')


if __name__ == '__main__':
    main()
