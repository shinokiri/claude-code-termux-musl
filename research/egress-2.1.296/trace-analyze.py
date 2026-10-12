#!/usr/bin/env python3
"""Correlate complete synthetic outgoing strace buffers with loopback TLS streams.

This parser fails closed on unparsed records, truncated network buffers, socket
writes without a corresponding connect/send address and ambiguous stream matches.
It reports IPC separately; a local IPC peer is not proof of its later behavior.
"""
import argparse
import ast
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

HERE = Path(__file__).resolve().parent
LINE = re.compile(r'^(\d+\.\d+) \[([0-9a-f?]+)\] (.*)$')
CALL = re.compile(r'^(\w+)\((.*)\)\s+= (.*)$')
FD = re.compile(r'(?<!\w)(\d+)<([^>]+)>')
SOCKET = re.compile(r'^(TCPv6|TCP|UDPv6|UDP|UNIX(?:-[A-Z]+)?|NETLINK):\[(\d+)\]')
STRING = re.compile(r'"(?:\\.|[^"\\])*"')
WRITES = {'write', 'writev', 'pwrite64', 'pwritev', 'pwritev2', 'sendto', 'sendmsg', 'sendmmsg',
          'sendfile', 'splice', 'vmsplice', 'tee', 'copy_file_range'}
TRANSFERS = {'sendmsg', 'recvmsg', 'sendmmsg', 'recvmmsg'}


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def records(directory):
    rows, errors, manifests = [], [], []
    for path in sorted(directory.glob('syscalls.*')):
        tid = int(path.name.rsplit('.', 1)[1])
        manifests.append({'name': path.name, 'bytes': path.stat().st_size, 'sha256': sha(path)})
        pending = None
        for line_no, line in enumerate(path.read_text().splitlines(), 1):
            match = LINE.match(line)
            if not match:
                if line.startswith(('+++ ', '--- ')) or not line.strip():
                    continue
                errors.append({'file': path.name, 'line': line_no, 'reason': 'unparsed prefix', 'text': line[:500]})
                continue
            timestamp, ip, text = match.groups()
            if text.startswith(('+++ ', '--- ')):
                continue
            if text.endswith('<unfinished ...>'):
                if pending:
                    errors.append({'file': path.name, 'line': line_no, 'reason': 'overlapping unfinished syscall'})
                pending = (timestamp, ip, text[:-len('<unfinished ...>')], line_no)
                continue
            if text.startswith('<... '):
                resumed = re.match(r'<\.\.\. (\w+) resumed>(.*)$', text)
                if not pending or not resumed or not pending[2].startswith(resumed[1] + '('):
                    errors.append({'file': path.name, 'line': line_no, 'reason': 'unmatched resumed syscall', 'text': text[:500]})
                    continue
                timestamp, ip, start, line_no = pending
                text, pending = start + resumed[2], None
            call = CALL.match(text)
            if not call:
                errors.append({'file': path.name, 'line': line_no, 'reason': 'unparsed call', 'text': text[:500]})
                continue
            name, args, result = call.groups()
            ret = re.match(r'(-?(?:0x[0-9a-f]+|\d+))', result)
            rows.append({'tid': tid, 'time': float(timestamp), 'ip': '0x' + ip,
                         'syscall': name, 'args': args, 'result': result,
                         'return': int(ret[1], 8 if re.fullmatch(r'0[0-7]+', ret[1]) else 0) if ret else None, 'line': line_no})
        if pending:
            errors.append({'file': path.name, 'line': pending[3], 'reason': 'unfinished at end', 'text': pending[2][:500]})
    return sorted(rows, key=lambda row: (row['time'], row['tid'], row['line'])), errors, manifests


def descriptor(text):
    match = FD.search(text)
    if not match:
        return {'fd': None, 'kind': 'unresolved', 'label': text[:80]}
    fd, label = int(match[1]), match[2]
    sock = SOCKET.match(label)
    if sock:
        return {'fd': fd, 'kind': 'internet' if sock[1].startswith(('TCP', 'UDP')) else 'ipc',
                'protocol': sock[1], 'inode': sock[2], 'label': label}
    if label.startswith('socket:['):
        return {'fd': fd, 'kind': 'unresolved-socket', 'label': label}
    if label.startswith('pipe:['):
        return {'fd': fd, 'kind': 'pipe', 'label': label}
    if label.startswith('anon_inode:'):
        return {'fd': fd, 'kind': 'anonymous', 'label': label}
    return {'fd': fd, 'kind': 'file-or-device', 'label': label}


def endpoint(text):
    family = re.search(r'sa_family=(AF_\w+)', text)
    if not family:
        return None
    family = family[1]
    if family == 'AF_INET':
        addr, port = re.search(r'inet_addr\("([^"]+)"\)', text), re.search(r'sin_port=htons\((\d+)\)', text)
        if addr and port:
            return {'family': family, 'address': addr[1], 'port': int(port[1])}
    elif family == 'AF_INET6':
        addr, port = re.search(r'inet_pton\(AF_INET6, "([^"]+)"', text), re.search(r'sin6_port=htons\((\d+)\)', text)
        if addr and port:
            return {'family': family, 'address': addr[1], 'port': int(port[1])}
    elif family == 'AF_UNIX':
        path = re.search(r'sun_path=(@?"(?:\\.|[^"\\])*")', text)
        return {'family': family, 'path': path[1] if path else None}
    return {'family': family}


def compact(row):
    return {key: row[key] for key in ('tid', 'time', 'ip', 'syscall', 'return', 'line')}


def write_buffer(row):
    args, name, size = row['args'], row['syscall'], row['return']
    if name in ('write', 'pwrite64', 'sendto'):
        # Only the buffer following the descriptor, not a destination string.
        value = args.split(', ', 1)[1]
        match = STRING.match(value)
        if not match or value[match.end():].startswith('...'):
            raise ValueError('outgoing string is unavailable or truncated')
        data = ast.literal_eval('b' + match[0])
    elif name in ('writev', 'pwritev', 'pwritev2', 'sendmsg'):
        chunks = []
        for match in re.finditer(r'iov_base=("(?:\\.|[^"\\])*"|[^,}]+)(\.\.\.)?, iov_len=(\d+)', args):
            if match[2] or not match[1].startswith('"'):
                raise ValueError('unavailable or truncated outgoing iovec')
            chunk = ast.literal_eval('b' + match[1])
            if len(chunk) != int(match[3]):
                raise ValueError('iovec printed length differs from declared length')
            chunks.append(chunk)
        if not chunks:
            raise ValueError('no decoded iovecs')
        data = b''.join(chunks)
    else:
        raise ValueError('this syscall needs a separate byte accounting rule: ' + name)
    if len(data) < size:
        raise ValueError('printed buffer shorter than accepted bytes')
    return data[:size]


def base_analysis(directory):
    rows, errors, manifests = records(directory)
    sockets, writes, ipc, transfers, lifecycle, unclassified, binds = {}, [], [], [], [], [], []
    for row in rows:
        name, args, ret = row['syscall'], row['args'], row['return']
        desc = descriptor(args)
        if name == 'socket' and ret is not None and ret >= 0:
            created = descriptor(row['result'])
            if 'inode' in created:
                sockets.setdefault(created['inode'], {'descriptor': created, 'connects': [], 'writes': []})['created'] = compact(row)
        if name == 'socketpair' and ret == 0:
            pair = [descriptor(match[0]) for match in FD.finditer(args)]
            if len(pair) != 2 or any('inode' not in item for item in pair):
                unclassified.append({**compact(row), 'reason': 'socketpair outputs unresolved', 'args': args})
            else:
                for index, created in enumerate(pair):
                    item = sockets.setdefault(created['inode'], {'descriptor': created, 'connects': [], 'writes': []})
                    item.update(created=compact(row), pair_inode=pair[1-index]['inode'])
        if name == 'bind' and ret == 0:
            binds.append({**compact(row), 'descriptor': desc, 'target': endpoint(args)})
        if name in ('accept', 'accept4') and ret is not None and ret >= 0:
            created = descriptor(row['result'])
            if 'inode' not in created:
                unclassified.append({**compact(row), 'reason': 'accepted descriptor unresolved', 'result': row['result']})
            else:
                sockets.setdefault(created['inode'], {'descriptor': created, 'connects': [], 'writes': []}).update(
                    created=compact(row), accepted_from=desc)
        if name == 'connect':
            target = endpoint(args)
            entry = {**compact(row), 'descriptor': desc, 'target': target, 'result': row['result']}
            if 'inode' in desc:
                sockets.setdefault(desc['inode'], {'descriptor': desc, 'connects': [], 'writes': []})['connects'].append(entry)
            else:
                unclassified.append({**entry, 'reason': 'connect descriptor unavailable'})
        if name in ('clone', 'clone3', 'fork', 'vfork', 'execve', 'execveat', 'exit', 'exit_group', 'setsid', 'dup', 'dup2', 'dup3', 'fcntl'):
            if name != 'fcntl' or 'F_DUPFD' in args:
                lifecycle.append({**compact(row), 'args': args})
        if name in TRANSFERS and 'SCM_RIGHTS' in args:
            transfers.append({**compact(row), 'descriptor': desc, 'args': args})
        if name not in WRITES or ret is None or ret <= 0:
            continue
        # splice/tee/copy_file_range's second decoded fd is its output fd.
        # Socket output still fails payload correlation until bytes are modeled.
        if name in ('splice', 'tee', 'copy_file_range'):
            descriptors = list(FD.finditer(args))
            if len(descriptors) != 2:
                unclassified.append({**compact(row), 'reason': 'multi-fd write descriptors unavailable', 'args': args})
                continue
            desc = descriptor(descriptors[1][0])
        entry = {**compact(row), 'descriptor': desc, 'args': args}
        writes.append(entry)
        if desc['kind'] == 'internet':
            item = sockets.setdefault(desc['inode'], {'descriptor': desc, 'connects': [], 'writes': []})
            item['writes'].append(entry)
            if target := endpoint(args):
                item.setdefault('send_addresses', []).append(target)
        elif desc['kind'] == 'ipc':
            ipc.append(entry)
            sockets.setdefault(desc['inode'], {'descriptor': desc, 'connects': [], 'writes': []})['writes'].append(entry)
        elif desc['kind'].startswith('unresolved'):
            unclassified.append({**entry, 'reason': 'successful write descriptor unresolved'})
    syscall_counts = Counter(row['syscall'] for row in rows)
    special = [compact(row) | {'args': row['args'], 'result': row['result']} for row in rows
               if row['syscall'].startswith('io_uring') or row['syscall'] in ('pidfd_getfd', 'bpf')]
    successful_special = [row for row in special if row['return'] is not None and row['return'] >= 0]
    svc = {int(row['address'], 16) + 4: row['address'] for row in json.loads((HERE / 'native-results.json').read_text())['svc_candidates']}
    direct = []
    for row in rows:
        try:
            ip = int(row['ip'], 16)
        except ValueError:
            continue
        if ip in svc:
            direct.append({**compact(row), 'svc_address': svc[ip], 'descriptor': descriptor(row['args'])})
    return {'rows': rows, 'parse_errors': errors, 'trace_files': manifests, 'sockets': sockets,
            'writes': writes, 'ipc': ipc, 'transfers': transfers, 'lifecycle': lifecycle,
            'unclassified': unclassified, 'special': special, 'successful_special': successful_special,
            'syscall_counts': dict(sorted(syscall_counts.items())), 'inline_main_elf': direct, 'binds': binds}


def public_base(data):
    return {'traced_tids': len(data['trace_files']), 'syscalls': sum(data['syscall_counts'].values()),
            'trace_bytes': sum(row['bytes'] for row in data['trace_files']), 'trace_files': data['trace_files'],
            'syscall_counts': data['syscall_counts'], 'parse_errors': data['parse_errors'],
            'positive_write_calls_by_descriptor': dict(Counter(row['descriptor']['kind'] for row in data['writes'])),
            'unclassified': data['unclassified'], 'descriptor_transfers': data['transfers'],
            'process_lifecycle': data['lifecycle'], 'special_mechanisms': data['special'],
            'inline_main_elf': data['inline_main_elf']}


def analyze_cli(directory):
    data = base_analysis(directory / 'trace')
    fixture = json.loads((directory / 'report.json').read_text())
    output = public_base(data)
    guard_path = directory / 'trace/guard.jsonl'
    if guard_path.exists():
        invocation = json.loads((directory / 'trace/invocation.json').read_text())
        guard_program = invocation['traced_command'][0]
        broker_tids = {row['tid'] for row in data['rows'] if row['syscall']=='execve' and row['return']==0
                       and row['args'].startswith(json.dumps(guard_program)+', ')}
        known_copy = [row for row in data['successful_special'] if row['tid'] in broker_tids
                      and row['syscall']=='pidfd_getfd' and '<anon_inode:seccomp notify>' in row['result']]
        data['successful_special'] = [row for row in data['successful_special'] if row not in known_copy]
        guard_rows = [json.loads(line) for line in guard_path.read_text().splitlines()]
        output['fixture_guard'] = {'broker_tids': sorted(broker_tids), 'sha256': invocation['guard_sha256'],
                                   'notification_count': sum('syscall' in row for row in guard_rows),
                                   'denied': [row for row in guard_rows if row.get('allowed') is False],
                                   'lifecycle': [row for row in guard_rows if 'event' in row],
                                   'listener_copies': known_copy}
    tunnels = fixture['transport']['tunnels']
    stream_results, inactive, matched, failures = [], [], set(), []
    for inode, item in data['sockets'].items():
        if item['descriptor']['kind'] != 'internet':
            continue
        row = {'inode': inode, 'protocol': item['descriptor']['protocol'], 'connects': item['connects'],
               'created': item.get('created'), 'write_calls': len(item['writes']),
               'write_tids': sorted({write['tid'] for write in item['writes']})}
        row['outgoing_bytes'] = sum(write['return'] for write in item['writes'])
        if not item['writes'] and not item['connects'] and not item.get('send_addresses'):
            row['classification'] = 'created-but-no-observed-connect-or-positive-send'
            inactive.append(row)
            continue
        expected_address = fixture['transport']['proxy_address']
        known_connect = (bool(item['connects']) and all(
            connection['target'] == {'family': 'AF_INET', 'address': expected_address[0], 'port': expected_address[1]}
            for connection in item['connects']))
        if not known_connect:
            failures.append('Internet socket does not have only fixture-proxy connect attempts: ' + inode)
        try:
            stream = b''.join(write_buffer(write) for write in item['writes'])
            header, tls = stream.split(b'\r\n\r\n', 1)
            request = header.split(b'\r\n')[0].decode()
            if not request.startswith('CONNECT ') or not request.endswith(' HTTP/1.1'):
                raise ValueError('stream has no supported CONNECT header')
            authority = request.split()[1]
            digest = hashlib.sha256(tls).hexdigest()
            candidates = [index for index, tunnel in enumerate(tunnels)
                          if tunnel['authority'] == authority and tunnel.get('client_sha256') == digest
                          and tunnel['client_bytes'] == len(tls)]
            row.update(authority=authority, connect_header_bytes=len(header)+4, tls_bytes=len(tls), tls_sha256=digest,
                       matched_tunnels=candidates)
            if len(candidates) != 1 or candidates[0] in matched:
                raise ValueError('no unique unused receiver tunnel with equal encrypted bytes')
            index = candidates[0]
            matched.add(index)
            row['client_after_upstream_eof_bytes'] = tunnels[index].get('client_after_upstream_eof_bytes', 0)
            requests = [request for request in fixture['requests']
                        if request.get('receiver_peer') == tunnels[index]['upstream_local']
                        and request.get('receiver_local') == tunnels[index]['upstream_peer']]
            row['requests'] = [{'host': request['host'], 'method': request['method'], 'path': request['path'],
                                'receiver_timestamp_ns': request['receiver_timestamp_ns']} for request in requests]
            if not requests:
                failures.append('Matched TLS tunnel has no decoded request: ' + inode)
        except (ValueError, SyntaxError) as error:
            row['error'] = str(error)
            failures.append('Socket ' + inode + ': ' + str(error))
        stream_results.append(row)
    if len(matched) != len(tunnels):
        failures.append('Unmatched receiver tunnels')
    if sum(len(row.get('requests', [])) for row in stream_results) != len(fixture['requests']):
        failures.append('Unmatched or duplicate plaintext receiver requests')
    if guard_path.exists() and (len(broker_tids)!=1 or len(known_copy)!=1):
        failures.append('Fixture guard broker/listener provenance is ambiguous')
    if data['parse_errors'] or data['unclassified'] or data['successful_special']:
        failures.append('Trace parse/descriptor/special-mechanism gaps require review')
    if not fixture['passed']:
        failures.append('Original CLI fixture acceptance failed')
    ipc_groups = []
    for inode, item in data['sockets'].items():
        if item['descriptor']['kind'] == 'ipc' and item['writes']:
            group = {'inode': inode, 'protocol': item['descriptor']['protocol'], 'connects': item['connects'],
                     'write_calls': len(item['writes']), 'accepted_bytes': sum(row['return'] for row in item['writes']),
                     'write_tids': sorted({row['tid'] for row in item['writes']})}
            for key in ('created', 'pair_inode', 'accepted_from'):
                if key in item:
                    group[key] = item[key]
            connected_bindings = [bind for bind in data['binds'] if any(bind['target'] == conn['target'] for conn in item['connects'])]
            if connected_bindings:
                group['target_bound_in_trace'] = connected_bindings
            if 'pair_inode' not in item and 'accepted_from' not in item and not item['connects']:
                failures.append('IPC writer lacks a recorded origin: ' + inode)
            ipc_groups.append(group)
    background_path = directory / 'background.json'
    if background_path.exists():
        background = json.loads(background_path.read_text())
        traced_tids = {row['tid'] for row in data['rows']}
        records = background['records']
        background['all_recorded_pids_traced'] = all(row['pid'] in traced_tids for row in records)
        background['privacy_states_expected'] = all(row['privacyReady'] == (fixture['mode'] == 'optional-off') for row in records)
        output['background'] = background
        if not all(background[key] for key in ('completed_before_cleanup', 'cleanup_verified', 'daemon_and_session_observed',
                                               'all_recorded_pids_traced', 'privacy_states_expected')):
            failures.append('Background process acceptance failed')
    output.update(internet_streams=stream_results, inactive_internet_sockets=inactive, ipc_streams=ipc_groups, receiver_tunnels=len(tunnels),
                  receiver_requests=len(fixture['requests']), model_requests=len(fixture['model']),
                  event_batches=fixture['event_batches'], tool_results=fixture['tool_results'],
                  fixture_passed=fixture['passed'], unmatched_receiver_tunnels=sorted(set(range(len(tunnels)))-matched),
                  passed=not failures, failures=failures)
    return output


def analyze_challenge(directory):
    data = base_analysis(directory / 'trace')
    receiver = json.loads((directory / 'receiver.json').read_text())
    output = public_base(data)
    tcp = [row for row in data['writes'] if row['descriptor'].get('protocol') == 'TCP']
    udp = [row for row in data['writes'] if row['descriptor'].get('protocol') == 'UDP']
    direct = [row for row in tcp if '"INLINE_SVC_WRITE\\n"' in row['args']]
    address = int(re.search(r'INLINE_WRITE_ADDRESS=(0x[0-9a-f]+)', (directory / 'trace/stdout.txt').read_text())[1], 16)
    assembly = (directory / 'inline-write.asm').read_text()
    function_va = int(re.search(r'([0-9a-f]+) <inline_write>', assembly)[1], 16)
    svc_va = int(re.search(r'^\s*([0-9a-f]+):\s+d4000001\s+svc', assembly, re.M)[1], 16)
    expected_pc = address + svc_va - function_va + 4
    checks = {
        'receiver_markers_and_order': receiver['receiver_passed'],
        'all_records_parsed': not data['parse_errors'],
        'descriptors_resolved': not data['unclassified'],
        'direct_svc_pc_matches_disassembly': len(direct) == 1 and int(direct[0]['ip'], 16) == expected_pc,
        'tcp_bytes_match': sum(row['return'] for row in tcp) == len(receiver['tcp']),
        'three_socket_writing_processes': len({row['tid'] for row in tcp}) == 3,
        'one_tcp_inode_through_ipc_and_exec': len({row['descriptor']['inode'] for row in tcp}) == 1,
        'vector_and_sendfile_seen': {'writev', 'sendfile'} <= {row['syscall'] for row in tcp},
        'scm_rights_send_and_receive_seen': {'sendmsg', 'recvmsg'} <= {row['syscall'] for row in data['transfers']},
        'exec_after_setsid_and_dup_seen': {'execve', 'setsid', 'dup3'} <= {row['syscall'] for row in data['lifecycle']},
        'udp_sendto_and_batch_seen': {'sendto', 'sendmmsg'} == {row['syscall'] for row in udp},
        'no_successful_unmodeled_special_io': not data['successful_special'],
    }
    udp_bytes = 0
    for row in udp:
        if row['syscall'] == 'sendmmsg':
            sizes = [int(size) for size in re.findall(r'msg_len=(\d+)', row['args'])]
            if len(sizes) < row['return']:
                checks['batch_lengths_present'] = False
            udp_bytes += sum(sizes[:row['return']])
        else:
            udp_bytes += row['return']
    checks['udp_bytes_match'] = udp_bytes == sum(len(row['data']) for row in receiver['udp'])
    output.update(checks=checks, passed=all(checks.values()), expected_inline_pc=hex(expected_pc),
                  tcp_writes=tcp, udp_writes=udp, tcp_bytes=len(receiver['tcp']), udp_bytes=udp_bytes,
                  receiver_sha256=sha(directory / 'receiver.json'))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Use a new output file')
    report = {'schema': 1, 'analyzed_utc': datetime.now(timezone.utc).isoformat(),
              'run_results_sha256': sha(args.input / 'results.json'),
              'challenge': analyze_challenge(args.input / 'challenge'), 'cases': {},
              'production_changes': False}
    run_results = json.loads((args.input / 'results.json').read_text())
    report['guard_negative'] = run_results.get('guard_negative')
    for name in run_results['cases']:
        report['cases'][name] = analyze_cli(args.input / name)
        row = report['cases'][name]
        print(json.dumps({'case': name, 'passed': row['passed'], 'tids': row['traced_tids'],
                          'streams': len(row['internet_streams']), 'requests': row['receiver_requests'],
                          'parse_errors': len(row['parse_errors']), 'unclassified': len(row['unclassified']),
                          'failures': row['failures']}), flush=True)
    report['passed'] = report['challenge']['passed'] and all(row['passed'] for row in report['cases'].values())
    if report['guard_negative']:
        report['passed'] = report['passed'] and report['guard_negative']['passed']
    report['limits'] = [
        'A finite synthetic scenario matrix is not coverage of every runtime branch or a no-egress proof.',
        'strace changes scheduling/timing and ptrace is detectable; these tests do not rule out observer-dependent behavior.',
        'Socket identity comes from strace descriptor/inode decoding. Inode reuse over long traces and multiple incoming writers need separate analysis.',
        'TLS ciphertext is matched byte-for-byte by length and SHA256 at the tunnel boundary; plaintext classification comes from the local TLS receiver.',
        'Local IPC, pipes and shared files may feed services outside the traced descendant tree; their later behavior is outside this experiment.',
        'No real account, model service, remote-control session, arbitrary MCP/plugin or full gateway server is exercised.',
        'Successful io_uring/pidfd_getfd/bpf or unparsed/multi-FD sends fail acceptance until additional modeling exists.',
        'Guarded fixtures intentionally reject real SSH execution, external IP destinations and the system DNS proxy; they cannot validate the successful external helper branch.',
        'The research guard uses seccomp user-notification CONTINUE after inspecting pointer arguments: TOCTOU and arbitrary IPC/file delegation remain, so it is not a hostile-code sandbox or a production privacy control.',
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    if not report['passed']:
        raise SystemExit('Trace correlation needs review; inspect output')


if __name__ == '__main__':
    main()
