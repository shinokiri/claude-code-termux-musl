#!/usr/bin/env python3
"""Decode bounded syscall windows; retain unresolved numbers explicitly.

An immediate is reported only for a conservative straight-line suffix. This
does not prove all incoming control-flow paths use that number or identify the
kind of file descriptor written. Full decoded windows stay in --evidence.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def syscall_names():
    result = subprocess.run(['clang', '-dM', '-E', '-x', 'c', '-include', 'asm/unistd.h', '/dev/null'],
                            capture_output=True, text=True, check=True)
    definitions = dict(re.findall(r'^#define (__NR\w+) (.+)$', result.stdout, re.M))

    def value(name, seen=()):
        if name in seen:
            return None
        text = definitions.get(name, '').strip().strip('()')
        if re.fullmatch(r'[0-9]+', text):
            return int(text)
        if re.fullmatch(r'0x[0-9a-fA-F]+', text):
            return int(text, 16)
        if text in definitions:
            return value(text, seen + (name,))
        return None

    names = {}
    for name in definitions:
        if not name.startswith('__NR_') or name in ('__NR_syscalls', '__NR_arch_specific_syscall'):
            continue
        number = value(name)
        if number is not None:
            names.setdefault(number, []).append(name[5:])
    return {number: sorted(values) for number, values in names.items()}


def infer(window, register):
    allowed = {'mov', 'movk', 'add', 'sub', 'and', 'orr', 'eor', 'ldr', 'ldur', 'ldp',
               'str', 'stur', 'stp', 'adr', 'adrp', 'sxtw', 'uxtw', 'nop', 'cmp', 'cmn', 'tst',
               'strb', 'strh', 'sturh', 'stlr', 'neg', 'lsr', 'lsl', 'csel', 'csinc',
               'cset', 'csinv', 'madd'}
    mention = re.compile(r'\b[wx]' + str(register) + r'\b')
    immediate = re.compile(r'mov\s+[wx]' + str(register) + r',\s*#([0-9]+)$')
    for index in range(len(window) - 2, -1, -1):
        asm = window[index]['assembly'].split('//', 1)[0].strip()
        match = immediate.fullmatch(asm)
        if match:
            return {'number': int(match[1]), 'number_load': window[index]['address'],
                    'intervening_instructions': len(window) - index - 2,
                    'method': 'bounded straight-line immediate, incoming branches not proven'}
        if not asm or asm.split()[0] not in allowed or mention.search(asm):
            return None
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=Path.home() / '.local/share/claude-termux/current/runtime/claude')
    parser.add_argument('--scan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.evidence.exists():
        raise RuntimeError('Use new output files')
    inventory = json.loads(args.scan.read_text())
    if digest(args.binary) != inventory['scanned_sha256']:
        raise RuntimeError('Binary hash differs from inventory')
    sites = [{'address': row['address'], 'entry': 'SVC', 'register': 8} for row in inventory['svc_candidates']]
    sites += [{'address': row['address'], 'entry': 'syscall@plt', 'register': 0}
              for row in inventory['selected_direct_branches'] if row['import'] == 'syscall']
    windows, encoded = [], []
    with args.binary.open('rb') as binary:
        for site in sites:
            address = int(site['address'], 16)
            segment = next(row for row in inventory['segments'] if int(row['address'], 16) <= address < int(row['address'], 16) + row['file_bytes'])
            start = max(address - 128, int(segment['address'], 16))
            binary.seek(segment['file_offset'] + start - int(segment['address'], 16))
            raw = binary.read(address - start + 4)
            words = [raw[i:i + 4] for i in range(0, len(raw), 4)]
            encoded.extend(' '.join(f'0x{byte:02x}' for byte in word) for word in words)
            windows.append({'site': site, 'start': start, 'words': words})
    decoded = subprocess.run(['llvm-mc', '--disassemble', '--triple=aarch64'], input='\n'.join(encoded) + '\n',
                             capture_output=True, text=True, check=True)
    lines = [line.strip() for line in decoded.stdout.splitlines() if line.strip()]
    if decoded.stderr or len(lines) != len(encoded):
        raise RuntimeError('Decoder diagnostics or word/assembly mismatch: ' + decoded.stderr[:1000])
    names = syscall_names()
    rows, evidence, cursor = [], [], 0
    for item in windows:
        window = [{'address': hex(item['start'] + i * 4), 'bytes': word.hex(), 'assembly': lines[cursor + i]}
                  for i, word in enumerate(item['words'])]
        cursor += len(window)
        inference = infer(window, item['site']['register'])
        if inference:
            inference['names'] = names.get(inference['number'], [])
        evidence.append({**item['site'], 'instructions': window, 'inference': inference})
        rows.append({**item['site'], 'inference': inference,
                     'window_sha256': hashlib.sha256(b''.join(item['words'])).hexdigest()})
    summary = {}
    for kind in ('SVC', 'syscall@plt'):
        chosen = [row for row in rows if row['entry'] == kind]
        counts = Counter(row['inference']['number'] for row in chosen if row['inference'])
        summary[kind] = {'total': len(chosen), 'unresolved': sum(row['inference'] is None for row in chosen),
                         'number_candidates': {str(number): {'count': count, 'names': names.get(number, [])}
                                               for number, count in sorted(counts.items())}}
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2) + '\n')
    result = {'schema': 1, 'scanned_sha256': inventory['scanned_sha256'],
              'scan_sha256': digest(args.scan), 'decoded_evidence_sha256': digest(args.evidence),
              'decoder': subprocess.check_output(['llvm-mc', '--version'], text=True).splitlines()[0],
              'number_names_source': 'Local Termux AArch64 asm/unistd.h macros, expanded by clang',
              'summary': summary, 'sites': rows,
              'limits': ['Numbers describe the identified straight-line suffix only; incoming branches and reachability remain unproven.',
                         'LLVM MC window-relative branch destinations are not original ELF destinations; this study uses them only to reject control transfer.',
                         'Unresolved numbers require control/dataflow follow-up, not a guess from nearby unrelated instructions.',
                         'write/pwrite can target files or descriptors; no network or telemetry classification follows from their names alone.',
                         'No runtime calls or process-wide observation were performed by this static study.']}
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
