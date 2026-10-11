#!/usr/bin/env python3
"""Prepare a private, checksum-verified ARM64 musl runtime for device testing."""
import argparse
import base64
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

MUSL = {
    "version": "1.2.6-r2",
    "url": "https://dl-cdn.alpinelinux.org/alpine/v3.24/main/aarch64/musl-1.2.6-r2.apk",
    "sha256": "5e9674b7f41152fe2119093b5cb4c13eaaadb19c2d5422b2d7267913e663ee6e",
    "loader_sha256": "32377e6d71725bb019e9ff6d5e9f16b4d5156d6f2c36504191c2d6a7c4d4a44d",
}
PATCHELF_REV = "382d8bc12db9e720515da49d9c272ef4a622c3c0"
REPO = Path(__file__).resolve().parents[1]


def digest(path, algorithm="sha256"):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, algorithm).digest()


def download(url, path, expected, algorithm="sha256"):
    if path.exists() and digest(path, algorithm) == expected:
        return
    part = path.with_suffix(path.suffix + ".part")
    print("Downloading", url.rsplit("/", 1)[-1], flush=True)
    try:
        with urllib.request.urlopen(url, timeout=45) as response, part.open("wb") as out:
            shutil.copyfileobj(response, out, 1024 * 1024)
        if digest(part, algorithm) != expected:
            raise RuntimeError("Download checksum mismatch: " + url)
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)


def member(archive, name, destination):
    # Read only an expected regular file; never unpack arbitrary archive paths.
    with tarfile.open(archive, "r:*", ignore_zeros=True) as tf:
        matches = [m for m in tf if m.name.lstrip("./") == name]
        if len(matches) != 1 or not matches[0].isfile():
            raise RuntimeError("Missing/ambiguous regular archive member: " + name)
        with tf.extractfile(matches[0]) as src, destination.open("wb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
    destination.chmod(0o700)


def prepare_patchelf(root):
    patcher = root / "tools/patchelf"
    if patcher.exists():
        result = subprocess.run([str(patcher), "--version"], capture_output=True)
        if result.returncode == 0:
            return patcher
    # Compile against this phone's libc++; a newer Termux .deb may require a
    # newer libc++ than the installed one. Do not upgrade the user's packages.
    source = root / "tools/patchelf-src"
    if not source.exists():
        source.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        subprocess.run(["git", "-C", str(source), "fetch", "--depth=1",
                        "https://github.com/NixOS/patchelf.git", PATCHELF_REV], check=True)
        subprocess.run(["git", "-C", str(source), "checkout", "-q", "FETCH_HEAD"], check=True)
    revision = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
    if revision != PATCHELF_REV:
        raise RuntimeError("Unexpected patchelf source revision")
    subprocess.run(["clang++", "-std=c++17", "-O0", str(source / "src/patchelf.cc"),
                    "-o", str(patcher)], check=True)
    subprocess.run([str(patcher), "--version"], check=True)
    return patcher


def check_runtime(binary, root, version):
    env = dict(os.environ)
    for name in list(env):
        if name in ("LD_PRELOAD", "LD_LIBRARY_PATH", "BUN_OPTIONS", "NODE_OPTIONS") or name.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_CONFIG_")):
            env.pop(name, None)
    # General startup checks run independently of the selected privacy policy.
    # The manager has a separate mandatory gate when optional-off is selected.
    env["CLAUDE_TERMUX_PRIVACY"] = "standard"
    with tempfile.TemporaryDirectory(prefix="verify-", dir=root / "tmp") as directory:
        env.update(CLAUDE_CONFIG_DIR=directory + "/config", TMPDIR=directory,
                   CLAUDE_CODE_TMPDIR=directory, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                   DISABLE_AUTOUPDATER="1", DISABLE_UPDATES="1")
        for option in ("--version", "--help", "--init-only"):
            result = subprocess.run([str(binary), option], env=env, cwd=directory,
                                    capture_output=True, text=True, timeout=30)
            if result.returncode or (option == "--version" and result.stdout.strip() != version + " (Claude Code)"):
                raise RuntimeError("Candidate failed " + option + ": " + result.stdout[-2000:] + result.stderr[-2000:])


def copy_backup(source, destination):
    # Android SELinux can forbid hard links even within Termux private storage.
    temporary = destination.with_name(destination.name + ".copy")
    try:
        shutil.copyfile(source, temporary)
        temporary.chmod(source.stat().st_mode & 0o777)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def activate(root, candidate, record):
    # Check before touching the working installation. Keep one previous binary
    # without hard links; rename does not interrupt already-running sessions.
    check_runtime(candidate, root, record["version"])
    binary, record_path = root / "claude", root / "runtime.json"
    if binary.exists() and record_path.exists():
        previous = root / "claude.prev"
        copy_backup(binary, previous)
        copy_backup(record_path, root / "runtime.prev.json")
    temporary_record = root / "runtime.new.json"
    temporary_record.write_text(json.dumps(record, indent=2) + "\n")
    candidate.replace(binary)
    temporary_record.replace(record_path)


def prepare(root, version, shared=None):
    if (root.parent / "files.json").exists():
        raise RuntimeError("Prepare a new candidate; installed releases are immutable")
    for name in ["lib", "tools", "downloads", "home", "tmp"]:
        (root / name).mkdir(exist_ok=True, mode=0o700)
    build_root = shared or root
    for name in ("downloads", "tools"):
        (build_root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    cache = build_root / "downloads"
    loader = root / "lib/ld-musl-aarch64.so.1"
    if not loader.exists() or digest(loader).hex() != MUSL["loader_sha256"]:
        apk = cache / "musl.apk"
        download(MUSL["url"], apk, bytes.fromhex(MUSL["sha256"]))
        member(apk, "lib/ld-musl-aarch64.so.1", loader)
        if digest(loader).hex() != MUSL["loader_sha256"]:
            raise RuntimeError("musl loader checksum mismatch")
        if shared is None:
            apk.unlink()
    patcher = prepare_patchelf(build_root)
    subprocess.run([sys.executable, str(REPO / "tools/build-bridges.py"), "--root", str(root),
                    "--cache", str(cache)], check=True)
    package = "@anthropic-ai/claude-code-linux-arm64-musl"
    with urllib.request.urlopen("https://registry.npmjs.org/" + package + "/" + version, timeout=30) as response:
        metadata = json.load(response)
    if metadata["version"] != version:
        raise RuntimeError("Registry version mismatch")
    integrity = metadata["dist"]["integrity"]
    algorithm, value = integrity.split("-", 1)
    if algorithm != "sha512":
        raise RuntimeError("Expected npm SHA-512 integrity")
    binary = root / "claude"
    record_path = root / "runtime.json"
    record = json.loads(record_path.read_text()) if record_path.exists() else {}
    if not binary.exists() or record.get("privacy") != "optional-v1" or record.get("execution") != "native-v1" or record.get("version") != version or record.get("root") != str(root) or record.get("patched_sha256") != digest(binary).hex():
        archive = cache / "claude.tgz"
        download(metadata["dist"]["tarball"], archive, base64.b64decode(value, validate=True), "sha512")
        candidate = root / "claude.candidate"
        try:
            member(archive, "package/claude", candidate)
            original_hash = digest(candidate).hex()
            subprocess.run([str(patcher), "--set-interpreter", str(loader), str(candidate)], check=True)
            spec = importlib.util.spec_from_file_location("patch_runtime", REPO / "tools/patch-runtime.py")
            patch = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(patch)
            patch.add_dependency(candidate, root / "libtermux-musl-compat.so")
            record = {"version": version, "source": metadata["dist"]["tarball"], "integrity": integrity,
                      "original_sha256": original_hash, "patched_sha256": digest(candidate).hex(), "musl": MUSL,
                      "interpreter": str(loader), "patch": "ELF interpreter and layout-preserving DT_NEEDED",
                      "execution": "native-v1", "privacy": "optional-v1", "release_lease": 1, "root": str(root)}
            activate(root, candidate, record)
        finally:
            candidate.unlink(missing_ok=True)
        if shared is None:
            archive.unlink()
    else:
        check_runtime(binary, root, version)
    print(json.dumps(record, indent=2), flush=True)
    print("Verified --version, --help and --init-only", flush=True)


def rollback(root):
    previous = root / "claude.prev"
    record_path = root / "runtime.prev.json"
    if not previous.exists() or not record_path.exists():
        raise RuntimeError("No previous runtime to roll back to")
    record = json.loads(record_path.read_text())
    if record.get("patched_sha256") != digest(previous).hex():
        raise RuntimeError("Previous runtime checksum mismatch")
    check_runtime(previous, root, record["version"])
    # Stage the previous version before activate() rotates the backup.
    candidate = root / "claude.candidate"
    candidate.unlink(missing_ok=True)
    copy_backup(previous, candidate)
    try:
        activate(root, candidate, record)
    finally:
        candidate.unlink(missing_ok=True)
    print("Rolled back to", record["version"])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=REPO / "build/runtime")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--version", default="2.1.296")
    mode.add_argument("--rollback", action="store_true")
    args = p.parse_args()
    if os.uname().machine != "aarch64":
        p.error("This prototype supports aarch64 Termux only")
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.rollback:
            rollback(root)
        else:
            prepare(root, args.version)


if __name__ == "__main__":
    main()
