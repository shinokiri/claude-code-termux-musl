#!/usr/bin/env python3
"""Build an instrumented research copy; never replace an installed library."""
from pathlib import Path
import difflib
import hashlib
import json
import os
import shutil
import subprocess
import tarfile

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ['NATIVE_RESEARCH_REPO'])
ROOT = Path(os.environ['NATIVE_RESEARCH_ROOT'])
source = HERE / 'source'
source.mkdir(exist_ok=False)
header = HERE / 'runtime-trace.h'
shutil.copy2(header, source / header.name)
edits = {
    'termux-musl-dns.c': [
        ('#include <unistd.h>', '#include <unistd.h>\n#include "runtime-trace.h"'),
        ('                struct addrinfo **result) {',
         '                struct addrinfo **result) {\n    native_trace_record("getaddrinfo", __builtin_return_address(0));'),
    ],
    'termux-musl-resolver.c': [
        ('#include <unistd.h>', '#include <unistd.h>\n#include "runtime-trace.h"'),
        ('FILE *fopen(const char *path, const char *mode) {',
         'FILE *fopen(const char *path, const char *mode) {\n    if (path && !strcmp(path, "/etc/resolv.conf")) native_trace_record("resolv-conf", __builtin_return_address(0));'),
        ('    int type = 0;\n    socklen_t type_size = sizeof(type);',
         '    native_trace_record("dns-sentinel-connect", __builtin_return_address(0));\n    int type = 0;\n    socklen_t type_size = sizeof(type);'),
    ],
}
hashes = {}
patches = []
for name, changes in edits.items():
    original = (REPO / 'libexec' / name).read_text()
    changed = original
    for old, new in changes:
        assert changed.count(old) == 1
        changed = changed.replace(old, new)
    (source / name).write_text(changed)
    hashes[name] = {'original': hashlib.sha256(original.encode()).hexdigest(),
                    'instrumented': hashlib.sha256(changed.encode()).hexdigest()}
    patches.extend(difflib.unified_diff(original.splitlines(True), changed.splitlines(True),
                                      fromfile='libexec/' + name, tofile='research/' + name))
(HERE / 'instrumentation.patch').write_text(''.join(patches))
archive = Path(os.environ['NATIVE_RESEARCH_HEADERS'])
assert hashlib.sha256(archive.read_bytes()).hexdigest() == 'ca72b84f4eb3c36bbbec04d919eb10159d414c7a43413cc2253841d301fa9486'
headers = HERE / 'headers'
headers.mkdir(exist_ok=False)
with tarfile.open(archive, 'r:*', ignore_zeros=True) as stream:
    for entry in stream:
        path = Path(entry.name)
        if not entry.name.startswith('usr/include/'):
            continue
        assert not path.is_absolute() and '..' not in path.parts and (entry.isfile() or entry.isdir())
        if entry.isfile():
            target = headers / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with stream.extractfile(entry) as data:
                target.write_bytes(data.read())
resources = subprocess.check_output(['clang', '-print-resource-dir'], text=True).strip()
(HERE / 'privacy-mode').write_text('standard\n')
(HERE / 'release.lease').touch(mode=0o600)
output = HERE / 'libtermux-musl-compat.so'
command = ['clang', '--target=aarch64-linux-musl', '-nostdlib', '-nostdinc',
           '-isystem', resources + '/include', '-isystem', str(headers / 'usr/include'),
           '-O2', '-fPIC', '-shared', '-Wall', '-Wextra', '-Werror',
           '-Wl,-soname,libtermux-musl-compat.so',
           '-DTERMUX_PREFIX=' + json.dumps(os.environ['PREFIX']),
           '-DBIONIC_EXEC_LIBRARY=' + json.dumps(str(ROOT / 'libtermux-bionic-exec.so')),
           '-DPRIVACY_MODE_FILE=' + json.dumps(str(HERE / 'privacy-mode')),
           '-DPRIVACY_PRELOAD=' + json.dumps(str(ROOT.parent / 'libexec/termux-privacy.mjs')),
           '-DCLAUDE_BINARY=' + json.dumps(str(ROOT / 'claude')),
           '-DRELEASE_LEASE=' + json.dumps(str(HERE / 'release.lease')),
           '-o', str(output), str(REPO / 'libexec/termux-exec.c'),
           str(REPO / 'libexec/termux-privacy.c'), str(source / 'termux-musl-dns.c'),
           str(source / 'termux-musl-resolver.c')]
subprocess.run(command, check=True)
record = {'source_hashes': hashes, 'observer_sha256': hashlib.sha256(header.read_bytes()).hexdigest(),
          'library_sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
          'runtime': str(ROOT), 'command': command, 'production_modified': False}
(HERE / 'build.json').write_text(json.dumps(record, indent=2) + '\n')
print('Research library built', record['library_sha256'])
