#!/usr/bin/env python3
"""Correlate synthetic DNS/TLS fixtures with native return addresses."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ['NATIVE_RESEARCH_REPO'])
ROOT = Path(os.environ['NATIVE_RESEARCH_ROOT'])
library = HERE / 'libtermux-musl-compat.so'
assert hashlib.sha256(library.read_bytes()).hexdigest() == json.loads((HERE / 'build.json').read_text())['library_sha256']
environment = {key: os.environ[key] for key in ('PATH', 'PREFIX', 'ANDROID_ROOT', 'ANDROID_DATA') if key in os.environ}
home = HERE / 'home'
home.mkdir(exist_ok=False)
environment.update(HOME=str(home), NATIVE_DNS_TRACE_FIXTURE='isolated-dns-callsite-v1')
reports = {}
for name in ('dns', 'network'):
    trace = HERE / (name + '-trace.jsonl')
    assert not trace.exists()
    env = {**environment, 'NATIVE_DNS_TRACE_FILE': str(trace)}
    if name == 'dns':
        command = [sys.executable, str(REPO / 'tests/dns-smoke.py'), '--root', str(ROOT),
                   '--binary', str(ROOT / 'claude'), '--library', str(library), '--privacy', 'standard',
                   '--report', str(HERE / 'dns-result.json')]
    else:
        source = (REPO / 'tests/network-smoke.py').read_text()
        old = '    try:\n        process = subprocess.run([str(APP / \'bin/claude-termux\'), \'--version\'],'
        new = ('    env.update(LD_PRELOAD=' + repr(str(library)) + ', CLAUDE_TERMUX_PRIVACY="standard")\n'
               '    try:\n        process = subprocess.run([str(args.root.resolve() / "claude"), "--version"],')
        assert source.count(old) == 1
        source = source.replace(old, new)
        runner = HERE / 'network-fixture.py'
        source = source.replace('APP = Path(__file__).resolve().parents[1]', 'APP = Path(' + repr(str(REPO)) + ')', 1)
        runner.write_text(source)
        command = [sys.executable, str(runner), '--root', str(ROOT), '--report', str(HERE / 'network-result.json')]
    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=65)
    (HERE / (name + '-stdout.txt')).write_text(result.stdout)
    (HERE / (name + '-stderr.txt')).write_text(result.stderr)
    rows = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    counts = {}
    for row in rows:
        key = row['api'] + '@' + row['return_address']
        counts[key] = counts.get(key, 0) + 1
    reports[name] = {'exit_code': result.returncode, 'trace_rows': len(rows), 'calls': counts,
                     'all_observed_in_main_binary': all(row['main_binary'] for row in rows)}
    print(json.dumps({name: reports[name]}), flush=True)
    if result.returncode != 0 or not rows:
        print(result.stderr[-3500:])
        break
(HERE / 'runtime-observations.json').write_text(json.dumps(reports, indent=2) + '\n')
raise SystemExit(0 if len(reports) == 2 and all(x['exit_code'] == 0 and x['trace_rows'] for x in reports.values()) else 1)
