#!/usr/bin/env python3
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys

HERE = Path(__file__).resolve().parent
REPO = Path(os.environ['NATIVE_RESEARCH_REPO'])
ROOT = Path(os.environ['NATIVE_RESEARCH_ROOT'])
source = (HERE / 'network-fixture.py').read_text()
old = "str(APP / 'tests/network-probe.mjs')"
assert source.count(old) == 1
source = source.replace(old, repr(str(HERE / 'network-single.mjs')))
begin = source.index("        assert report['fetchStatus']")
end = source.index("        report['observed'] = seen", begin)
source = source[:begin] + '''        kind = report['kind']
        assert report['proxyEnvironment'] == []
        assert seen['dns'], 'fresh API process did not perform observed DNS'
        expected_host = 'wrong-certificate.dns-audit.invalid' if kind == 'wrong-certificate' else 'claude-network.invalid'
        assert all(host == expected_host for host in seen['dns'])
        assert seen['sni'] == [expected_host]
        if kind in ('fetch', 'node-https'):
            assert report['status'] == 200 and len(seen['http']) == 1
        elif kind == 'wss':
            assert report['connected'] and len(seen['http']) == 1 and seen['http'][0]['upgrade'] == 'websocket'
        elif kind == 'wrong-certificate':
            assert report['rejected'] and not seen['http']
        else:
            raise AssertionError('Unknown fixture kind')
        assert all(item['host'] == expected_host + ':' + str(server.server_port) for item in seen['http'])
''' + source[end:]
fixture = HERE / 'single-api-fixture.py'
fixture.write_text(source)
results = {}
base_env = {key: os.environ[key] for key in ('PATH', 'PREFIX', 'ANDROID_ROOT', 'ANDROID_DATA') if key in os.environ}
base_env.update(HOME=str(HERE / 'home'), NATIVE_DNS_TRACE_FIXTURE='isolated-dns-callsite-v1')
for kind in ('fetch', 'node-https', 'wss', 'wrong-certificate'):
    trace = HERE / (kind + '-single-trace.jsonl')
    assert not trace.exists()
    env = {**base_env, 'NATIVE_NETWORK_CASE': kind, 'NATIVE_DNS_TRACE_FILE': str(trace)}
    result = subprocess.run([sys.executable, str(fixture), '--root', str(ROOT), '--report', str(HERE / (kind + '-single-result.json'))],
                            env=env, text=True, capture_output=True, timeout=40)
    (HERE / (kind + '-single-stderr.txt')).write_text(result.stderr)
    rows = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    results[kind] = {'exit_code': result.returncode, 'trace': rows}
    print(json.dumps({kind: results[kind]}), flush=True)
    if result.returncode or not rows:
        print(result.stderr[-2000:]); break
(HERE / 'single-api-observations.json').write_text(json.dumps(results, indent=2) + '\n')
assert len(results) == 4 and all(not value['exit_code'] and value['trace'] for value in results.values())
