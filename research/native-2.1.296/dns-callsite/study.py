#!/usr/bin/env python3
"""Reproduce native DNS call sites using pinned, local-only Termux fixtures.

Requires the reviewed Claude 2.1.296 / adapter 1.7.2, clang and cached musl
headers. Builds a separate observer library; never changes the installation.
Full Ghidra/REA analysis is a separate experiment, not performed by this script.
"""
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import argparse
import hashlib
import json
import mmap
import os
import shutil
import struct
import subprocess
import sys

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
ORIGINAL_SHA256 = 'b0bbc936a82930933d7d5645081050d5f827a946cf6abc6575383b777a0feec0'
TEXT_SHA256 = '270a16c4fb9a0db57546bc5b0cbdf721e59285f16a8f546ffba827fb82842f00'
HEADERS_SHA256 = 'ca72b84f4eb3c36bbbec04d919eb10159d414c7a43413cc2253841d301fa9486'
SOURCE_HASHES = {
    'libexec/termux-exec.c': 'dc300260b17d6fb76d96a0882867348473e3855bc8b8639ce1dd3dbb99ac8b9a',
    'libexec/termux-privacy.c': '18772877aef578f3b08b551bc5f94ce9c2d515548e726ec2f8b6c3bcbb5a4af2',
    'libexec/termux-musl-dns.c': '77c5937ae3001c0ad91076313fcd6ae8a04648bc28c3c13ce1c2b9f65c8dd84c',
    'libexec/termux-musl-resolver.c': 'c4e2ce2095a5758f7931a3bd18eb0899b2223866950ca0a0d0faa9f490b79bb7',
    'tests/dns-smoke.py': '6385afcb1d1a4fef084ad1094df24fb31c7d7bf55552a8784fa4957d8755872a',
    'tests/dns-probe.mjs': 'cef91a55d8ef167aec7e8b992b78c8d01ec4b40fde5e3460494f381dad191c7c',
    'tests/dns-worker.mjs': 'b3f1dca75e92a3aedf1e7d1a8a5aa223ebc8a3a30a918ac1e18af7e8d337356e',
    'tests/network-smoke.py': 'b213eaa15be42298e9276040cc5f3c16666320db9fc017a6928c689c51954319',
    'tests/network-probe.mjs': '2225e5dddba6757eb473d6dfcf0a154d9256404aafe508c2d378ad04da205e94',
    'tests/tls-cert.c': '980be0974dab3a8010aa1b27bfc35938321c151cf1e497d5973c746752c1eaca',
}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def inspect_text(binary):
    """Validate ET_EXEC addresses and reproduce the bounded direct-branch scan."""
    reference = json.loads((HERE / 'direct-branches.json').read_text())
    targets = {int(address, 16): name for address, name in reference['targets'].items()}
    with binary.open('rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        header = struct.unpack_from('<16sHHIQQQIHHHHHH', data)
        require(header[0][:6] == b'\x7fELF\x02\x01' and header[1:3] == (2, 183),
                'Expected an AArch64, little-endian ET_EXEC ELF')
        offset, stride, count, names_index = header[6], header[11], header[12], header[13]
        require(stride == 64 and 0 < names_index < count, 'Unsupported section table')
        sections = [struct.unpack_from('<IIQQQQIIQQ', data, offset + i * stride) for i in range(count)]
        names_section = sections[names_index]
        names = data[names_section[4]:names_section[4] + names_section[5]]
        matching = [s for s in sections if names[s[0]:].split(b'\0', 1)[0] == b'.text']
        require(len(matching) == 1, 'Expected exactly one .text section')
        section = matching[0]
        va, file_offset, size = section[3:6]
        text = data[file_offset:file_offset + size]
        require(va == 0x178a000 and size == 56079452 and hashlib.sha256(text).hexdigest() == TEXT_SHA256,
                'Executable code differs from the reviewed original .text')
        candidates = []
        for index, (word,) in enumerate(struct.iter_unpack('<I', text)):
            if word & 0x7c000000 != 0x14000000:
                continue
            immediate = word & 0x3ffffff
            if immediate & 0x2000000:
                immediate -= 0x4000000
            address = va + index * 4
            target = address + immediate * 4
            if target in targets:
                candidates.append((hex(address), hex(target), targets[target]))
        expected = [(c['address'], c['target'], c['target_name']) for c in reference['candidates']]
        require(candidates == expected, 'Direct-branch inventory differs from the reviewed result')
        decoded = 0
        for line in (HERE / 'runtime-evidence.jsonl').read_text().splitlines():
            row = json.loads(line)
            for instruction in row.get('instructions', []):
                index = int(instruction['address'], 16) - va
                require(text[index:index + instruction['length']].hex() == instruction['bytes'],
                        'Saved Ghidra instruction bytes do not match this ELF')
                decoded += 1
    return {'elf_type': 'ET_EXEC', 'text_address': hex(va), 'text_sha256': TEXT_SHA256,
            'text_bytes': size, 'text_file_offset': file_offset,
            'direct_branches_reproduced': len(candidates), 'saved_ghidra_instructions_verified': decoded,
            'address_rule': 'For this ET_EXEC, return_address minus 4 is the ELF call-site VA; do not subtract dli_fbase.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='New directory for the research library and fixture reports')
    parser.add_argument('--root', type=Path, default=Path.home() / '.local/share/claude-termux/current/runtime')
    parser.add_argument('--musl-headers', type=Path, default=REPO / '.runtime/downloads/musl-dev.apk',
                        help='Verified musl-dev-1.2.6-r2.apk from Alpine v3.24/aarch64; no automatic download')
    args = parser.parse_args()
    root, output, headers = args.root.resolve(), args.output.resolve(), args.musl_headers.resolve()
    require(not any(c.isspace() for path in (root, output, HERE) for c in str(path)),
            'The pinned Bun preload parser requires paths without whitespace')
    require(not output.exists() and not output.is_relative_to(root.parent),
            'Use a new output directory outside the installed release')
    require(bool(os.environ.get('PREFIX')), 'Run this study in Termux with PREFIX set')
    runtime = json.loads((root / 'runtime.json').read_text())
    adapter = json.loads((root.parent / 'adapter.json').read_text())
    require(runtime['version'] == '2.1.296' and adapter['version'] == '1.7.2'
            and runtime['original_sha256'] == ORIGINAL_SHA256,
            'This study requires the reviewed Claude 2.1.296 / adapter 1.7.2')
    patched_sha256 = digest(root / 'claude')
    require(patched_sha256 == runtime['patched_sha256'], 'Installed runtime digest mismatch')
    for name, sha256 in SOURCE_HASHES.items():
        require(digest(REPO / name) == sha256, 'Pinned fixture/source changed: ' + name)
    require(digest(headers) == HEADERS_SHA256, 'Unexpected musl header archive')
    identity = inspect_text(root / 'claude')
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in ('runtime-trace.h', 'prepare-trace.py', 'run-probes.py', 'run-single-apis.py', 'network-single.mjs'):
        shutil.copy2(HERE / name, output / name)
    environment = {key: os.environ[key] for key in ('PATH', 'PREFIX', 'ANDROID_ROOT', 'ANDROID_DATA') if key in os.environ}
    environment.update(NATIVE_RESEARCH_REPO=str(REPO), NATIVE_RESEARCH_ROOT=str(root),
                       NATIVE_RESEARCH_HEADERS=str(headers))
    started = datetime.now(timezone.utc).isoformat()
    for name in ('prepare-trace.py', 'run-probes.py', 'run-single-apis.py'):
        subprocess.run([sys.executable, str(output / name)], env=environment, check=True, timeout=190)
    combined = json.loads((output / 'runtime-observations.json').read_text())
    singles = json.loads((output / 'single-api-observations.json').read_text())
    expected = {'fetch': {0x1f77598, 0x1f77684}, 'node-https': {0x1dea84c},
                'wss': {0x1f77598, 0x1f77684}, 'wrong-certificate': {0x1f77598, 0x1f77684}}
    cases = {}
    for name, result in singles.items():
        rows = result['trace']
        require(all(row['main_binary'] and row['api'] == 'getaddrinfo' for row in rows),
                'Unexpected source for observed address lookup')
        addresses = {int(row['return_address'], 16) - 4 for row in rows}
        require(addresses == expected[name], 'Cold-process API call sites changed: ' + name)
        cases[name] = {'passed': True, 'getaddrinfo_call_sites': sorted(map(hex, addresses))}
    traces = {}
    for name in ('dns', 'network'):
        rows = [json.loads(line) for line in (output / (name + '-trace.jsonl')).read_text().splitlines()]
        require(all(row['main_binary'] for row in rows), 'Observed call outside the pinned main ELF')
        counts = Counter((row['api'], hex(int(row['return_address'], 16) - 4)) for row in rows)
        traces[name] = [{'api': api, 'call_site': pc, 'count': count} for (api, pc), count in sorted(counts.items())]
    require({(r['api'], r['call_site']) for r in traces['dns']} ==
            {('resolv-conf', '0x2a9eff4'), ('dns-sentinel-connect', '0x2aaf558')}, 'DNS fixture call sites changed')
    require(digest(root / 'claude') == patched_sha256, 'Installed ELF changed during the study')
    report = {'schema': 1, 'started_utc': started, 'finished_utc': datetime.now(timezone.utc).isoformat(),
              'passed': True, 'original_sha256': ORIGINAL_SHA256, 'patched_sha256': patched_sha256,
              'identity': identity, 'source_hashes': SOURCE_HASHES, 'musl_headers_sha256': HEADERS_SHA256,
              'library_sha256': digest(output / 'libtermux-musl-compat.so'),
              'combined_fixture_exit_codes': {name: value['exit_code'] for name, value in combined.items()},
              'trace_call_sites': traces, 'cold_process_cases': cases, 'production_modified': False,
              'limits': ['Only the exercised synthetic DNS/TLS paths are dynamically covered.',
                         'The observer changes timing and is absent from production.',
                         'A return address identifies the last link-setting call; intervening tail calls can retain it.',
                         'This is not a complete REA result, call graph, TLS fingerprint comparison or privacy audit.']}
    (output / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'passed': True, 'direct_branches': identity['direct_branches_reproduced'],
                      'cold_process_cases': len(cases), 'output': str(output)}))


if __name__ == '__main__':
    main()
