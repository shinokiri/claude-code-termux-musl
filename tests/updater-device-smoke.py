#!/usr/bin/env python3
"""Exercise a published adapter update and background checks in a disposable install."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "libexec"))
import termux_manage as manage
import termux_update as updates


def background_checks(data, claude, env):
    previous = updates.read_json(data / "update-status.json")
    previous["checked_at"] = 0
    manage.atomic_json(data / "update-status.json", previous)
    child, terminal = pty.fork()
    if child == 0:
        os.chdir(data)
        os.execve(str(claude), [str(claude)], env)
    watcher, watcher_identity = None, None
    try:
        completed = previous["completed_at"]
        checks = 0
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            if select.select([terminal], [], [], 0.2)[0]:
                try:
                    os.read(terminal, 65536)
                except OSError:
                    pass
            if updates.process_identity(child) is None:
                raise RuntimeError("Interactive Claude exited before background checks")
            if watcher is None:
                # Android kernels may omit /proc/<pid>/task/<tid>/children.
                for candidate in Path("/proc").iterdir():
                    if not candidate.name.isdigit():
                        continue
                    try:
                        fields = (candidate / "stat").read_text().rsplit(")", 1)[1].split()
                        if fields[1] != str(child):
                            continue
                        command = (candidate / "cmdline").read_bytes()
                    except OSError:
                        continue
                    if b"termux_update.py" in command:
                        watcher = int(candidate.name)
                        watcher_identity = updates.process_identity(watcher)
            last = updates.read_json(data / "update-status.json", {})
            if last.get("completed_at", 0) > completed:
                if last["result"] != "current":
                    raise RuntimeError("Unexpected background result: " + json.dumps(last))
                completed = last["completed_at"]
                checks += 1
                if checks == 2:
                    break
                # Simulate the next interval elapsing while the same session is
                # still open, rather than waiting an hour in a device test.
                with (data / "install.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    last["checked_at"] = 0
                    manage.atomic_json(data / "update-status.json", last)
        if checks != 2 or watcher is None:
            raise RuntimeError("The interactive session did not perform both background checks")
    finally:
        try:
            os.kill(child, signal.SIGTERM)
        except ProcessLookupError:
            pass
        os.waitpid(child, 0)
        os.close(terminal)
        if watcher is not None:
            deadline = time.monotonic() + 35
            while updates.process_identity(watcher) == watcher_identity and time.monotonic() < deadline:
                time.sleep(0.2)
            if updates.process_identity(watcher) == watcher_identity:
                os.kill(watcher, signal.SIGTERM)
                raise RuntimeError("Updater watcher outlived its Claude session")
    return {"checks_in_one_interactive_session": checks, "watcher_exited_with_session": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, help="Copy already verified build/download caches from this installation")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="claude-updater-device-") as directory:
        data = Path(directory)
        config, commands = data / "config", data / "bin"
        base = data / "releases/fixture"
        base.mkdir(parents=True)
        installation = {"schema": 1, "data_dir": str(data), "config_dir": str(config), "bin_dir": str(commands)}
        manage.snapshot(base, installation)
        # Only this isolated fixture pretends to be an older installation. Both
        # real artifacts then come through the public update/download path.
        manage.atomic_json(base / "adapter.json", {"schema": 1, "version": "0.0.0"})
        (base / "runtime").mkdir()
        (base / "runtime/claude").write_bytes(b"fixture: never executed")
        manage.atomic_json(base / "runtime/runtime.json", {"version": "0.0.0", "root": str(base / "runtime")})
        manage.seal(base)
        manage.atomic_link(data / "current", base.relative_to(data))
        commands.mkdir()
        for link, target in manage.command_links(data, commands).items():
            manage.atomic_link(link, target)
        manage.atomic_json(config / "settings.json", {"env": {"CLAUDE_CODE_PROCESS_WRAPPER": manage.wrapper_value(data)}})
        if args.cache:
            for name in ("downloads/musl.apk", "downloads/musl-dev.apk", "downloads/claude.tgz", "tools/patchelf"):
                source = args.cache / name
                if source.is_file():
                    target = data / "cache" / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
        env = {key: value for key, value in os.environ.items() if not key.startswith(("ANTHROPIC_", "CLAUDE_", "DISABLE_AUTOUPDATER", "DISABLE_UPDATES"))}
        env.update(CLAUDE_CONFIG_DIR=str(config), TMPDIR=str(data),
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
        claude = commands / "claude"

        def run(*arguments):
            result = subprocess.run([str(claude), *arguments], env=env, cwd=data, capture_output=True, text=True, timeout=900)
            if result.returncode:
                raise RuntimeError(result.stdout[-8000:] + result.stderr[-4000:])
            print(result.stdout, end="", flush=True)
            return result.stdout

        available = json.loads(run("update", "--check"))
        assert available["result"] == "available"
        run("update")
        installed = updates.status(data)
        assert installed["installed_claude"] == available["target_version"]
        assert installed["installed_adapter"] == available["target_adapter"]
        assert installed["last_attempt"]["result"] == "updated"
        assert manage.release_at(data, "previous") == base
        assert not list(data.glob("adapter-*"))
        run("--version")
        count = len(list((data / "releases").iterdir()))
        run("update")
        assert len(list((data / "releases").iterdir())) == count
        subprocess.run([str(commands / "claude-termux-manage"), "doctor"], env=env, cwd=data, check=True)
        report = {"official_version": installed["installed_claude"], "adapter_version": installed["installed_adapter"],
                  "published_adapter_upgrade": "passed", "official_package_upgrade": "passed",
                  "no_op_update": "passed", "temporary_source_cleanup": "passed",
                  "real_account_tested": False}
        report.update(background_checks(data, claude, env))
        args.report.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
