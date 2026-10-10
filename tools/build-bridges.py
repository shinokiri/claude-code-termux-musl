#!/usr/bin/env python3
"""Build small Bionic helpers against this device's Termux libraries."""
import argparse
import json
import os
from pathlib import Path
import subprocess

REPO = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO / ".runtime")
    args = parser.parse_args()
    root = args.root.resolve()
    prefix = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    definitions = {
        "CLAUDE_BINARY": root / "claude",
        "CLAUDE_DISPATCH": root / "claude-dispatch",
        "TERMUX_BASH": prefix / "bin/bash",
        "TERMUX_PRELOAD": prefix / "lib/libtermux-exec-ld-preload.so",
    }
    for name in ["claude-dispatch", "termux-shell", "termux-http-proxy"]:
        command = ["clang", "-O2", "-Wall", "-Wextra"]
        command += ["-D" + key + "=" + json.dumps(str(value)) for key, value in definitions.items()]
        output = root / (name + ".new")
        command += [str(REPO / "libexec" / (name + ".c")), "-o", str(output)]
        subprocess.run(command, check=True)
        output.replace(root / name)
        print("Built", name, flush=True)


if __name__ == "__main__":
    main()
