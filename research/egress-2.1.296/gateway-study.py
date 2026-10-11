#!/usr/bin/env python3
"""Replay gateway components against loopback receivers, without an account."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading

HERE = Path(__file__).resolve().parent
PATCHED = '0563350c19d156e3c4e1595f9524185de7d948543ac5edbd5c6b9140e376a06d'


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.home() / '.local/share/claude-termux/current/runtime')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    if sha(root / 'claude') != PATCHED or output.exists():
        raise RuntimeError('Requires pinned installed binary and new output directory')
    if any(c.isspace() for c in str(HERE)):
        raise RuntimeError('Pinned Bun preload parser requires a path without whitespace')
    output.mkdir(parents=True, mode=0o700)
    rows, result = [], {'schema': 1, 'started_utc': datetime.now(timezone.utc).isoformat(), 'modes': {}}

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            status = 500 if '/fail/' in self.path else 400 if '/refuse/' in self.path else 200
            rows.append({'path': self.path, 'bytes': len(body), 'body_sha256': hashlib.sha256(body).hexdigest(),
                         'headers': {key.lower(): value for key, value in self.headers.items()},
                         'status': status, 'peer': self.client_address[0]})
            self.send_response(status)
            self.send_header('Content-Length', '0')
            self.end_headers()

    receiver = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
    threading.Thread(target=receiver.serve_forever, daemon=True).start()
    try:
        for mode in ('standard', 'optional-off'):
            start = len(rows)
            report = output / (mode + '.json')
            with tempfile.TemporaryDirectory(prefix='gateway-research-', dir=output) as temporary:
                run = Path(temporary)
                (run / 'home').mkdir()
                env = {key: os.environ[key] for key in ('PATH', 'PREFIX', 'ANDROID_ROOT', 'ANDROID_DATA') if key in os.environ}
                env.update(HOME=str(run / 'home'), TMPDIR=str(run), CLAUDE_CONFIG_DIR=str(run / 'config'),
                           CLAUDE_CODE_TMPDIR=str(run), CLAUDE_TERMUX_AUTO_UPDATE='0',
                           CLAUDE_TERMUX_PRIVACY=mode, BUN_OPTIONS='--preload ' + str(HERE / 'gateway-probe.mjs'),
                           GATEWAY_RESEARCH_FIXTURE='loopback-only-v1', GATEWAY_RESEARCH_REPORT=str(report),
                           GATEWAY_RESEARCH_PORT=str(receiver.server_address[1]))
                child = subprocess.Popen([str(root / 'claude'), '--version'], env=env, cwd=run,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
                try:
                    stdout, stderr = child.communicate(timeout=80)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    stdout, stderr = child.communicate()
                    raise RuntimeError('Owned component fixture timed out')
                finally:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            (output / (mode + '.stderr')).write_text(stderr)
            (output / (mode + '.stdout')).write_text(stdout)
            observed = rows[start:]
            data = json.loads(report.read_text()) if report.exists() else {'passed': False, 'error': stderr[-2000:]}
            expected = [call for case in data.get('cases', []) for call in case['calls']]
            wire_matches = Counter((row['path'], row['body_sha256']) for row in observed) == Counter((row['path'], row['body_sha256']) for row in expected)
            expected_by_path = {row['path']: row for row in expected}
            headers_match = all(all(row['headers'].get(key) == value for key, value in expected_by_path[row['path']]['headers'].items()) for row in observed)
            data.update(exit_code=child.returncode, receiver_requests=len(observed), receiver_matches=wire_matches,
                        receiver_headers_match=headers_match, peers_loopback=all(row['peer'] == '127.0.0.1' for row in observed))
            data['passed'] = bool(data['passed'] and child.returncode == 0 and wire_matches and headers_match and data['peers_loopback'])
            result['modes'][mode] = data
            print(json.dumps({'mode': mode, 'passed': data['passed'], 'cases': len(data.get('cases', [])),
                              'requests': len(observed), 'error': data.get('error')}), flush=True)
    finally:
        receiver.shutdown()
        receiver.server_close()
    result.update(finished_utc=datetime.now(timezone.utc).isoformat(), patched_sha256=PATCHED,
                  passed=all(value['passed'] for value in result['modes'].values()), production_modified=False,
                  original_gateway_server_started=False, receiver=rows,
                  limits=[
                      'Original hash-pinned class/functions/route fragment are re-evaluated with explicit fixture dependencies; they are not called through the live gateway module binding.',
                      'The original Is safe-fetch/SSRF/DNS/TLS helper, logger and Google credential provider are replaced; actual fetch only targets a loopback receiver.',
                      'Postgres, OIDC/session admission, full server startup, managed-settings delivery and external collectors are not exercised.',
                      'optional-off comparison verifies initialized client protection permits these component requests; it does not prove every gateway server path is unchanged.',
                      'No real account, credentials, model endpoint or remote-control session is used.',
                  ])
    if sha(root / 'claude') != PATCHED:
        raise RuntimeError('Production ELF changed')
    (output / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
    if not result['passed']:
        raise SystemExit('Gateway component study failed; inspect output')


if __name__ == '__main__':
    main()
