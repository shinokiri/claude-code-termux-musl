#!/usr/bin/env python3
"""Expose this checkout's tested launcher as `claude` in a user bin directory."""
import argparse
import os
from pathlib import Path
import subprocess

REPO = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin-dir", type=Path, default=Path.home() / ".local/bin")
    args = parser.parse_args()
    launcher = REPO / "bin/claude-termux"
    directory = args.bin_dir.expanduser().absolute()
    command = directory / "claude"
    if os.path.lexists(command):
        if command.is_symlink() and command.resolve() == launcher:
            print("Already installed:", command)
            return
        parser.error(str(command) + " already exists; leaving it untouched")
    subprocess.run([str(launcher), "--version"], check=True, timeout=30)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    command.symlink_to(launcher)
    print("Installed:", command, "->", launcher)
    path = [Path(item).absolute() for item in os.get_exec_path() if item]
    if directory not in path:
        print("Add this directory to your shell's PATH:", directory)


if __name__ == "__main__":
    main()
