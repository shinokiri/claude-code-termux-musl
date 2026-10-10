#!/usr/bin/env python3
"""Build the musl DNS library and small Bionic execution helpers."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

REPO = Path(__file__).resolve().parents[1]
MUSL_HEADERS = {
    "url": "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/aarch64/musl-dev-1.2.6-r2.apk",
    "sha256": "ca72b84f4eb3c36bbbec04d919eb10159d414c7a43413cc2253841d301fa9486",
}


def build_dns(root, cache):
    spec = importlib.util.spec_from_file_location("prepare_runtime", REPO / "tools/prepare-runtime.py")
    prepare = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prepare)
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive = cache / "musl-dev.apk"
    prepare.download(MUSL_HEADERS["url"], archive, bytes.fromhex(MUSL_HEADERS["sha256"]))
    # Extract only headers from the verified package. Never use host Bionic
    # structures or compiler defaults to build a library loaded inside musl.
    with tempfile.TemporaryDirectory(prefix="musl-headers-", dir=cache) as directory:
        headers = Path(directory)
        with tarfile.open(archive, "r:*", ignore_zeros=True) as stream:
            for entry in stream:
                path = Path(entry.name)
                if not entry.name.startswith("usr/include/"):
                    continue
                if path.is_absolute() or ".." in path.parts or (not entry.isfile() and not entry.isdir()):
                    raise RuntimeError("Invalid musl header package member")
                if entry.isfile():
                    target = headers / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with stream.extractfile(entry) as source:
                        target.write_bytes(source.read())
        resources = subprocess.check_output(["clang", "-print-resource-dir"], text=True).strip()
        output = root / "libtermux-musl-dns.so.new"
        subprocess.run(["clang", "--target=aarch64-linux-musl", "-nostdlib", "-nostdinc",
                        "-isystem", resources + "/include", "-isystem", str(headers / "usr/include"),
                        "-O2", "-fPIC", "-shared", "-Wall", "-Wextra", "-Werror",
                        "-Wl,-soname,libtermux-musl-dns.so", "-o", str(output),
                        str(REPO / "libexec/termux-musl-dns.c"),
                        str(REPO / "libexec/termux-musl-resolver.c")], check=True)
        output.replace(root / "libtermux-musl-dns.so")
    print("Built libtermux-musl-dns.so", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO / ".runtime")
    parser.add_argument("--cache", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    prefix = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    build_dns(root, args.cache or root / "downloads")
    definitions = {
        "CLAUDE_BINARY": root / "claude",
        "CLAUDE_DISPATCH": root / "claude-dispatch",
        "CLAUDE_DNS_LIBRARY": root / "libtermux-musl-dns.so",
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
