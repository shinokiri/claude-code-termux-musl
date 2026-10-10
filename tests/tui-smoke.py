#!/usr/bin/env python3
"""Open a fresh onboarding TUI in a PTY and check double-Ctrl-C termination."""
import argparse
import json
import fcntl
import os
from pathlib import Path
import pty
import select
import subprocess
import tempfile
import termios
import time

REPO = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO / "build/runtime")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    run = Path(tempfile.mkdtemp(prefix="tui-", dir=root / "tests"))
    env = dict(os.environ)
    for key in list(env):
        if key.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_CONFIG_")):
            env.pop(key)
    env.update(HOME=str(run), CLAUDE_CONFIG_DIR=str(run / "config"),
               CLAUDE_TERMUX_ROOT=str(root), CLAUDE_TERMUX_AUTO_UPDATE="0",
               CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", TERM="xterm-256color")
    master, slave = pty.openpty()
    process = subprocess.Popen([str(REPO / "bin/claude-termux"), "--debug-file", str(run / "debug.log")],
                               stdin=slave, stdout=slave, stderr=slave, env=env, cwd=run, start_new_session=True,
                               preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
    os.close(slave)
    output = bytearray()

    def collect(duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline and process.poll() is None:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break

    try:
        collect(4)
        visible = b"Welcome" in output and json.loads((root / "runtime.json").read_text())["version"].encode() in output and b"Choose" in output
        os.write(master, b"\x03")
        collect(0.3)
        os.write(master, b"\x03")
        collect(10)
        graceful = process.poll() is not None
        third_press = False
        if not graceful:
            os.write(master, b"\x03")
            collect(3)
            third_press = process.poll() is not None
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        os.close(master)
    (run / "terminal.txt").write_bytes(output)
    report = {"onboarding_rendered": visible, "double_ctrl_c_exited": graceful,
              "third_ctrl_c_exited": third_press,
              "returncode": process.returncode, "artifacts": str(run)}
    (root / "tui-smoke.json").write_text(json.dumps(report, indent=2) + "\n")
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    raise SystemExit(not (visible and graceful))


if __name__ == "__main__":
    main()
