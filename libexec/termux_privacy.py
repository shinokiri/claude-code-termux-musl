"""Persist optional-reporting policy and reject unsupported protected upgrades."""
import json
import os
from pathlib import Path
import subprocess
import sys

MODES = ("standard", "optional-off")
REQUIRED_POLICY_REVISION = 7


def profiles(release):
    files = [release / "libexec/privacy-profile.json", *sorted((release / "libexec/privacy-profiles").glob("*.json"))]
    found = {}
    for path in files:
        if path.is_file():
            profile = json.loads(path.read_text())
            if profile.get("policy_revision", 0) >= REQUIRED_POLICY_REVISION:
                found[profile["claude"]] = profile
    return found


def select_profile(release, version):
    profile = profiles(release).get(version)
    if profile is not None:
        (release / "libexec/privacy-profile.json").write_text(json.dumps(profile, indent=2) + "\n")
    return profile


def profile_index(release):
    return [{"version": p["claude"], "original_sha256": p["original_sha256"], "policy_revision": p["policy_revision"]}
            for p in profiles(release).values()]


def read_mode(data):
    path = data / "privacy-mode"
    if path.is_symlink():
        raise RuntimeError("Privacy mode must be a regular file")
    if path.exists() and not path.is_file():
        raise RuntimeError("Privacy mode must be a regular file")
    try:
        mode = path.read_text().removesuffix("\n")
    except FileNotFoundError:
        return "standard"
    if mode not in MODES:
        raise RuntimeError("Invalid privacy mode; expected standard or optional-off")
    return mode


def supported(release, record=None):
    profile_file = release / "libexec/privacy-profile.json"
    if not profile_file.is_file():
        return False
    if record is None:
        record = json.loads((release / "runtime/runtime.json").read_text())
    profile = json.loads(profile_file.read_text())
    return (profile.get("policy_revision", 0) >= REQUIRED_POLICY_REVISION
            and record.get("privacy") == "optional-v1" and record.get("version") == profile["claude"]
            and record.get("original_sha256") == profile["original_sha256"])


def require_support(release, mode, record=None):
    if mode == "optional-off" and not supported(release, record):
        raise RuntimeError("Candidate has no verified optional-off privacy profile at the required coverage level; current version and policy were kept")


def status(data, release):
    return {"mode": read_mode(data), "supported": supported(release),
            "scope": "Known optional reporters; business traffic and server-side collection remain",
            "existing_processes": "Restart Claude processes after changing mode"}


def set_mode(data, release, mode):
    if mode not in MODES:
        raise RuntimeError("Invalid privacy mode")
    require_support(release, mode)
    if mode == "optional-off":
        # Exercise actual native loading, producers, exporters, workers and
        # self-spawning before committing the user's selection.
        result = subprocess.run([sys.executable, str(release / "tests/privacy-smoke.py"),
                                 "--root", str(release / "runtime")], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError("Privacy self-test failed; mode unchanged:\n" + result.stdout[-1500:] + result.stderr[-1000:])
    path = data / "privacy-mode"
    temporary = data / "privacy-mode.new"
    try:
        with temporary.open("w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(mode + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return status(data, release)
