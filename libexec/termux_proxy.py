"""Build the explicitly requested diagnostic proxy in a disposable shared cache."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess

APP = Path(__file__).resolve().parents[1]


def ensure_proxy(root):
    legacy = root / "termux-http-proxy"
    if legacy.is_file():
        return legacy
    source = APP / "libexec/termux-http-proxy.c"
    manifest = root.parent / "installation.json"
    cache = (Path(json.loads(manifest.read_text())["data_dir"]) / "cache"
             if manifest.is_file() else root) / "optional-proxy"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    compiler = subprocess.check_output(["clang", "--version"], text=True)
    identity = hashlib.sha256(source.read_bytes() + compiler.encode()).hexdigest()
    binary = cache / identity
    receipt = cache / (identity + ".json")
    with (cache / "build.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if binary.is_file() and receipt.is_file():
            expected = json.loads(receipt.read_text())
            if expected.get("sha256") == hashlib.sha256(binary.read_bytes()).hexdigest():
                return binary
        temporary = cache / (identity + ".new")
        try:
            subprocess.run(["clang", "-O2", "-Wall", "-Wextra", str(source), "-o", str(temporary)], check=True)
            temporary.chmod(0o700)
            record = {"sha256": hashlib.sha256(temporary.read_bytes()).hexdigest(), "source_and_compiler": identity}
            temporary.replace(binary)
            receipt.write_text(json.dumps(record) + "\n")
            os.chmod(receipt, 0o600)
        finally:
            temporary.unlink(missing_ok=True)
    return binary
