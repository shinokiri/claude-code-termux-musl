#!/usr/bin/env python3
"""Compare bundled reporters against local receivers; no account or model calls."""
import argparse
from collections import Counter
import http.server
import json
import os
from pathlib import Path
import select
import subprocess
import tempfile
import threading
from fixture_runtime import environment
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    runs = root / "tests"
    runs.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="privacy-", dir=runs))
    observed = []
    state = {"mode": "standard"}

    class Receiver(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                body = self.rfile.read(length)
            elif self.headers.get("Transfer-Encoding") == "chunked":
                chunks = []
                while True:
                    length = int(self.rfile.readline().strip().split(b";")[0], 16)
                    if not length:
                        self.rfile.readline()
                        break
                    chunks.append(self.rfile.read(length))
                    self.rfile.read(2)
                body = b"".join(chunks)
            else:
                body = b""
            observed.append({"mode": state["mode"], "path": urlsplit(self.path).path, "bytes": len(body)})
            payload = b"" if self.headers.get("Content-Type") == "application/x-protobuf" else b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_POST

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    results = {}
    try:
        for mode in ("standard", "optional-off"):
            state["mode"] = mode
            directory = run / mode
            directory.mkdir()
            h2log = directory / "http2.jsonl"
            h2env = environment(directory)
            h2 = subprocess.Popen(["node", str(REPO / "tests/privacy-receiver.cjs"), str(h2log)], env=h2env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                if not select.select([h2.stdout], [], [], 10)[0]:
                    raise RuntimeError("Local HTTP/2 receiver timed out")
                port = h2.stdout.readline().strip()
                if not port.isdigit():
                    raise RuntimeError("Local HTTP/2 receiver failed")
                env = dict(h2env)
                # This probe enters the musl ELF directly, without the public
                # launcher that normally strips Bionic loader variables.
                env.pop("LD_PRELOAD", None)
                env.pop("LD_LIBRARY_PATH", None)
                proxy = "http://127.0.0.1:" + str(server.server_port)
                env.update(CLAUDE_TERMUX_PRIVACY=mode, CLAUDE_CONFIG_DIR=str(directory / "config"), HOME=str(directory / "home"),
                           TMPDIR=str(directory), CLAUDE_CODE_TMPDIR=str(directory),
                           DISABLE_AUTOUPDATER="1", DISABLE_UPDATES="1", HTTP_PROXY=proxy, HTTPS_PROXY=proxy,
                           http_proxy=proxy, https_proxy=proxy, NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost",
                           PRIVACY_HTTP_PORT=str(server.server_port), PRIVACY_H2_PORT=port, PRIVACY_TEST_MODE=mode, CLAUDE_AX_SCREEN_READER="1",
                           PRIVACY_REPORT=str(directory / "result.json"),
                           BUN_OPTIONS="--preload " + str(REPO / "tests/privacy-probe.mjs"))
                process = subprocess.run([str(root / "claude"), "--version"], env=env, cwd=directory,
                                         capture_output=True, text=True, timeout=85)
                (directory / "stderr.txt").write_text(process.stderr)
                report = json.loads((directory / "result.json").read_text()) if (directory / "result.json").exists() else {}
                results[mode] = {"exit": process.returncode, **report}
                if process.returncode:
                    print(json.dumps({"mode": mode, "report": report, "stderr": process.stderr[-1800:]}), flush=True)
            finally:
                h2.stdin.close()
                try:
                    h2.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    h2.kill()
                    h2.wait()
            results[mode]["http2_received"] = [json.loads(x) for x in h2log.read_text().splitlines()] if h2log.exists() else []
    finally:
        server.shutdown()
        server.server_close()
    failures = []
    for mode, result in results.items():
        if result["exit"] or any(c.get("error") for c in result.get("cases", [])):
            failures.append(mode + " probe failed")
        http_received = [r for r in observed if r["mode"] == mode]
        expected_http = {"/v1/messages": 3, "/api/eval/fixture": 2,
                         "/api/frame/list": 2, "/v1/code/sessions/fixture/agent-proxy/frame/list": 2}
        if mode == "standard":
            expected_http.update({"/" + signal + transport: 1 for signal in ("log", "metric", "trace") for transport in ("Json", "Proto")})
            # Three direct event probes plus one original initializer flush:
            # the collector control now enables the real isolated config reader.
            expected_http.update({"/api/event_logging/v2/batch": 4, "/api/claude_code/metrics": 2,
                                  "/api/claude_cli_feedback": 2, "/plugin-telemetry": 2,
                                  "/api/frame/track": 2, "/v1/code/sessions/fixture/agent-proxy/frame/track": 2})
        expected_h2 = 14 if mode == "standard" else 5
        if Counter(r["path"] for r in http_received) != expected_http or len(result["http2_received"]) != expected_h2:
            failures.append(mode + " receiver counts differ")
        result["http_received"] = http_received
        if mode == "optional-off" and any(r["path"] not in expected_http for r in [*http_received, *result["http2_received"]]):
            failures.append("optional reporting reached receiver")
    output = {"passed": not failures, "failures": failures, "run": str(run), "results": results,
              "scope": "Synthetic direct components and transports, local receivers; not a proof of all egress or authenticated features."}
    (run / "report.json").write_text(json.dumps(output, indent=2) + "\n")
    if args.report:
        args.report.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps({"passed": not failures, "failures": failures, "run": str(run),
                      "counts": {m: {"http": len(r["http_received"]), "http2": len(r["http2_received"]), "cases": len(r.get("cases", []))} for m, r in results.items()}}, indent=2))
    return bool(failures)


if __name__ == "__main__":
    from fixture_runtime import enter
    enter()
    raise SystemExit(main())
