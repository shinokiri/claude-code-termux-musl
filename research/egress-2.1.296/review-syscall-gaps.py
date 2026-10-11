#!/usr/bin/env python3
"""Verify the three version-specific gaps in the bounded syscall scan.

The two stat sites share an x8=79 definition across a test-bit branch. The
seccomp site's x0=277 definition precedes a long straight-line filter setup.
This validates the stated paths, not every possible incoming indirect branch.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess

HERE = Path(__file__).resolve().parent


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=Path.home() / '.local/share/claude-termux/current/runtime/claude')
    parser.add_argument('--scan', type=Path, required=True)
    parser.add_argument('--context', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    inventory = json.loads(args.scan.read_text())
    context = json.loads(args.context.read_text())
    unresolved = {int(row['address'], 16) for row in context['sites'] if row['inference'] is None}
    require(unresolved == {0x2e21d00, 0x2e21e60, 0x18bb9a4}, 'Unexpected unresolved sites')
    with args.binary.open('rb') as stream:
        require(hashlib.file_digest(stream, 'sha256').hexdigest() == inventory['scanned_sha256'], 'Unreviewed ELF')
    spec = importlib.util.spec_from_file_location('context', HERE / 'syscall-context.py')
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)

    def read(address, length):
        segment = next(row for row in inventory['segments'] if int(row['address'], 16) <= address
                       and address + length <= int(row['address'], 16) + row['file_bytes'])
        with args.binary.open('rb') as stream:
            stream.seek(segment['file_offset'] + address - int(segment['address'], 16))
            return stream.read(length)

    def decode(addresses):
        words = [read(address, 4) for address in addresses]
        encoded = '\n'.join(' '.join(f'0x{byte:02x}' for byte in word) for word in words) + '\n'
        result = subprocess.run(['llvm-mc', '--disassemble', '--triple=aarch64'], input=encoded,
                                text=True, capture_output=True, check=True)
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        require(not result.stderr and len(lines) == len(addresses), 'Decoder mismatch')
        return [{'address': hex(address), 'bytes': word.hex(), 'assembly': asm}
                for address, word, asm in zip(addresses, words, lines)]

    branch_address = 0x2e21cf8
    branch = struct.unpack('<I', read(branch_address, 4))[0]
    displacement = (branch >> 5) & 0x3fff
    if displacement & 0x2000:
        displacement -= 0x4000
    destination = branch_address + displacement * 4
    bit = ((branch >> 19) & 31) | ((branch >> 31) << 5)
    require(branch & 0x7f000000 == 0x37000000 and branch & 31 == 4 and bit == 8
            and destination == 0x2e21e5c, 'Unexpected test-bit branch')
    common = [0x2e21cec, 0x2e21cf0, 0x2e21cf4, branch_address]
    paths = [
        (common + [0x2e21cfc, 0x2e21d00], False),
        (common + [0x2e21e5c, 0x2e21e60], True),
    ]
    reviewed = []
    for addresses, taken in paths:
        instructions = decode(addresses)
        straight = [row for row in instructions if int(row['address'], 16) != branch_address]
        inferred = helpers.infer(straight, 8)
        require(inferred is not None and inferred['number'] == 79, 'x8=79 not preserved on reviewed path')
        require(instructions[-1]['assembly'] == 'svc\t#0', 'Expected SVC')
        reviewed.append({'address': hex(addresses[-1]), 'number': 79, 'names': ['newfstatat'],
                         'method': 'explicit test-bit branch path preserves x8=79',
                         'branch_taken': taken, 'branch_address': hex(branch_address),
                         'branch_target': hex(destination), 'instructions': instructions})
    addresses = list(range(0x18bb7f0, 0x18bb9a4 + 4, 4))
    instructions = decode(addresses)
    inferred = helpers.infer(instructions, 0)
    require(inferred is not None and inferred['number'] == 277, 'x0=277 not preserved through filter setup')
    word = struct.unpack('<I', read(addresses[-1], 4))[0]
    offset = word & 0x3ffffff
    if offset & 0x2000000:
        offset -= 0x4000000
    require(word & 0xfc000000 == 0x94000000 and addresses[-1] + 4 * offset == 0x4d056a0,
            'Expected syscall PLT call')
    reviewed.append({'address': hex(addresses[-1]), 'number': 277, 'names': ['seccomp'],
                     'method': 'long straight-line x0=277 filter setup', 'number_load': inferred['number_load'],
                     'instruction_count': len(instructions),
                     'bytes_sha256': hashlib.sha256(b''.join(bytes.fromhex(row['bytes']) for row in instructions)).hexdigest(),
                     'instructions': instructions})
    result = {'schema': 1, 'scanned_sha256': inventory['scanned_sha256'], 'reviewed': reviewed,
              'passed': True, 'remaining_number_gaps_in_enumerated_sites': 0,
              'limits': ['The reviewed paths provide call-number evidence; they do not prove every incoming control-flow path or runtime execution.',
                         'No indirect calls, dynamically generated code, loaded libraries, socket identity or payload semantics are resolved by this follow-up.']}
    require(not args.output.exists(), 'Use a new output file')
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'passed': True, 'reviewed': len(reviewed), 'remaining_number_gaps': 0}))


if __name__ == '__main__':
    main()
