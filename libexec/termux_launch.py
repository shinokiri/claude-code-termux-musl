#!/data/data/com.termux/files/usr/bin/python3
"""Launch Claude and its self-spawned processes across the Bionic/musl boundary."""
import json
import os
from pathlib import Path
import select
import subprocess
import sys

APP = Path(__file__).resolve().parents[1]


def main(process_wrapper=False):
    manifest = APP / "installation.json"
    installation = json.loads(manifest.read_text()) if manifest.exists() else {}
    root = Path(os.environ.get("CLAUDE_TERMUX_ROOT", APP / ("runtime" if installation else "build/runtime"))).resolve()
    arguments = sys.argv[1:]
    if not process_wrapper and installation and arguments[:1] == ["update"]:
        command = APP / "bin/claude-termux-manage"
        os.execv(sys.executable, [sys.executable, str(command), "update", *arguments[1:]])
    if not process_wrapper:
        try:
            from termux_update import start_monitor
            start_monitor(installation, arguments)
        except (OSError, ValueError, RuntimeError):
            # Offline/disabled/broken update checks must never block starting.
            pass
    if process_wrapper:
        if not arguments:
            raise RuntimeError("process wrapper requires a Claude binary and its arguments")
        if not (root / "claude").exists() or not os.path.samefile(root / "claude", arguments[0]):
            root = Path(arguments[0]).resolve().parent
    else:
        arguments = [str(root / "claude"), *arguments]
    prefix = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))
    dns_mode = os.environ.get("CLAUDE_TERMUX_DNS_MODE", "native")
    if dns_mode not in ("native", "http-proxy"):
        raise RuntimeError("CLAUDE_TERMUX_DNS_MODE must be native or http-proxy")
    native_execution = (root / "libtermux-musl-compat.so").is_file()
    if native_execution and not os.access(root / "libtermux-bionic-exec.so", os.R_OK):
        raise RuntimeError("Bionic execution compatibility library is missing")
    required = ["claude"] if native_execution else ["claude", "claude-dispatch", "termux-shell"]
    if dns_mode == "http-proxy":
        required.append("termux-http-proxy")
    if any(not os.access(root / name, os.X_OK) for name in required):
        sys.exit("claude-termux: run tools/prepare-runtime.py and tools/build-bridges.py first")
    if dns_mode == "native" and not os.access(root / ("libtermux-musl-compat.so" if native_execution else "libtermux-musl-dns.so"), os.R_OK):
        raise RuntimeError("Android DNS compatibility library is missing; rebuild bridges")
    env = dict(os.environ)
    env.setdefault("CLAUDE_TERMUX_BIONIC_PRELOAD", env.get("LD_PRELOAD") or str(prefix / "lib/libtermux-exec-ld-preload.so"))
    env.setdefault("CLAUDE_TERMUX_BIONIC_LIBRARY_PATH", env.get("LD_LIBRARY_PATH", ""))
    env.setdefault("CLAUDE_CODE_SHELL", str(prefix / "bin/bash"))
    if native_execution:
        owned_wrappers = {str(APP / "bin/claude-process-wrapper")}
        if installation:
            owned_wrappers.add(str(Path(installation["data_dir"]) / "current/bin/claude-process-wrapper"))
        owned_wrappers |= {json.dumps([value]) for value in tuple(owned_wrappers)}
        if env.get("CLAUDE_CODE_PROCESS_WRAPPER") in owned_wrappers:
            env.pop("CLAUDE_CODE_PROCESS_WRAPPER")
        shell_prefix = env.get("CLAUDE_CODE_SHELL_PREFIX", "")
        if shell_prefix == str(root / "termux-shell") or (installation and shell_prefix.startswith(installation["data_dir"] + "/releases/") and shell_prefix.endswith("/runtime/termux-shell")):
            env.pop("CLAUDE_CODE_SHELL_PREFIX", None)
    if not native_execution:
        env.setdefault("CLAUDE_CODE_PROCESS_WRAPPER", str(APP / "bin/claude-process-wrapper"))
        env["CLAUDE_CODE_SHELL_PREFIX"] = str(root / "termux-shell")
    elif dns_mode == "http-proxy":
        # Explicit legacy transport needs a separately owned proxy for each
        # Claude child. Native DNS has no parent-owned resource to re-create.
        env.setdefault("CLAUDE_CODE_PROCESS_WRAPPER", str(APP / "bin/claude-process-wrapper"))
    env.update(DISABLE_AUTOUPDATER="1", DISABLE_UPDATES="1", CLAUDE_TERMUX_SESSION="1")
    env.setdefault("SSL_CERT_FILE", str(prefix / "etc/tls/cert.pem"))
    env.setdefault("CLAUDE_CONFIG_DIR", installation.get("config_dir", str(root / "config")))
    if native_execution and installation:
        from termux_manage import migrate_legacy_settings
        migrate_legacy_settings(Path(env["CLAUDE_CONFIG_DIR"]), Path(installation["data_dir"]))
    env.setdefault("TMPDIR", str(root / "tmp"))
    env.setdefault("CLAUDE_CODE_TMPDIR", env["TMPDIR"])
    Path(env["CLAUDE_CODE_TMPDIR"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    proxy = None
    try:
        # Drop a proxy owned by an old parent, including when upgrading from 1.0.
        # Explicit user proxies are preserved in either DNS mode.
        inherited_proxy = env.pop("CLAUDE_TERMUX_MANAGED_PROXY", None)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            if inherited_proxy and env.get(key) == inherited_proxy:
                env.pop(key)
        if inherited_proxy:
            env.pop("CLAUDE_CODE_PROXY_RESOLVES_HOSTS", None)
        ambient = any(env.get(key) for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"))
        fallback_proxy = env.get("ALL_PROXY") or env.get("all_proxy")
        if not ambient and fallback_proxy:
            if not fallback_proxy.startswith(("http://", "https://")):
                raise RuntimeError("For Claude Code, set HTTPS_PROXY to an HTTP CONNECT proxy; an ALL_PROXY-only SOCKS URL is unsupported")
            env["HTTP_PROXY"] = env["HTTPS_PROXY"] = fallback_proxy
            ambient = True
        if not ambient and dns_mode == "http-proxy" and env.get("CLAUDE_MUSL_NO_PROXY") != "1":
            token_path = root / "proxy-token"
            command = [str(root / "termux-http-proxy"), "--auth-file", str(token_path)]
            if env.get("CLAUDE_TERMUX_PROXY_LOG"):
                command += ["--log", env["CLAUDE_TERMUX_PROXY_LOG"]]
            proxy = subprocess.Popen(command,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            if not select.select([proxy.stdout], [], [], 2 if process_wrapper else 15)[0]:
                raise RuntimeError("DNS proxy startup timed out")
            port = proxy.stdout.readline().strip()
            if not port.isdigit() or not 1 <= int(port) <= 65535:
                raise RuntimeError("DNS proxy did not report a valid port")
            url = "http://claude:" + token_path.read_text().strip() + "@127.0.0.1:" + port
            for key in ["HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"]:
                env[key] = url
            env["CLAUDE_TERMUX_MANAGED_PROXY"] = url
            env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")
            env.setdefault("no_proxy", env["NO_PROXY"])
        if proxy or ambient:
            env["CLAUDE_CODE_PROXY_RESOLVES_HOSTS"] = "1"
        # The dispatcher is Bionic and strips incompatible loader variables before
        # executing musl. Its argv[0] must survive re-entry as ugrep/bfs/rg.
        if proxy:
            # Keep its parent-liveness pipe across exec. The proxy also watches
            # getppid(), so inherited descriptors in children cannot orphan it.
            os.set_inheritable(proxy.stdin.fileno(), True)
        # Replacing the launcher preserves foreground TTY signals: a single
        # Ctrl-C reaches Claude once, with no supervising process forwarding it.
        if native_execution:
            # The ELF dependency loads compatibility code in every Claude
            # process. A Bionic library handles the reverse boundary in tools.
            env.pop("LD_PRELOAD", None)
            env.pop("LD_LIBRARY_PATH", None)
            os.execve(str(root / "claude"), arguments, env)
        os.execve(str(root / "claude-dispatch"), arguments, env)
    finally:
        if proxy:
            proxy.stdin.close()
            try:
                proxy.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait()


def entry(process_wrapper=False):
    try:
        sys.exit(main(process_wrapper))
    except (OSError, RuntimeError) as error:
        sys.exit("claude-termux: " + str(error))
