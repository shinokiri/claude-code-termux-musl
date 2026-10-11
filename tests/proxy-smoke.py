#!/usr/bin/env python3
"""Check authenticated CONNECT, Android DNS/TLS, and per-session proxy cleanup."""
import argparse
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
from termux_proxy import ensure_proxy

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "build/runtime"
URL = "https://registry.npmjs.org/@anthropic-ai/claude-code-linux-arm64-musl/2.1.296"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    root = args.root.resolve()
    with tempfile.TemporaryDirectory(prefix="proxy-test-", dir=root / "tmp") as directory:
        token = Path(directory) / "token"
        proxy = subprocess.Popen([str(ensure_proxy(root)), "--auth-file", str(token)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            if not select.select([proxy.stdout], [], [], 10)[0]:
                raise RuntimeError("proxy startup timed out")
            port = int(proxy.stdout.readline())
            address = "http://claude:" + token.read_text().strip() + "@127.0.0.1:" + str(port)
            command = ["curl", "-q", "--config", "-", "--noproxy", "", "--silent", "--show-error",
                       "--fail", "--connect-timeout", "10", "--max-time", "30", URL]
            result = subprocess.run(command, input='proxy = "' + address + '"\n', capture_output=True, text=True, timeout=35)
            if result.returncode:
                raise RuntimeError(result.stderr.replace(address, "[proxy]"))
            assert json.loads(result.stdout)["version"] == "2.1.296"
            unauthenticated = 'proxy = "http://127.0.0.1:' + str(port) + '"\n'
            result = subprocess.run(command, input=unauthenticated, capture_output=True, text=True, timeout=15)
            assert result.returncode != 0 and "407" in result.stderr, result.stderr
            assert token.stat().st_mode & 0o777 == 0o600
        finally:
            proxy.stdin.close()
            try:
                status = proxy.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait()
                raise RuntimeError("proxy did not exit after launcher pipe closed")
            proxy.stdout.close()
        assert status == 0
    report = {"authenticated_connect_dns_tls": "passed", "reject_missing_token": "passed", "parent_exit_cleanup": "passed"}
    (root / "proxy-smoke.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
