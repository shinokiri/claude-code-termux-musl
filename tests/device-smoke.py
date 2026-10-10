#!/usr/bin/env python3
"""Exercise the real Claude tools with a local API stub; no account or paid calls."""
import argparse
import http.server
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

REPO = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO / ".runtime")
    parser.add_argument("--with-proxy", action="store_true", help="Send the local API traffic through the Bionic proxy")
    args = parser.parse_args()
    root = args.root.resolve()
    runs = root / "tests"
    runs.mkdir(exist_ok=True)
    run = Path(tempfile.mkdtemp(prefix="smoke-", dir=runs))
    workspace = run / "workspace"
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
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_CONFIG_")) or key.lower().endswith("_proxy"):
            env.pop(key)
    env.update(HOME=str(run / "home"), CLAUDE_CONFIG_DIR=str(run / "config"),
               TMPDIR=str(run / "tmp"), CLAUDE_CODE_TMPDIR=str(run / "tmp"),
               CLAUDE_TERMUX_ROOT=str(root), ANTHROPIC_API_KEY="local-test-key-no-account",
               ANTHROPIC_BASE_URL="http://127.0.0.1:" + str(server.server_port),
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1",
               ENABLE_TOOL_SEARCH="false",
               DISABLE_ERROR_REPORTING="1", CLAUDE_MUSL_NO_PROXY="0" if args.with_proxy else "1",
               NO_PROXY="" if args.with_proxy else "127.0.0.1,localhost", no_proxy="")
    Path(env["HOME"]).mkdir()
    if args.with_proxy:
        env["CLAUDE_TERMUX_PROXY_LOG"] = str(run / "proxy.log")
    command = [str(REPO / "bin/claude-termux"), "-p", "Run the requested fixture checks.",
               "--model", "claude-sonnet-4-6", "--setting-sources", "", "--strict-mcp-config",
               "--mcp-config", json.dumps(mcp_config), "--settings", json.dumps(settings),
               "--allowedTools", "Bash,Read,Write,Edit,Grep,Glob,mcp__smoke__probe", "--tools", "Bash,Read,Write,Edit,Grep,Glob",
               "--permission-mode", "acceptEdits", "--no-session-persistence", "--max-turns", "12",
               "--output-format", "stream-json", "--verbose", "--debug-file", str(run / "debug.log")]
    print("Testing in", run, flush=True)
    start = time.monotonic()
    try:
        result = subprocess.run(command, env=env, cwd=workspace, capture_output=True, text=True, timeout=90)
        (run / "stdout.jsonl").write_text(result.stdout)
        (run / "stderr.txt").write_text(result.stderr)
        state.update(returncode=result.returncode, seconds=round(time.monotonic() - start, 2))
    except subprocess.TimeoutExpired as error:
        (run / "stdout.jsonl").write_bytes(error.stdout or b"")
        (run / "stderr.txt").write_bytes(error.stderr or b"")
        state.update(returncode="timeout", seconds=90)
    finally:
        server.shutdown()
        server.server_close()
    failures = []
    if state["returncode"] != 0:
        failures.append("CLI did not exit successfully")
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
    print(json.dumps(state, indent=2), flush=True)
    raise SystemExit(bool(failures))


if __name__ == "__main__":
    main()
