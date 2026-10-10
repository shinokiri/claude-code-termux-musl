#!/usr/bin/env python3
"""Build native compatibility libraries and the optional DNS proxy fallback."""
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


def build_compat(root, cache, prefix):
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
        output = root / "libtermux-musl-compat.so.new"
        subprocess.run(["clang", "--target=aarch64-linux-musl", "-nostdlib", "-nostdinc",
                        "-isystem", resources + "/include", "-isystem", str(headers / "usr/include"),
                        "-O2", "-fPIC", "-shared", "-Wall", "-Wextra", "-Werror",
                        "-Wl,-soname,libtermux-musl-compat.so",
                        "-DTERMUX_PREFIX=" + json.dumps(str(prefix)),
                        "-DBIONIC_EXEC_LIBRARY=" + json.dumps(str(root / "libtermux-bionic-exec.so")),
                        "-o", str(output), str(REPO / "libexec/termux-exec.c"),
                        str(REPO / "libexec/termux-musl-dns.c"),
                        str(REPO / "libexec/termux-musl-resolver.c")], check=True)
        output.replace(root / "libtermux-musl-compat.so")
    output = root / "libtermux-bionic-exec.so.new"
    subprocess.run(["clang", "-O2", "-fPIC", "-shared", "-Wall", "-Wextra", "-Werror",
                    "-DTERMUX_PREFIX=" + json.dumps(str(prefix)),
                    "-DBIONIC_EXEC_LIBRARY=" + json.dumps(str(root / "libtermux-bionic-exec.so")),
                    "-o", str(output), str(REPO / "libexec/termux-exec.c")], check=True)
    output.replace(root / "libtermux-bionic-exec.so")
    # Schema-1 managers shipped in 1.1 detect native DNS by this filename when
    # rolling forward to a newer release. The runtime loads the new name only.
    alias = root / "libtermux-musl-dns.so"
    alias.unlink(missing_ok=True)
    alias.symlink_to("libtermux-musl-compat.so")
    print("Built native musl/Bionic compatibility libraries", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO / "build/runtime")
    parser.add_argument("--cache", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    prefix = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (root.parent / "files.json").exists():
        raise RuntimeError("Build in a new candidate directory; installed releases are immutable")
    build_compat(root, args.cache or root / "downloads", prefix)
    for name in ["termux-http-proxy"]:
        command = ["clang", "-O2", "-Wall", "-Wextra"]
        output = root / (name + ".new")
        command += [str(REPO / "libexec" / (name + ".c")), "-o", str(output)]
        subprocess.run(command, check=True)
        output.replace(root / name)
        print("Built", name, flush=True)


if __name__ == "__main__":
    main()
