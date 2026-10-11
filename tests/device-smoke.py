#!/usr/bin/env python3
"""Exercise the real Claude tools with a local API stub; no account or paid calls."""
import argparse
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import socket
import socketserver
import struct

REPO = Path(__file__).resolve().parents[1]


def clean_test_processes(records):
    """Only signal PIDs created by this test wrapper, guarding PID reuse."""
    if not records.exists():
        return True

    def still_ours(process):
        try:
            fields = Path(f"/proc/{process['pid']}/stat").read_text().rsplit(")", 1)[1].split()
            return fields[19] == process["start"] and fields[0] != "Z"
        except (ProcessLookupError, FileNotFoundError):
            return False

    # Claude can take several seconds to settle its PTY/MCP children on Android.
    deadline = time.monotonic() + 20
    signalled = set()
    while time.monotonic() < deadline:
        processes = [json.loads(line) for line in records.read_text().splitlines()]
        # Stop the supervisor first so it cannot respawn a worker during cleanup.
        processes.sort(key=lambda process: process["kind"] != ["daemon"])
        live = [process for process in processes if still_ours(process)]
        if not live:
            return True
        for process in live:
            identity = (process["pid"], process["start"])
            if identity not in signalled:
                try:
                    os.kill(process["pid"], signal.SIGTERM)
                except ProcessLookupError:
                    pass
                signalled.add(identity)
        time.sleep(0.05)
    raise RuntimeError("A process created by this background test did not exit")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO / "build/runtime")
    parser.add_argument("--with-proxy", action="store_true", help="Send the local API traffic through the Bionic proxy")
    parser.add_argument("--process-wrapper", action="store_true", help="Enter via the self-spawn wrapper with musl-style inherited environment")
    parser.add_argument("--report", type=Path, help="Also write the report to this path")
    parser.add_argument("--background", action="store_true", help="Exercise Claude's real on-demand daemon and background session")
    parser.add_argument("--privacy", choices=("standard", "optional-off"), default="standard")
    parser.add_argument("--raw-body-test", action="store_true", help="Positive control for opt-in raw API file recording")
    args = parser.parse_args()
    root = args.root.resolve()
    native_execution = (root / "libtermux-musl-compat.so").is_file()
    runs = root / "tests"
    runs.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="smoke-", dir=runs))
    workspace = run / "workspace"
    if args.background:
        # Claude protects shared checkouts from background edits. Test inside a
        # real disposable linked worktree, with that protection left enabled.
        repository = run / "repository"
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        subprocess.run(["git", "-C", str(repository), "-c", "user.name=Adapter Test",
                        "-c", "user.email=adapter-test@example.invalid", "-c", "commit.gpgsign=false",
                        "commit", "-q", "--allow-empty", "-m", "Local fixture"], check=True)
        subprocess.run(["git", "-C", str(repository), "worktree", "add", "-q", "--detach", str(workspace)], check=True)
    else:
        workspace.mkdir()
    (workspace / "fixture.txt").write_text("a needle in Termux\n")
    (workspace / "via-env.py").write_text("#!/usr/bin/env python3\nprint('ENV_PY_OK')\n")
    (workspace / "via-env-node").write_text("#!/usr/bin/env node\nconsole.log('ENV_NODE_OK')\n")
    for name in ["via-env.py", "via-env-node"]:
        (workspace / name).chmod(0o700)
    (workspace / "hook.py").write_text("#!/usr/bin/env python3\nfrom pathlib import Path\nPath('hook-ok.txt').write_text('HOOK_OK')\n")
    (workspace / "hook.py").chmod(0o700)
    (workspace / "mcp-server.py").write_text('''#!/usr/bin/env python3
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message.get("method")
    if method == "initialize":
        result = {"protocolVersion": message["params"]["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "smoke", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "probe", "description": "Return a fixed local test marker", "inputSchema": {"type": "object", "properties": {}}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "MCP_OK"}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
''')
    (workspace / "mcp-server.py").chmod(0o700)
    mcp_config = {"mcpServers": {"smoke": {"command": str(workspace / "mcp-server.py"), "args": []}}}
    settings = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": str(workspace / "hook.py")}]}]}}
    destination = workspace / "written.txt"
    plan = [
        ("Bash", {"command": "set -e; printf 'SHELL_OK\\n'; ./via-env.py; ./via-env-node; grep needle fixture.txt; find . -name fixture.txt; git --version; type grep; type find; printf 'DISPATCH=%s\\n' \"$CLAUDE_CODE_EXECPATH\"", "description": "Check native Termux shell and script execution"}),
        ("Read", {"file_path": str(workspace / "fixture.txt")}),
        ("Write", {"file_path": str(destination), "content": "written-by-claude\n"}),
        ("Edit", {"file_path": str(destination), "old_string": "written-by-claude", "new_string": "edited-by-claude"}),
        ("Grep", {"pattern": "needle", "path": str(workspace), "output_mode": "content"}),
        ("Glob", {"pattern": "**/*.txt", "path": str(workspace)}),
        ("mcp__smoke__probe", {}),
    ]
    if native_execution and not args.with_proxy:
        plan[0][1]["command"] += '; test -z "$CLAUDE_CODE_PROCESS_WRAPPER"; test -z "$CLAUDE_CODE_SHELL_PREFIX"; "$CLAUDE_CODE_EXECPATH" --version'
    state = {"calls": 0, "tool_results": {}, "paths": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["paths"].append(self.path)
            if "count_tokens" in self.path:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"input_tokens": 100}')
                return
            if not self.path.startswith("/v1/messages"):
                self.send_error(404)
                return
            for message in body.get("messages", []):
                for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
                    if block.get("type") == "tool_result":
                        state["tool_results"][block["tool_use_id"]] = block
            index = len(state["tool_results"])
            state["calls"] += 1
            if index < len(plan):
                name, arguments = plan[index]
                block = {"type": "tool_use", "id": "toolu_smoke_" + str(index), "name": name, "input": arguments}
                stop = "tool_use"
            else:
                block = {"type": "text", "text": "MOCK_SMOKE_DONE"}
                stop = "end_turn"
            response = {"id": "msg_smoke_" + str(state["calls"]), "type": "message", "role": "assistant",
                        "model": body.get("model", "claude-sonnet-4-6"), "content": [block], "stop_reason": stop,
                        "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 30}}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if body.get("stream") else "application/json")
            self.end_headers()
            if not body.get("stream"):
                self.wfile.write(json.dumps(response).encode())
                return
            start = dict(response, content=[], stop_reason=None)
            events = [("message_start", {"type": "message_start", "message": start})]
            initial = dict(block)
            if stop == "tool_use":
                initial["input"] = {}
                delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
            else:
                initial["text"] = ""
                delta = {"type": "text_delta", "text": block["text"]}
            events += [
                ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": initial}),
                ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 30}}),
                ("message_stop", {"type": "message_stop"}),
            ]
            for event, data in events:
                self.wfile.write(("event: " + event + "\ndata: " + json.dumps(data) + "\n\n").encode())
            self.wfile.flush()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    dns_requests = []
    class AndroidDNS(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            command = b''
            while not command.endswith(b'\0'):
                data = self.request.recv(1)
                if not data:
                    return
                command += data
            _, host, service, flags, family, kind, protocol, network = command[:-1].decode().split()
            dns_requests.append(host)
            if host != 'claude-dns-fixture.invalid':
                self.request.sendall(b'401\0' + struct.pack('=i', 8))
                return
            address = struct.pack('=H', socket.AF_INET) + struct.pack('!H', 0 if service == '^' else int(service)) + socket.inet_aton('127.0.0.1') + b'\0' * 8
            self.request.sendall(b'222\0' + struct.pack('!IIIIII', 1, int(flags), socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, len(address)) + address + struct.pack('!II', 0, 0))
    dns_directory = tempfile.TemporaryDirectory(prefix='claude-dns-')
    dns_path = str(Path(dns_directory.name) / 'netd.sock')
    dns_server = socketserver.ThreadingUnixStreamServer(dns_path, AndroidDNS)
    threading.Thread(target=dns_server.serve_forever, daemon=True).start()
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_CONFIG_")) or key.lower().endswith("_proxy"):
            env.pop(key)
    env.update(CLAUDE_CONFIG_DIR=str(run / "config"),
               TMPDIR=str(run / "tmp"), CLAUDE_CODE_TMPDIR=str(run / "tmp"),
               CLAUDE_TERMUX_ROOT=str(root), ANTHROPIC_API_KEY="local-test-key-no-account",
               ANTHROPIC_BASE_URL="http://" + ("127.0.0.1" if args.with_proxy else "claude-dns-fixture.invalid") + ":" + str(server.server_port),
               CLAUDE_TERMUX_DNS_SOCKET=dns_path,
               CLAUDE_TERMUX_DNS_MODE="http-proxy" if args.with_proxy else "native",
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1",
               ENABLE_TOOL_SEARCH="false",
               DISABLE_ERROR_REPORTING="1", CLAUDE_MUSL_NO_PROXY="0" if args.with_proxy else "1",
               NO_PROXY="" if args.with_proxy else "127.0.0.1,localhost", no_proxy="")
    env["CLAUDE_TERMUX_PRIVACY"] = args.privacy
    if args.privacy == "optional-off":
        env.pop("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", None)
        env.pop("DISABLE_TELEMETRY", None)
    if args.raw_body_test or args.privacy == "optional-off":
        # Try to enable raw-body file collection through user settings; the
        # privacy initializer must suppress this independent collection path.
        settings.setdefault("env", {})["OTEL_LOG_RAW_API_BODIES"] = "file:" + str(run / "raw-api-bodies")
    if args.background:
        # Only this temporary workspace is trusted. No real profile is edited.
        profile = {"hasCompletedOnboarding": True, "theme": "dark",
                   "projects": {str(workspace): {"hasTrustDialogAccepted": True}}}
        config = Path(env["CLAUDE_CONFIG_DIR"])
        config.mkdir()
        for path in (config / ".claude.json",):
            path.write_text(json.dumps(profile))
        wrapper = run / "record-wrapper"
        if native_execution:
            probe = run / "record-process.mjs"
            probe.write_text("import fs from 'node:fs';\n"
                             + "const record = {pid:process.pid, start:fs.readFileSync('/proc/self/stat','utf8').split(')')[1].trim().split(/\\s+/)[19], "
                             + "kind:process.argv.includes('daemon') ? ['daemon'] : process.argv.includes('--session-id') ? ['--session-id'] : ['entry'], "
                             + "native:fs.readFileSync('/proc/self/maps','utf8').includes('/libtermux-musl-compat.so'), "
                             + "wrapper:!!process.env.CLAUDE_CODE_PROCESS_WRAPPER, shellPrefix:!!process.env.CLAUDE_CODE_SHELL_PREFIX};\n"
                             + "fs.appendFileSync(" + json.dumps(str(run / "wrapper-calls.jsonl")) + ", JSON.stringify(record)+'\\n');\n")
            env["BUN_OPTIONS"] = "--preload " + str(probe)
        else:
            wrapper.write_text("#!" + sys.executable + "\nimport json,os,sys\nfrom pathlib import Path\n"
                           + "record = {'kind': sys.argv[2:3], 'pid': os.getpid(), 'start': Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]}\n"
                           + "with Path(" + repr(str(run / "wrapper-calls.jsonl")) + ").open('a') as f: f.write(json.dumps(record) + '\\n')\n"
                           + "os.execv(" + repr(str(REPO / "bin/claude-process-wrapper")) + ", ["
                           + repr(str(REPO / "bin/claude-process-wrapper")) + ", *sys.argv[1:]])\n")
            wrapper.chmod(0o700)
            settings["env"] = {"CLAUDE_CODE_PROCESS_WRAPPER": str(wrapper)}
            env["CLAUDE_CODE_PROCESS_WRAPPER"] = str(wrapper)
        # Interactive background sessions require an approved credential source.
        # A helper in this isolated user config supplies only the local stub key.
        settings["apiKeyHelper"] = "printf '%s' local-test-key-no-account"
        env.pop("ANTHROPIC_API_KEY")
        (config / "settings.json").write_text(json.dumps(settings))
    if args.privacy == "optional-off":
        observer = run / "privacy-observer.mjs"
        observer.write_text("import fs from 'node:fs';\n"
                            + "process.on('exit', () => { const state=globalThis[Symbol.for('claude-termux.privacy.v1')]; "
                            + "fs.appendFileSync(" + json.dumps(str(run / "privacy-observed.jsonl"))
                            + ", JSON.stringify({pid:process.pid, ready:state?.ready===true, counts:state?.counts, "
                            + "broadFlags:!!process.env.DISABLE_TELEMETRY||!!process.env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC})+'\\n'); });\n")
        env["BUN_OPTIONS"] = (env.get("BUN_OPTIONS", "") + " --preload " + str(observer)).strip()
    if args.with_proxy:
        env["CLAUDE_TERMUX_PROXY_LOG"] = str(run / "proxy.log")
    command = [str(REPO / "bin/claude-termux"), "-p", "Run the requested fixture checks.",
               "--model", "claude-sonnet-4-6", "--setting-sources", "", "--strict-mcp-config",
               "--mcp-config", json.dumps(mcp_config), "--settings", json.dumps(settings),
               "--allowedTools", "Bash,Read,Write,Edit,Grep,Glob,mcp__smoke__probe", "--tools", "Bash,Read,Write,Edit,Grep,Glob",
               "--permission-mode", "acceptEdits", "--no-session-persistence", "--max-turns", "12",
               "--output-format", "stream-json", "--verbose", "--debug-file", str(run / "debug.log")]
    if args.process_wrapper:
        # A musl parent does not carry Bionic loader variables. The stale
        # parent-owned proxy must be replaced before the child can make calls.
        env["CLAUDE_TERMUX_BIONIC_PRELOAD"] = env.pop("LD_PRELOAD", "")
        env["CLAUDE_TERMUX_BIONIC_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH", "")
        stale = "http://claude:expired-test-token@127.0.0.1:1"
        env["CLAUDE_TERMUX_MANAGED_PROXY"] = stale
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[key] = stale
        command[:1] = [str(REPO / "bin/claude-process-wrapper"), str(root / "claude")]
    if args.background:
        command[1] = "--bg"
        command[command.index("--setting-sources") + 1] = "user"
        for option in ("--output-format", "--max-turns"):
            index = command.index(option)
            del command[index:index + 2]
        command.remove("--no-session-persistence")
        command.remove("--verbose")
    print("Testing in", run, flush=True)
    start = time.monotonic()
    try:
        result = subprocess.run(command, env=env, cwd=workspace, capture_output=True, text=True, timeout=90)
        (run / "stdout.jsonl").write_text(result.stdout)
        (run / "stderr.txt").write_text(result.stderr)
        state.update(returncode=result.returncode, seconds=round(time.monotonic() - start, 2))
        if args.background and result.returncode == 0:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                transcripts = list((Path(env["CLAUDE_CONFIG_DIR"]) / "projects").rglob("*.jsonl"))
                if len(state["tool_results"]) == len(plan) and any('"type":"assistant"' in p.read_text() and 'MOCK_SMOKE_DONE' in p.read_text() for p in transcripts):
                    break
                time.sleep(0.1)
            else:
                state["returncode"] = "background timeout"
            status = subprocess.run([str(REPO / "bin/claude-termux"), "daemon", "status"],
                                    env=env, cwd=workspace, capture_output=True, text=True, timeout=15)
            (run / "daemon-status.txt").write_text(status.stdout + status.stderr)
            calls = run / "wrapper-calls.jsonl"
            records = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
            state["background_spawns"] = [record["kind"] for record in records]
            if native_execution:
                state["background_native_verified"] = bool(records) and all(record["native"] and (args.with_proxy or not record["wrapper"]) and not record["shellPrefix"] for record in records)
            else:
                state["background_wrapper_verified"] = "launcher: " + str(wrapper) in status.stdout
            state["seconds"] = round(time.monotonic() - start, 2)
    except subprocess.TimeoutExpired as error:
        (run / "stdout.jsonl").write_bytes(error.stdout or b"")
        (run / "stderr.txt").write_bytes(error.stderr or b"")
        state.update(returncode="timeout", seconds=90)
    finally:
        if args.background:
            state["background_cleanup"] = clean_test_processes(run / "wrapper-calls.jsonl")
        server.shutdown()
        server.server_close()
        dns_server.shutdown()
        dns_server.server_close()
        dns_directory.cleanup()
    state['dns_queries'] = dns_requests
    failures = []
    if args.privacy == "optional-off":
        observations = run / "privacy-observed.jsonl"
        records = [json.loads(line) for line in observations.read_text().splitlines()] if observations.exists() else []
        state["privacy_processes"] = records
        if not records or not all(record["ready"] and not record["broadFlags"] for record in records):
            failures.append("A Claude process did not load privacy controls without broad flags")
        if not any(record.get("counts", {}).get("event", 0) for record in records):
            failures.append("Real CLI event producer was not exercised")
    if args.raw_body_test or args.privacy == "optional-off":
        state["raw_api_body_files"] = [str(p.relative_to(run)) for p in (run / "raw-api-bodies").rglob("*") if p.is_file()]
    if args.privacy == "optional-off":
        if state["raw_api_body_files"]:
            failures.append("Optional raw API body files were collected")
    elif args.raw_body_test and not state["raw_api_body_files"]:
        failures.append("Raw API body positive control produced no files")
    if not args.with_proxy and 'claude-dns-fixture.invalid' not in dns_requests:
        failures.append('CLI did not use the DNS compatibility library')
    if state["returncode"] != 0:
        failures.append("CLI did not exit successfully")
    if args.background:
        if not state.get("background_native_verified" if native_execution else "background_wrapper_verified") or ["daemon"] not in state.get("background_spawns", []) or ["--session-id"] not in state.get("background_spawns", []):
            failures.append("Actual daemon/session did not use the expected execution boundary")
    for index, (name, _) in enumerate(plan):
        response = state["tool_results"].get("toolu_smoke_" + str(index))
        if not response or response.get("is_error"):
            failures.append(name + " failed: " + json.dumps(response))
    bash = json.dumps(state["tool_results"].get("toolu_smoke_0"))
    for expected in ["SHELL_OK", "ENV_PY_OK", "ENV_NODE_OK", "needle", "fixture.txt", "git version"]:
        if expected not in bash:
            failures.append("Bash output missing " + expected)
    if not destination.exists() or destination.read_text() != "edited-by-claude\n":
        failures.append("Write/Edit did not produce the expected file")
    if not (workspace / "hook-ok.txt").exists():
        failures.append("SessionStart script hook did not run")
    if "MCP_OK" not in json.dumps(state["tool_results"].get("toolu_smoke_6")):
        failures.append("stdio MCP script did not return its marker")
    if args.with_proxy:
        log = run / "proxy.log"
        if not log.exists() or "127.0.0.1" not in log.read_text():
            failures.append("The local API requests did not appear in the proxy log")
    state["failures"] = failures
    (run / "report.json").write_text(json.dumps(state, indent=2) + "\n")
    if args.report:
        args.report.write_text(json.dumps(state, indent=2) + "\n")
    print(json.dumps(state, indent=2), flush=True)
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
