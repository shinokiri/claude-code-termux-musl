#!/usr/bin/env python3
"""Build a deterministic, source-only GitHub release; no Claude binary included."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "libexec"))
import termux_manage as manage
from termux_update import version


def package(destination):
    identity = json.loads((REPO / "adapter.json").read_text())
    version(identity["version"])
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / "claude-termux-adapter.tar.gz"
    with archive.open("wb") as target, gzip.GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as stream:
            for name in sorted(manage.source_files()):
                source = REPO / name
                if source.is_symlink() or not source.is_file():
                    raise RuntimeError("Package sources must be regular files: " + name)
                value = source.read_bytes()
                member = tarfile.TarInfo(name)
                member.size = len(value)
                member.mode = 0o700 if source.stat().st_mode & 0o111 else 0o600
                stream.addfile(member, io.BytesIO(value))
    manifest = dict(identity, sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
    (destination / "update.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    package(parser.parse_args().output)
