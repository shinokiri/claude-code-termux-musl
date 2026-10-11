"""Follow official Claude releases and published Termux adapter releases."""
import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from termux_trust import UpdateTrustError, verify_manifest
import termux_privacy as privacy

APP = Path(__file__).resolve().parents[1]
PACKAGE = "@anthropic-ai/claude-code-linux-arm64-musl"
REPOSITORY = "https://github.com/shinokiri/claude-code-termux-musl"
MANIFEST_URL = REPOSITORY + "/releases/latest/download/update.json"
DEFAULTS = {"automatic": True, "channel": "latest", "interval_seconds": 3600,
            "pinned_version": None, "paused": False}


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError("Expected an exact numeric version, got " + repr(value))
    return tuple(int(part) for part in value.split("."))


def fetch(url, limit=128 * 1024):
    if not url.startswith("https://"):
        raise ValueError("Update downloads require HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent": "claude-termux-updater/1", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=20) as response:
        if not response.url.startswith("https://"):
            raise RuntimeError("Refusing an insecure update redirect")
        value = response.read(limit + 1)
    if len(value) > limit:
        raise RuntimeError("Update response exceeds size limit")
    return value


def resolve_version(selector):
    if selector not in ("latest", "stable"):
        version(selector)
        return selector
    metadata = json.loads(fetch("https://registry.npmjs.org/" + PACKAGE + "/" + selector))
    if metadata.get("name") != PACKAGE:
        raise RuntimeError("Unexpected official package identity")
    version(metadata.get("version"))
    return metadata["version"]


def settings(data):
    saved = read_json(data / "update-settings.json", {})
    if not isinstance(saved, dict):
        raise ValueError("update-settings.json must be an object")
    value = dict(DEFAULTS, **saved)
    if value["channel"] not in ("latest", "stable"):
        raise ValueError("Update channel must be latest or stable")
    if type(value["automatic"]) is not bool or type(value["paused"]) is not bool:
        raise ValueError("automatic and paused must be booleans")
    if type(value["interval_seconds"]) is not int or not 60 <= value["interval_seconds"] <= 86400:
        raise ValueError("Update interval must be 60..86400 seconds")
    if value["pinned_version"] is not None:
        version(value["pinned_version"])
    return value


def status(data):
    return {"settings": settings(data),
            "installed_claude": read_json(data / "current/runtime/runtime.json", {}).get("version"),
            "installed_adapter": read_json(data / "current/adapter.json", {}).get("version", "unversioned"),
            "last_attempt": read_json(data / "update-status.json", {}),
            "log": str(data / "update.log")}


def due(data, now=None):
    options = settings(data)
    if not options["automatic"] or options["paused"]:
        return False
    last = read_json(data / "update-status.json", {}).get("checked_at", 0)
    now = time.time() if now is None else now
    # A clock adjustment must not suppress checks indefinitely.
    return now < last or now - last >= options["interval_seconds"]


def adapter_manifest():
    raw = fetch(MANIFEST_URL)
    try:
        manifest = json.loads(raw)
        if manifest.get("schema") != 1:
            raise ValueError("Unsupported manifest")
        version(manifest.get("version"))
        if not isinstance(manifest.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", manifest["sha256"]):
            raise ValueError("Invalid archive checksum")
    except (ValueError, AttributeError) as error:
        raise UpdateTrustError("Invalid adapter release manifest") from error
    signature_url = REPOSITORY + "/releases/download/adapter-v" + manifest["version"] + "/update.json.sig"
    try:
        signature = fetch(signature_url, limit=4096)
    except Exception as error:
        raise UpdateTrustError("Cannot authenticate adapter release signature; current installation retained") from error
    verify_manifest(raw, signature)
    return manifest


def protected_target(current, wanted, selector, index):
    """Preflight only; artifact hashes and behavioral checks remain mandatory."""
    if not isinstance(index, list):
        raise UpdateTrustError("Invalid privacy profile index")
    verified = set()
    for entry in index:
        if not isinstance(entry, dict):
            raise UpdateTrustError("Invalid privacy profile entry")
        candidate = entry.get("version")
        try:
            version(candidate)
        except ValueError as error:
            raise UpdateTrustError("Invalid privacy profile version") from error
        checksum = entry.get("original_sha256")
        revision = entry.get("policy_revision")
        if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum) or type(revision) is not int:
            raise UpdateTrustError("Invalid privacy profile index")
        if revision >= privacy.REQUIRED_POLICY_REVISION:
            verified.add(candidate)
    if wanted in verified:
        return wanted
    if selector not in ("latest", "stable"):
        return None
    eligible = [v for v in verified if version(current) <= version(v) <= version(wanted)]
    return max(eligible, key=version) if eligible else None


def unpack_adapter(manifest, destination):
    url = REPOSITORY + "/releases/download/adapter-v" + manifest["version"] + "/claude-termux-adapter.tar.gz"
    archive = fetch(url, limit=4 * 1024 * 1024)
    if hashlib.sha256(archive).hexdigest() != manifest["sha256"]:
        raise RuntimeError("Adapter archive checksum mismatch")
    seen, total = set(), 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as stream:
        for member in stream:
            name = PurePosixPath(member.name)
            total += member.size
            if (name.is_absolute() or ".." in name.parts or not name.parts
                    or member.name != str(name) or member.name in seen
                    or not member.isfile() or total > 8 * 1024 * 1024 or len(seen) >= 300):
                raise RuntimeError("Unsafe adapter archive member")
            if name.parts[0] not in ("bin", "libexec", "tools", "tests", "docs", "README.md", "LICENSE", "adapter.json"):
                raise RuntimeError("Unexpected adapter archive member")
            seen.add(member.name)
            path = destination.joinpath(*name.parts)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with stream.extractfile(member) as source, path.open("xb") as target:
                target.write(source.read())
            path.chmod(0o700 if member.mode & 0o111 else 0o600)
    identity = read_json(destination / "adapter.json", {})
    if identity != {"schema": 1, "version": manifest["version"]}:
        raise RuntimeError("Adapter archive identity mismatch")
    for name in ("tools/install-user.py", "libexec/termux_manage.py", "libexec/termux_update.py"):
        if not (destination / name).is_file():
            raise RuntimeError("Incomplete adapter archive")


def run(manage, data, config, bin_dir, lock_fd, requested=None, channel=None, check=False, background=False):
    """Called with the installation lock held; only accepted candidates activate."""
    if background and not due(data):
        return
    if background:
        # One attempt replaces its log, so a long-running session cannot grow it
        # without bound. The installation lock also serializes multiple sessions.
        with (data / "update.log").open("w") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            try:
                _run(manage, data, config, bin_dir, lock_fd, requested, channel, check, background)
            except (OSError, ValueError, RuntimeError, UpdateTrustError, subprocess.SubprocessError, tarfile.TarError) as error:
                print("Update failed:", error, flush=True)
    else:
        _run(manage, data, config, bin_dir, lock_fd, requested, channel, check, background)


def _run(manage, data, config, bin_dir, lock_fd, requested, channel, check, background):
    options = settings(data)
    # Explicit `update` unpins a manual version and resumes a rollback pause.
    # A background attempt keeps both choices untouched.
    selector = (options["pinned_version"] or options["channel"]) if background else (requested or channel or options["channel"])
    current = manage.release_at(data, "current")
    current_version = read_json(current / "runtime/runtime.json")["version"]
    current_adapter = read_json(current / "adapter.json", {}).get("version", "0.0.0")
    report = {"checked_at": time.time(), "result": "checking", "channel": channel or options["channel"]}
    if not check:
        manage.atomic_json(data / "update-status.json", report)
    try:
        target = resolve_version(selector)
        # Channel changes never silently downgrade an installed binary. Exact
        # --version is the explicit mechanism for a downgrade and pin.
        if selector in ("latest", "stable") and version(target) < version(current_version):
            target = current_version
        report.update(current_version=current_version, target_version=target, current_adapter=current_adapter)
        manifest, adapter_error = None, None
        try:
            manifest = adapter_manifest()
        except (OSError, ValueError, RuntimeError) as error:
            # A GitHub outage must not prevent getting an official Claude update
            # that works with the already-installed adapter.
            adapter_error = str(error)
            print("Warning: adapter update check unavailable; retaining the installed adapter.", file=sys.stderr, flush=True)
        adapter_new = manifest is not None and version(manifest["version"]) > version(current_adapter)
        report["target_adapter"] = manifest["version"] if adapter_new else current_adapter
        if privacy.read_mode(data) == "optional-off":
            index = manifest.get("privacy_profiles", []) if adapter_new else privacy.profile_index(current)
            chosen = protected_target(current_version, target, selector, index)
            if chosen != target:
                report["privacy_hold"] = {"official_target": target, "reason": "Awaiting a verified optional-off profile"}
                if chosen is None:
                    adapter_new = False
                    report["target_adapter"] = current_adapter
                target = chosen or current_version
                report["target_version"] = target
        changed = target != current_version or adapter_new
        if check:
            report.update(result="available" if changed else "deferred" if "privacy_hold" in report else "current")
            if adapter_error:
                report["adapter_error"] = adapter_error
            print(json.dumps(report, indent=2))
            return
        if changed:
            manage.validate_release(current)
            print("Updating Claude Code", current_version, "->", target,
                  "; adapter", current_adapter, "->", report["target_adapter"], flush=True)
            if adapter_new:
                # The downloaded installer shares the parent's open lock file
                # description. No lock release/handoff race, and no Git checkout.
                with tempfile.TemporaryDirectory(prefix="adapter-", dir=data) as directory:
                    source = Path(directory)
                    unpack_adapter(manifest, source)
                    command = [sys.executable, str(source / "tools/install-user.py"), "--version", target,
                               "--data-dir", str(data), "--config-dir", str(config), "--bin-dir", str(bin_dir),
                               "--lock-fd", str(lock_fd)]
                    with tempfile.TemporaryFile(mode="w+") as output:
                        result = subprocess.run(command, pass_fds=(lock_fd,), stdout=output, stderr=subprocess.STDOUT, timeout=900)
                        output.seek(0)
                        details = output.read()
                        print(details, end="", flush=True)
                    if result.returncode:
                        raise RuntimeError("Adapter candidate failed validation (exit " + str(result.returncode) + ")")
            else:
                manage.install(data, config, bin_dir, target)
            installed = manage.release_at(data, "current")
            actual = manage.validate_release(installed)
            if actual["version"] != target or read_json(installed / "adapter.json", {}).get("version") != report["target_adapter"]:
                raise RuntimeError("Installer did not activate the requested Claude and adapter versions")
            report["result"] = "updated"
        else:
            report["result"] = "deferred" if "privacy_hold" in report else "current"
            if "privacy_hold" not in report:
                print("Claude Code", current_version, "and Termux adapter", current_adapter, "are up to date.", flush=True)
        if "privacy_hold" in report:
            print("Official Claude", report["privacy_hold"]["official_target"],
                  "awaits privacy verification; keeping verified Claude", target, "without weakening optional-off.", flush=True)
        if not background:
            options["channel"] = channel or (requested if requested in ("latest", "stable") else options["channel"])
            options["pinned_version"] = requested if requested and requested not in ("latest", "stable") else None
            options["paused"] = False
            manage.atomic_json(data / "update-settings.json", options)
        if adapter_error:
            report.update(result="partial", adapter_error=adapter_error)
        report["completed_at"] = time.time()
        report["release"] = manage.release_at(data, "current").name
        manage.atomic_json(data / "update-status.json", report)
        if changed:
            print("Update ready. New Claude sessions will use it; existing sessions keep running.", flush=True)
    except (OSError, ValueError, RuntimeError, UpdateTrustError, subprocess.SubprocessError, tarfile.TarError) as error:
        if not check:
            report.update(result="failed", completed_at=time.time(), error=str(error))
            manage.atomic_json(data / "update-status.json", report)
        raise


def process_identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[19] if fields[0] != "Z" else None
    except (OSError, IndexError):
        return None


def start_monitor(installation, arguments):
    """No network or waiting on the foreground launch path."""
    if (not installation or not sys.stdin.isatty() or not sys.stdout.isatty()
            or os.environ.get("CLAUDE_TERMUX_SESSION") or os.environ.get("CLAUDE_TERMUX_ROOT")
            or os.environ.get("CLAUDE_TERMUX_AUTO_UPDATE") == "0"
            or os.environ.get("DISABLE_AUTOUPDATER") == "1" or os.environ.get("DISABLE_UPDATES") == "1"
            or any(arg in ("-p", "--print", "--version", "-v", "--help", "-h", "--init-only", "--bg") for arg in arguments)
            or arguments[:1] in (["auth"], ["daemon"], ["mcp"], ["doctor"], ["install"], ["plugin"])):
        return
    data = Path(installation["data_dir"])
    options = settings(data)
    if not options["automatic"] or options["paused"]:
        return
    last = read_json(data / "update-status.json", {})
    if last.get("privacy_hold"):
        print("Claude Termux: a newer official version awaits privacy verification; the verified version remains active. See claude-termux-manage updates.", file=sys.stderr)
    elif last.get("result") in ("failed", "partial"):
        print("Claude Termux: the last update was incomplete; run claude-termux-manage updates for details.", file=sys.stderr)
    identity = process_identity(os.getpid())
    if identity is None:
        return
    env = dict(os.environ)
    # A detached updater needs no model credentials or parent-owned DNS proxy.
    inherited = env.pop("CLAUDE_TERMUX_MANAGED_PROXY", None)
    for key in list(env):
        if key.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "CLAUDE_CONFIG_")) or (inherited and env[key] == inherited):
            env.pop(key)
    env["CLAUDE_TERMUX_AUTO_UPDATE"] = "0"
    subprocess.Popen([sys.executable, str(APP / "libexec/termux_update.py"), "--watch", str(data),
                      "--parent", str(os.getpid()), "--identity", identity],
                     cwd=data, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def watch(data, parent, identity):
    # A small watcher follows each top-level interactive session. Shared due
    # timestamps and a nonblocking installation lock deduplicate actual work.
    while process_identity(parent) == identity:
        try:
            options = settings(data)
            if not options["automatic"]:
                return
            if due(data):
                subprocess.run([sys.executable, str(data / "current/bin/claude-termux-manage"),
                                "_auto-update", "--data-dir", str(data)], timeout=1200)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            # A failed attempt is reported by the manager; never affect Claude.
            pass
        time.sleep(30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", type=Path, required=True)
    parser.add_argument("--parent", type=int, required=True)
    parser.add_argument("--identity", required=True)
    args = parser.parse_args()
    watch(args.watch, args.parent, args.identity)
