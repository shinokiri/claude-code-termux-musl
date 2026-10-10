#!/usr/bin/env python3
"""Check exec/PID/argv, nested wrappers and child DNS after the parent exits.

A tiny executable fixture replaces Claude here so process lifetime and network
ownership can be measured without an account. device-smoke tests real Claude.
"""
import argparse
import http.server
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

APP = Path(__file__).resolve().parents[1]


def wait_for(check, description, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.05)
    raise RuntimeError("Timed out: " + description)


def closed(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return False
    except OSError:
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=APP / ".runtime")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    (root / "tests").mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="wrapper-", dir=root / "tests"))
    fixture = run / "runtime"
    fixture.mkdir()
    for name in ("termux-shell", "termux-http-proxy"):
        (fixture / name).symlink_to(root / name)
    binary = fixture / "claude"
    binary.write_text("#!" + sys.executable + "\n" + r'''
import json, os
from pathlib import Path
import subprocess, sys, time
import urllib.request, urllib.parse
run = Path(os.environ["WRAPPER_TEST_RUN"])
mode = sys.argv[1]
report = {"pid": os.getpid(), "argv": sys.argv[2:],
          "sentinel": os.environ.get("WRAPPER_TEST_SENTINEL"),
          "port": urllib.parse.urlsplit(os.environ["HTTPS_PROXY"]).port,
          "preload_removed": "LD_PRELOAD" not in os.environ,
          "saved_preload": os.environ.get("CLAUDE_TERMUX_BIONIC_PRELOAD")}
if mode == "parent":
    with (run / "child.stderr").open("w") as errors:
        child = subprocess.Popen([os.environ["WRAPPER_TEST_ENTRY"], sys.argv[0], "child", *sys.argv[2:]],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=errors, start_new_session=True)
    report["child_pid"] = child.pid
    (run / "parent.json").write_text(json.dumps(report))
else:
    (run / "ready.json").write_text(json.dumps(report))
    deadline = time.monotonic() + 15
    while not (run / "parent-exited").exists():
        if time.monotonic() > deadline:
            sys.exit(10)
        time.sleep(.05)
    with urllib.request.urlopen(os.environ["WRAPPER_TEST_URL"], timeout=5) as response:
        report["network"] = response.read().decode()
    (run / "child.json").write_text(json.dumps(report))
''')
    binary.chmod(0o700)
    subprocess.run(["clang", "-O2", "-DCLAUDE_BINARY=" + json.dumps(str(binary)),
                    str(APP / "libexec/claude-dispatch.c"), "-o", str(fixture / "claude-dispatch")], check=True)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"CHILD_NETWORK_OK")

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("CLAUDE_", "ANTHROPIC_")) or key.lower().endswith("_proxy"):
            env.pop(key)
    env.update(WRAPPER_TEST_RUN=str(run), WRAPPER_TEST_SENTINEL="keep this env value",
               WRAPPER_TEST_URL="http://127.0.0.1:" + str(server.server_port),
               WRAPPER_TEST_ENTRY=str(APP / "bin/claude-process-wrapper"),
               CLAUDE_CONFIG_DIR=str(run / "config"), CLAUDE_CODE_TMPDIR=str(run / "tmp"),
               NO_PROXY="", no_proxy="")
    env["CLAUDE_TERMUX_BIONIC_PRELOAD"] = env.pop("LD_PRELOAD", "")
    env["CLAUDE_TERMUX_BIONIC_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH", "")
    tokens = ["argument with spaces", "--joined=one two", "--separate", "three", "", "☃"]
    child_pid = None
    parent = None
    try:
        parent = subprocess.Popen([str(APP / "bin/claude-process-wrapper"), str(binary), "parent", *tokens],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        out, err = parent.communicate(timeout=10)
        assert parent.returncode == 0, err
        assert out == "", "Wrapper wrote to stdout before exec"
        parent_report = json.loads((run / "parent.json").read_text())
        child_pid = parent_report["child_pid"]
        assert parent_report["pid"] == parent.pid, "Wrapper lost PID across exec"
        ready = wait_for(lambda: (run / "ready.json").exists(), "nested wrapper starts")
        child = json.loads((run / "ready.json").read_text())
        assert child["pid"] == child_pid
        assert parent_report["port"] != child["port"], "Child reused its parent's proxy"
        wait_for(lambda: closed(parent_report["port"]), "parent proxy cleanup")
        (run / "parent-exited").touch()
        wait_for(lambda: (run / "child.json").exists(), "child network after parent exit")
        child = json.loads((run / "child.json").read_text())
        assert child["network"] == "CHILD_NETWORK_OK"
        for value in (parent_report, child):
            assert value["argv"] == tokens
            assert value["sentinel"] == "keep this env value"
            assert value["preload_removed"]
            assert value["saved_preload"] == env["CLAUDE_TERMUX_BIONIC_PRELOAD"]
        wait_for(lambda: closed(child["port"]), "child proxy cleanup")
        report = {"pid_preserved": True, "argv_preserved": True, "inherited_environment": True,
                  "nested_wrapper": True, "child_network_after_parent_exit": True,
                  "both_proxies_cleaned_up": True, "real_daemon": False}
        (run / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    finally:
        server.shutdown()
        server.server_close()
        if parent and parent.poll() is None:
            parent.kill()
            parent.wait()
        if child_pid:
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    main()
