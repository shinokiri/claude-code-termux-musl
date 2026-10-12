"""Run owned synthetic fixtures with clean state and a process-tree accident guard.

This is test infrastructure, not a runtime privacy policy or a hostile-code
sandbox. The kernel broker inspects pointer arguments before CONTINUE; local
IPC and files are not a general confinement boundary. No unguarded fallback.
"""
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "fixture-guard.c"


def environment(directory):
    """Preserve platform identity, but never ambient credentials or startup hooks."""
    directory = Path(directory).resolve()
    prefix = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))
    home, temporary = directory / "home", directory / "tmp"
    for path in (home, temporary, directory / "git-template"):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = {name: os.environ[name] for name in ("TERMUX_VERSION", "ANDROID_ROOT", "ANDROID_DATA") if name in os.environ}
    env.update(PATH=str(prefix / "bin") + ":/system/bin", PREFIX=str(prefix),
               HOME=str(home), TMPDIR=str(temporary), LANG="C.UTF-8", TERM="xterm-256color",
               XDG_CONFIG_HOME=str(home / ".config"), XDG_CACHE_HOME=str(home / ".cache"),
               XDG_DATA_HOME=str(home / ".local/share"), XDG_STATE_HOME=str(home / ".local/state"),
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_TEMPLATE_DIR=str(directory / "git-template"), GIT_TERMINAL_PROMPT="0",
               SSH_ASKPASS_REQUIRE="never", CLAUDE_TERMUX_AUTO_UPDATE="0")
    preload = prefix / "lib/libtermux-exec-ld-preload.so"
    if preload.is_file():
        env["LD_PRELOAD"] = str(preload)
    env["LD_LIBRARY_PATH"] = str(prefix / "lib")
    return env


def _status(pid):
    return dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines() if ":" in line)


def inside_guard():
    """A marker alone is insufficient: check the live ancestor and extra filter."""
    try:
        broker = int(os.environ["TERMUX_FIXTURE_BROKER"])
        if Path(os.readlink(f"/proc/{broker}/exe")).name != "fixture-guard":
            return False
        parent = _status(broker)
        current = _status(os.getpid())
        if int(current["Seccomp_filters"]) <= int(parent["Seccomp_filters"]):
            return False
        for _ in range(128):
            pid = int(current["PPid"])
            if pid == broker:
                return True
            if pid <= 1:
                return False
            current = _status(pid)
    except (OSError, ValueError, KeyError):
        return False
    return False


def build_guard(directory):
    binary = Path(directory) / "fixture-guard"
    subprocess.run(["clang", "-O2", "-Wall", "-Wextra", "-Werror", str(SOURCE), "-o", str(binary)],
                   env=environment(Path(directory) / "compiler"), check=True, capture_output=True, text=True)
    return binary


def summary(log, binary):
    counts, denied = Counter(), Counter()
    lifecycle = []
    for line in log.read_text().splitlines() if log.exists() else []:
        item = json.loads(line)
        if "event" in item:
            lifecycle.append(item)
        else:
            counts[str(item["syscall"])] += 1
            if not item["allowed"]:
                denied[(item["syscall"], item["reason"])] += 1
    ready = any(row["event"] == "ready" for row in lifecycle)
    finished = next((row for row in lifecycle if row["event"] == "finished"), None)
    return {"schema": 1, "ready": ready, "all_descendants_reaped": bool(finished),
            "guard_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
            "syscall_notifications": dict(counts),
            "denied": [{"syscall": n, "reason": reason, "count": count}
                       for (n, reason), count in sorted(denied.items())],
            "cleanup_signals": finished.get("cleanup_signals", 0) if finished else None,
            "scope": "Owned synthetic process tree; loopback/local IPC, no real SSH; not a hostile-code sandbox"}


def run(command, *, env=None, cwd=None, timeout=180, **kwargs):
    """Guard a complete fixture or a foreground target; reap detached descendants."""
    with tempfile.TemporaryDirectory(prefix="claude-fixture-") as temporary:
        directory = Path(temporary)
        binary = build_guard(directory)
        log = directory / "guard.jsonl"
        child_env = environment(directory / "state") if env is None else dict(env)
        child_env.pop("TERMUX_FIXTURE_BROKER", None)
        child_env["TERMUX_FIXTURE_TIMEOUT"] = str(timeout)
        result = subprocess.run([str(binary), str(log), "--", *map(str, command)],
                                env=child_env, cwd=cwd, timeout=timeout + 10, **kwargs)
        result.fixture = summary(log, binary)
        if result.returncode == 0 and (not result.fixture["ready"] or not result.fixture["all_descendants_reaped"]):
            raise RuntimeError("Fixture guard did not finish; no unguarded fallback")
        return result


def check_capability():
    """Check kernel facilities before downloading or allocating a new release."""
    code = ("import errno,socket\n"
            "s=socket.socket();assert s.connect_ex(('127.0.0.2',9))==errno.EPERM\n"
            "print('FIXTURE_CAPABILITY_OK')\n")
    result = run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
    if result.returncode or "FIXTURE_CAPABILITY_OK" not in result.stdout:
        raise RuntimeError("This kernel cannot run the required synthetic-test guard "
                           "(seccomp user notifications, pidfd_getfd and owned-process inspection); "
                           "no unguarded fallback. " + result.stderr[-1200:])


def enter():
    """Keep direct test commands and installer-invoked tests on the same path."""
    if inside_guard():
        return
    if "TERMUX_FIXTURE_BROKER" in os.environ:
        raise RuntimeError("Cannot verify the fixture broker and inherited filter; refusing unguarded execution")
    # --help does not start a fixture. Avoid building anything just for usage.
    if sys.argv[1:] == ["--help"]:
        return
    result = run([sys.executable, str(Path(sys.argv[0]).resolve()), *sys.argv[1:]], timeout=600)
    if "--report" in sys.argv:
        report = Path(sys.argv[sys.argv.index("--report") + 1])
        if report.is_file():
            value = json.loads(report.read_text())
            value["fixture"] = result.fixture
            report.write_text(json.dumps(value, indent=2) + "\n")
    print("Fixture boundary: " + json.dumps(result.fixture, separators=(",", ":")), file=sys.stderr, flush=True)
    raise SystemExit(result.returncode)
