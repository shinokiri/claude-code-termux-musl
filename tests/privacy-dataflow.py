#!/usr/bin/env python3
"""Observe real CLI business payload schemas at local TLS receivers, using fake credentials.

The focused --project-check and --collector-check serve as installation gates. This is not a
proof of all network egress. Reports retain field names and
synthetic-marker/equality checks, never request bodies or identifier values.
"""
import argparse
import gzip
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import select
import socket
import socketserver
import ssl
import struct
import subprocess
import tempfile
import threading
from urllib.parse import parse_qs, urlsplit

REPO = Path(__file__).resolve().parents[1]
PROMPT = "Run the synthetic data-flow fixture."
MARKER = "DATAFLOW_TOOL_CANARY"


def schema(value, path=""):
    fields = set()
    if isinstance(value, dict):
        for key, item in value.items():
            field = path + "." + key if path else key
            fields.add(field)
            fields.update(schema(item, field))
    elif isinstance(value, list):
        for item in value:
            fields.update(schema(item, path + "[]"))
    return sorted(fields)


def run_case(root, directory, cert, key, mode, posture, observe_callers=False, stop_project=False, both_platforms=False,
             collector_check=False, collector_profile=None):
    directory.mkdir()
    for name in ("home", "config", "tmp", "workspace"):
        (directory / name).mkdir()
    workspace = directory / "workspace"
    (workspace / "fixture.txt").write_text("DATAFLOW_FILE_CANARY\n")
    (workspace / ".npmrc").write_text("registry=https://registry.fixture.invalid/\n")
    (workspace / ".envrc").write_text("# synthetic secret-manager reference: op read op://fixture/item/key\n")
    (workspace / "AndroidManifest.xml").write_text('<manifest package="invalid.fixture"/>\n')
    if both_platforms:
        (workspace / "App.xcodeproj").mkdir()
        (workspace / "App.xcodeproj/project.pbxproj").write_text('SDKROOT = iphoneos;\n')
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    if collector_check:
        subprocess.run(["git", "-C", str(workspace), "config", "remote.origin.url", "https://git.fixture.invalid/group/project.git"], check=True)
    state = {"mode": mode, "synthetic_posture_feature": posture, "caller_observation": observe_callers,
             "experimental_project_stop": stop_project, "both_platforms": both_platforms, "collector_check": collector_check, "requests": [],
             "model": [], "features": [], "event_batches": 0, "tool_results": {}, "dns_attempts": []}
    identities = {}

    def remember(channel, value):
        if isinstance(value, str) and value:
            identities.setdefault(channel, set()).add(value)

    plan = [("Bash", {"command": "printf 'DATAFLOW_TOOL_CANARY\\n'", "description": "Print a synthetic fixture marker"}),
            ("Read", {"file_path": str(workspace / "fixture.txt")})]
    if collector_check:
        plan[0][1]["command"] = "set -e; git remote get-url origin; git status --porcelain; printf 'DATAFLOW_TOOL_CANARY\\n'"

    class Receiver(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def observe(self):
            headers = {k.lower(): v for k, v in self.headers.items()}
            state["requests"].append({"method": self.command, "host": headers.get("host"),
                "path": urlsplit(self.path).path, "query_fields": sorted(parse_qs(urlsplit(self.path).query)),
                "headers": sorted(headers),
                "tool_duration_entries": len(headers["x-claude-code-prev-tool-durations"].split(";")) if "x-claude-code-prev-tool-durations" in headers else 0,
                "posture_fields": sorted(set(re.findall(r"(?:^|,)\s*([a-z][a-z0-9_-]*)=", headers.get("x-claude-code-wiggly-dove", ""))))})
            if self.path.startswith("/v1/messages"):
                remember("header_session", headers.get("x-claude-code-session-id"))

        def reply(self, value, streaming=False):
            payload = json.dumps(value).encode()
            if streaming:
                block = value["content"][0]
                initial = dict(block)
                if block["type"] == "tool_use":
                    initial["input"] = {}
                    delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
                else:
                    initial["text"] = ""
                    delta = {"type": "text_delta", "text": block["text"]}
                events = [("message_start", {"type": "message_start", "message": dict(value, content=[], stop_reason=None)}),
                    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": initial}),
                    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": delta}),
                    ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": value["stop_reason"], "stop_sequence": None}, "usage": {"output_tokens": 20}}),
                    ("message_stop", {"type": "message_stop"})]
                payload = "".join("event: " + name + "\ndata: " + json.dumps(data) + "\n\n" for name, data in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if streaming else "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def auxiliary(self):
            features = {"tengu_wiggly_dove": {"defaultValue": True}} if posture else {}
            self.reply({"features": features, "restrictions": {}, "metrics_logging_enabled": False})

        def do_GET(self):
            self.observe()
            self.auxiliary()

        def do_POST(self):
            self.observe()
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                parts = []
                while True:
                    size = int(self.rfile.readline().strip().split(b";")[0], 16)
                    if not size:
                        while self.rfile.readline().strip():
                            pass
                        break
                    parts.append(self.rfile.read(size))
                    self.rfile.read(2)
                raw = b"".join(parts)
            else:
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            if self.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            try:
                body = json.loads(raw) if raw else {}
            except (ValueError, UnicodeError):
                body = {}
            if self.path.startswith("/api/event_logging/"):
                state["event_batches"] += 1
                for event in body.get("events", []):
                    remember("event_device", event.get("event_data", {}).get("device_id"))
            if self.path.startswith(("/api/eval/", "/api/eval-authed/")):
                state["features"].append(schema(body))
                remember("feature_device", body.get("attributes", {}).get("deviceID"))
                remember("feature_session", body.get("attributes", {}).get("sessionId"))
            if not self.path.startswith("/v1/messages"):
                self.auxiliary()
                return
            if "count_tokens" in self.path:
                self.reply({"input_tokens": 100})
                return
            try:
                identity = json.loads(body.get("metadata", {}).get("user_id", "{}"))
            except (ValueError, TypeError):
                identity = {}
            if not isinstance(identity, dict):
                identity = {}
            remember("model_device", identity.get("device_id"))
            remember("model_session", identity.get("session_id"))
            system = body.get("system", [])
            system_text = "\n".join(b.get("text", "") for b in system if isinstance(b, dict)) if isinstance(system, list) else str(system)
            billing = [line for line in system_text.splitlines() if line.startswith("x-anthropic-billing-header:")]
            body_text = json.dumps(body)
            state["model"].append({"fields": sorted(body), "identity_fields": sorted(identity),
                "identity_nonempty": {k: bool(v) for k, v in identity.items()},
                "diagnostic_fields": schema(body.get("diagnostics")),
                "previous_message_is_fixture": body.get("diagnostics", {}).get("previous_message_id") == "msg_dataflow_" + str(len(state["model"])),
                "billing_fields": sorted(set(field for line in billing for field in re.findall(r"\b([a-z_]+)=", line))),
                "contains_prompt": PROMPT in body_text, "contains_workspace": str(workspace) in body_text,
                "contains_tool_marker": MARKER in body_text, "contains_file_marker": "DATAFLOW_FILE_CANARY" in body_text})
            for message in body.get("messages", []):
                content = message.get("content", [])
                for block in content if isinstance(content, list) else []:
                    if block.get("type") == "tool_result":
                        state["tool_results"][block["tool_use_id"]] = {
                            "is_error": bool(block.get("is_error")),
                            "marker": MARKER in json.dumps(block), "file_marker": "DATAFLOW_FILE_CANARY" in json.dumps(block),
                            "git_remote_marker": "https://git.fixture.invalid/group/project.git" in json.dumps(block)}
            index = len(state["tool_results"])
            if index < len(plan):
                name, arguments = plan[index]
                block = {"type": "tool_use", "id": "toolu_dataflow_" + str(index), "name": name, "input": arguments}
            else:
                block = {"type": "text", "text": "DATAFLOW_DONE"}
            self.reply({"id": "msg_dataflow_" + str(len(state["model"])), "type": "message", "role": "assistant",
                "model": body.get("model"), "content": [block], "stop_reason": "tool_use" if index < len(plan) else "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 100, "output_tokens": 20}}, body.get("stream", False))

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)

    class FixtureProxy(Receiver):
        def do_CONNECT(self):
            # Every CONNECT terminates locally, irrespective of the requested host.
            # This fixture never forwards a request to a real service.
            upstream = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
            self.send_response(200, "Connection established")
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            try:
                while True:
                    ready, _, _ = select.select([self.connection, upstream], [], [], 8)
                    if not ready:
                        break
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        (upstream if source is self.connection else self.connection).sendall(data)
            except OSError:
                pass
            finally:
                upstream.close()

        def do_POST(self):
            self.path = urlsplit(self.path).path
            super().do_POST()

        def do_GET(self):
            self.path = urlsplit(self.path).path
            super().do_GET()

    class NoExternalDNS(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(5)
            command = b""
            while not command.endswith(b"\0") and len(command) < 65536:
                part = self.request.recv(1)
                if not part:
                    return
                command += part
            words = command.rstrip(b"\0").split()
            if words and words[0] == b"getaddrinfo":
                state["dns_attempts"].append(words[1].decode(errors="replace"))
                self.request.sendall(b"401\0" + struct.pack("=i", 8))
            # Unused raw DNS queries also stop here; no upstream forwarding.

    # Keep Unix socket paths short on Android.
    dns_temp = tempfile.TemporaryDirectory(prefix="dfdns-")
    dns_path = str(Path(dns_temp.name) / "dns")
    dns = socketserver.ThreadingUnixStreamServer(dns_path, NoExternalDNS)
    proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureProxy)
    for service in (server, proxy, dns):
        threading.Thread(target=service.serve_forever, daemon=True).start()
    observer = directory / "observer.mjs"
    observer.write_text("import fs from 'node:fs'; process.on('exit',()=>{const s=globalThis[Symbol.for('claude-termux.privacy.v1')];"
        + "fs.writeFileSync(" + json.dumps(str(directory / "observer.json"))
        + ",JSON.stringify({ready:s?.ready===true,mode:s?.mode,counts:s?.counts,broadFlags:!!process.env.DISABLE_TELEMETRY||!!process.env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC}));});\n")
    env = {k: os.environ[k] for k in ("PATH", "PREFIX", "LD_PRELOAD", "LD_LIBRARY_PATH", "ANDROID_ROOT", "ANDROID_DATA") if k in os.environ}
    env.update(HOME=str(directory / "home"), CLAUDE_CONFIG_DIR=str(directory / "config"),
        TMPDIR=str(directory / "tmp"), CLAUDE_CODE_TMPDIR=str(directory / "tmp"),
        XDG_CONFIG_HOME=str(directory / "home/.config"), XDG_CACHE_HOME=str(directory / "home/.cache"),
        ANTHROPIC_API_KEY="synthetic-dataflow-key-no-account", ANTHROPIC_BASE_URL="https://api.anthropic.com",
        CLAUDE_TERMUX_PRIVACY=mode, CLAUDE_TERMUX_DNS_SOCKET=dns_path,
        DISABLE_AUTOUPDATER="1", DISABLE_UPDATES="1", ENABLE_TOOL_SEARCH="false",
        DISABLE_ERROR_REPORTING="1", DISABLE_FEEDBACK_COMMAND="1", CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY="1",
        CLAUDE_CODE_PROXY_RESOLVES_HOSTS="1", NODE_EXTRA_CA_CERTS=str(cert),
        BUN_OPTIONS="--preload " + str(observer), SHELL=os.environ.get("PREFIX", "/data/data/com.termux/files/usr") + "/bin/bash")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env[name] = "http://127.0.0.1:" + str(proxy.server_port)
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    if observe_callers:
        env.update(DATAFLOW_PROFILE=str(root.parent / "libexec/privacy-profile.json"),
                   DATAFLOW_CALLER_REPORT=str(directory / "callers.json"),
                   DATAFLOW_STOP_PROJECT_MARKERS="1" if stop_project else "0")
        env["BUN_OPTIONS"] += " --preload " + str(REPO / "tests/privacy-caller-probe.mjs")
    if collector_check:
        env["CLAUDE_AX_SCREEN_READER"] = "1"
    if collector_profile:
        env["DATAFLOW_COLLECTOR_PROFILE"] = str(collector_profile)
    env["CLAUDE_TERMUX_ROOT"] = str(root)
    command = [str(root.parent / "bin/claude-termux"), "-p", PROMPT, "--model", "claude-sonnet-4-6",
        "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--tools", "Bash,Read", "--allowedTools", "Bash,Read", "--permission-mode", "acceptEdits",
        "--no-session-persistence", "--max-turns", "5", "--output-format", "json"]
    try:
        result = subprocess.run(command, env=env, cwd=workspace, capture_output=True, text=True, timeout=90)
        state["exit"] = result.returncode
        state["completed"] = "DATAFLOW_DONE" in result.stdout
        (directory / "stderr.txt").write_text(result.stderr)
    except subprocess.TimeoutExpired:
        state.update(exit="timeout", completed=False)
    finally:
        for service in (proxy, server, dns):
            service.shutdown()
            service.server_close()
        dns_temp.cleanup()
    state["observer"] = json.loads((directory / "observer.json").read_text()) if (directory / "observer.json").exists() else {}
    if observe_callers:
        state["callers"] = json.loads((directory / "callers.json").read_text()) if (directory / "callers.json").exists() else {}
    profile = directory / "config/.claude.json"
    if profile.exists():
        remember("profile_device", json.loads(profile.read_text()).get("userID"))
    state["identity_links"] = {}
    for kind in ("device", "session"):
        groups = {k: v for k, v in identities.items() if k.endswith(kind)}
        state["identity_links"][kind] = {"channels": sorted(groups), "distinct_per_channel": {k: len(v) for k, v in groups.items()},
            "same": len(set().union(*groups.values())) == 1 if groups else None}
    failures = []
    if state["exit"] != 0 or not state["completed"] or len(state["model"]) != 3:
        failures.append("CLI fixture did not complete three model requests")
    if not state["features"]:
        failures.append("Feature evaluation path was not exercised")
    for index, expected in enumerate(("marker", "file_marker")):
        tool = state["tool_results"].get("toolu_dataflow_" + str(index), {})
        if tool.get("is_error") or not tool.get(expected):
            failures.append("Synthetic tool " + str(index) + " failed")
    if state["observer"].get("broadFlags") or (mode == "optional-off" and not state["observer"].get("ready")):
        failures.append("Expected privacy state was not observed")
    if mode == "standard" and not state["event_batches"]:
        failures.append("Positive control did not receive original event reporting")
    if mode == "optional-off" and state["event_batches"]:
        failures.append("Known optional reporting reached the receiver")
    posture_count = sum("x-claude-code-wiggly-dove" in r["headers"] for r in state["requests"] if r["path"] == "/v1/messages")
    if posture_count != (3 if posture else 0):
        failures.append("Conditional posture feature was not exercised as intended")
    if observe_callers and not state["callers"].get("callsWithFields"):
        failures.append("Event caller observation did not exercise preconstructed payloads")
    if observe_callers:
        project = state["callers"].get("projectMarkers", {})
        reads = project.get("boundedReads", [])
        revision = json.loads((root.parent / "libexec/privacy-profile.json").read_text()).get("policy_revision", 0)
        if not project.get("calls"):
            failures.append("Project marker collector entry was not reached")
        if stop_project:
            if reads or project.get("resultFields"):
                failures.append("Stopped project collector still read or produced classification fields")
            if state["callers"].get("developmentIntentSignals") != 0:
                failures.append("Expected shared-collector effect on development-intent signals was not observed")
        else:
            if revision >= 3:
                if both_platforms:
                    if not reads or any(r["fixture"] != "project.pbxproj" or not r["returnedText"] or r["byteLimit"] != 1048576 for r in reads):
                        failures.append("Functional collector did not limit content reads to the Xcode project")
                    if state["callers"].get("developmentIntentKinds") != {"ios_app": 1, "android_app": 1}:
                        failures.append("Both development-intent system messages were not preserved")
                elif reads:
                    failures.append("Functional-only manifest fixture unexpectedly read project content")
                if project.get("resultFields") != ["has_android_app_project", "has_ios_app_project"]:
                    failures.append("Project collector did not produce only the two functional fields")
            else:
                for fixture in (".npmrc", ".envrc"):
                    if not any(r["fixture"] == fixture and r["returnedText"] for r in reads):
                        failures.append("Project collector did not exercise synthetic " + fixture)
            if not state["callers"].get("developmentIntentSignals"):
                failures.append("Shared project collector's system-message consumer was not exercised")
        if collector_check:
            rows = state["callers"].get("collectors", {})
            for name in ("git", "files", "permissions", "accessibility"):
                row = rows.get(name, {})
                if not row.get("calls") or row.get("completed") != row.get("calls"):
                    failures.append("Dedicated collector was not exercised: " + name)
                if revision >= 4:
                    if row.get("resultFields") or any(row.get(k) for k in ("fileReads", "gitConfigReads", "fileEnumerations", "gitChecks", "remoteLookups")):
                        failures.append("Dedicated analytics still collected: " + name)
                    expected_type = {"git": "null", "files": "undefined", "permissions": "object", "accessibility": "object"}[name]
                    if row.get("resultTypes") != [expected_type]:
                        failures.append("Dedicated collector changed its empty-result contract: " + name)
                elif name != "files" and not row.get("resultFields"):
                    failures.append("Original collector produced no control fields: " + name)
            if revision < 4 and not rows.get("files", {}).get("fileEnumerations"):
                failures.append("Original file enumeration was not observed")
            if not state["tool_results"].get("toolu_dataflow_0", {}).get("git_remote_marker"):
                failures.append("Business Git remote lookup was not preserved")
    state.update(passed=not failures, failures=failures)
    (directory / "report.json").write_text(json.dumps(state, indent=2) + "\n")
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", type=Path, help="New directory outside the installed runtime")
    destination.add_argument("--report", type=Path, help="Installation-gate report; fixtures go under runtime/tests")
    focus = parser.add_mutually_exclusive_group()
    focus.add_argument("--project-check", action="store_true", help="Focused functional project detection gate")
    focus.add_argument("--collector-check", action="store_true", help="Dedicated analytics and shared Git capability gate")
    parser.add_argument("--collector-profile", type=Path, help="Version-verified observer bindings for a historical baseline")
    args = parser.parse_args()
    root = args.root.resolve()
    if args.output:
        output = args.output.resolve()
        if output.is_relative_to(root.parent):
            parser.error("Write audit artifacts outside the installed release")
        output.mkdir(parents=True, exist_ok=False)
    else:
        (root / "tests").mkdir(exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix="dataflow-", dir=root / "tests"))
    output.chmod(0o700)
    runtime = json.loads((root / "runtime.json").read_text())
    if hashlib.sha256((root / "claude").read_bytes()).hexdigest() != runtime["patched_sha256"]:
        raise RuntimeError("Installed Claude binary differs from its runtime record")
    helper = output / "tls-cert"
    subprocess.run(["clang", "-O2", str(REPO / "tests/tls-cert.c"), "-lcrypto", "-o", str(helper)], check=True)
    cert, key = output / "cert.pem", output / "key.pem"
    hosts = ("api.anthropic.com", "api-staging.anthropic.com", "statsig.anthropic.com", "claude.ai",
             "http-intake.logs.us5.datadoghq.com", "browser-intake-us5-datadoghq.com")
    subprocess.run([str(helper), str(key), str(cert), ",".join("DNS:" + h for h in hosts)], check=True)
    key.chmod(0o600)
    cases = {}
    scenarios = (
            ("standard", "standard", False, False, False), ("optional-off", "optional-off", False, False, False),
            ("optional-off-feature", "optional-off", True, False, False), ("optional-off-callers", "optional-off", False, True, False),
            ("optional-off-project-stop", "optional-off", False, True, True))
    if args.project_check:
        scenarios = (("functional-project-detection", "optional-off", False, True, False),)
    elif args.collector_check:
        scenarios = (("dedicated-analytics-collectors", "optional-off", False, True, False),)
    for name, mode, posture, callers, stop_project in scenarios:
        cases[name] = run_case(root, output / name, cert, key, mode, posture, callers, stop_project,
                              args.project_check, args.collector_check, args.collector_profile.resolve() if args.collector_profile else None)
        print(json.dumps({"case": name, "passed": cases[name]["passed"], "failures": cases[name]["failures"],
                          "model_requests": len(cases[name]["model"]), "event_batches": cases[name]["event_batches"]}), flush=True)
    report = {"schema": 1, "claude": runtime["version"], "original_sha256": runtime["original_sha256"],
        "adapter": json.loads((root.parent / "adapter.json").read_text())["version"],
        "passed": all(c["passed"] for c in cases.values()), "cases": cases,
        "scope": "Isolated local TLS fixtures, fake API key, synthetic tools and feature assignment. No real account, model service, Remote Control or all-egress proof."}
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    return not report["passed"]


if __name__ == "__main__":
    raise SystemExit(main())
