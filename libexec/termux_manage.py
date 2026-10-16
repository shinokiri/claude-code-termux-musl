"""Install and switch complete, tested Claude Code releases on Termux."""
import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
import termux_privacy as privacy
from termux_trust import UpdateTrustError

APP = Path(__file__).resolve().parents[1]
BINARIES = ("claude-termux", "claude-process-wrapper", "claude-termux-manage")


def read_json(path):
    return json.loads(path.read_text())


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".new")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_link(path, target):
    temporary = path.with_name(path.name + ".new-" + uuid.uuid4().hex[:8])
    try:
        temporary.symlink_to(target)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def preparer():
    spec = importlib.util.spec_from_file_location("prepare_runtime", APP / "tools/prepare-runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def defaults():
    manifest = APP / "installation.json"
    if manifest.exists():
        return read_json(manifest)
    home = Path.home()
    return {
        "data_dir": str(Path(os.environ.get("XDG_DATA_HOME", home / ".local/share")) / "claude-termux"),
        "config_dir": os.environ.get("CLAUDE_CONFIG_DIR") or str(Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "claude-termux/claude"),
        "bin_dir": str(home / ".local/bin"),
    }


def release_at(data, name, required=True):
    pointer = data / name
    if not os.path.lexists(pointer) and not required:
        return None
    if not pointer.is_symlink():
        raise RuntimeError(str(pointer) + " is not a managed release link")
    release = pointer.resolve(strict=True)
    if release.parent != (data / "releases").resolve() or not (release / "installation.json").is_file():
        raise RuntimeError("Release link points outside this installation: " + str(pointer))
    return release


def validate_release(release):
    prepare = preparer()
    manifest = read_json(release / "installation.json")
    runtime = release / "runtime"
    record = read_json(runtime / "runtime.json")
    if Path(manifest["data_dir"]).resolve() != release.parent.parent or record.get("root") != str(runtime):
        raise RuntimeError("Release was moved; reinstall at its final path")
    for name, expected in read_json(release / "files.json").items():
        if prepare.digest(release / name).hex() != expected:
            raise RuntimeError("Installed file checksum mismatch: " + name)
    return record


def check_requirements():
    if os.uname().machine != "aarch64" or not Path("/data/data/com.termux/files/usr").is_dir():
        raise RuntimeError("This adapter supports ARM64 Termux only")
    api = subprocess.check_output(["/system/bin/getprop", "ro.build.version.sdk"], text=True).strip()
    if not api.isdecimal() or int(api) < 29:
        raise RuntimeError("Native Android DNS requires Android 10 / API 29 or later")
    required = ("python3", "clang", "clang++", "git", "bash", "node", "curl", "grep", "find")
    missing = [name for name in required if not shutil.which(name)]
    if missing:
        raise RuntimeError("Missing build/test tools: " + ", ".join(missing))
    prefix = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"))
    for name in ("lib/libtermux-exec-ld-preload.so", "etc/tls/cert.pem", "include/openssl/x509.h", "lib/libcrypto.so"):
        if not (prefix / name).is_file():
            raise RuntimeError("Missing Termux runtime dependency: " + str(prefix / name))
    fixture_runner().check_capability()


def fixture_runner():
    spec = importlib.util.spec_from_file_location("fixture_runtime", APP / "tests/fixture_runtime.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    return fixture


def wrapper_value(data):
    path = str(data / "current/bin/claude-process-wrapper")
    return json.dumps([path]) if any(c.isspace() for c in path) else path


def check_settings(config, data, native=True):
    path = config / "settings.json"
    settings = read_json(path) if path.exists() else {}
    if not isinstance(settings, dict) or not isinstance(settings.get("env", {}), dict):
        raise RuntimeError("Claude settings.json must be an object with an object env block")
    expected = wrapper_value(data)
    for existing in (settings.get("env", {}).get("CLAUDE_CODE_PROCESS_WRAPPER"), settings.get("processWrapper"), os.environ.get("CLAUDE_CODE_PROCESS_WRAPPER")):
        if not native and existing and existing != expected:
            raise RuntimeError("An existing Claude process wrapper is configured; reconcile it before installing this adapter")
    return settings


def execution_settings(settings, data, native):
    """Migrate only values owned by this installation; retain all privacy flags."""
    settings = json.loads(json.dumps(settings))
    expected = wrapper_value(data)
    if native:
        if settings.get("env", {}).get("CLAUDE_CODE_PROCESS_WRAPPER") == expected:
            del settings["env"]["CLAUDE_CODE_PROCESS_WRAPPER"]
        if settings.get("processWrapper") == expected:
            del settings["processWrapper"]
    else:
        settings.setdefault("env", {})["CLAUDE_CODE_PROCESS_WRAPPER"] = expected
    return settings


def migrate_legacy_settings(config, data):
    """Finish migration after a 1.1 manager rolls forward to a native release.

    New installers migrate before promotion. This path only repairs the exact
    owned legacy value, and never waits for an update holding the install lock.
    """
    settings = check_settings(config, data)
    if execution_settings(settings, data, True) == settings:
        return
    with (data / "install.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        settings = check_settings(config, data)
        changed = execution_settings(settings, data, True)
        if changed != settings:
            atomic_json(config / "settings.json", changed)


def command_links(data, bin_dir):
    return {bin_dir / "claude": data / "current/bin/claude-termux",
            bin_dir / "claude-termux-manage": data / "current/bin/claude-termux-manage"}


def check_links(data, bin_dir):
    for path, target in command_links(data, bin_dir).items():
        if not os.path.lexists(path):
            continue
        if path.is_symlink() and (path.readlink() == target or path.resolve() == APP / "bin/claude-termux"):
            continue
        raise RuntimeError("Existing command belongs to another installation: " + str(path))


def source_files():
    # Explicit source list: never copy a checkout's runtime, Git directory,
    # credentials, downloaded binaries, or arbitrary untracked files.
    files = ["README.md", "LICENSE", "adapter.json"]
    files += [str(path.relative_to(APP)) for path in sorted((APP / "docs").glob("*.md"))]
    files += ["docs/audits/2.1.296-dataflow.json", "docs/audits/2.1.296-project-privacy.json", "docs/audits/2.1.296-collector-privacy.json", "docs/audits/2.1.296-request-privacy.json"]
    files += ["docs/audits/2.1.296-tool-timing.json"]
    files += ["docs/audits/2.1.296-artifact-privacy.json"]
    files += ["docs/audits/2.1.301-privacy-update.json"]
    files += ["bin/" + name for name in BINARIES]
    files += ["libexec/" + name for name in ("termux-exec.c", "termux-http-proxy.c", "termux-musl-dns.c", "termux-musl-resolver.c", "termux_launch.py", "termux_manage.py", "termux_update.py", "termux_proxy.py", "termux_storage.py")]
    files += ["libexec/" + name for name in ("termux-privacy.c", "termux-privacy.mjs", "termux-privacy-network.cjs", "privacy-profile.json", "termux_privacy.py")]
    files += ["libexec/" + name for name in ("termux_trust.py", "release-signature.cjs", "release-keys.json")]
    files += ["libexec/termux-privacy-profile.mjs", "libexec/termux-privacy-project.mjs", "libexec/termux-privacy-requests.mjs"]
    files += [str(path.relative_to(APP)) for path in sorted((APP / "libexec/privacy-profiles").glob("*.json"))]
    files += ["tools/" + name for name in ("prepare-runtime.py", "patch-runtime.py", "build-bridges.py", "install-user.py")]
    files += ["tests/" + name for name in ("native-exec-smoke.py", "native-exec-probe.mjs", "native-exec-driver.c", "device-smoke.py", "proxy-smoke.py", "process-wrapper-smoke.py", "dns-smoke.py", "dns-probe.mjs", "dns-worker.mjs", "network-smoke.py", "network-probe.mjs", "tls-cert.c")]
    files += ["tests/" + name for name in ("privacy-smoke.py", "privacy-probe.mjs", "privacy-plugin.mjs", "privacy-project.mjs", "privacy-collectors.mjs", "privacy-requests.mjs", "privacy-artifacts.mjs", "privacy-query-probe.mjs", "privacy-receiver.cjs", "privacy-worker.mjs", "privacy-child.mjs")]
    files += ["tests/release-lease-smoke.py"]
    files += ["tests/fixture_runtime.py", "tests/fixture-guard.c"]
    files += ["tests/privacy-dataflow.py", "tests/privacy-caller-probe.mjs", "tests/privacy-dataflow-profile.json"]
    return files


def snapshot(release, installation):
    files = source_files()
    for name in files:
        destination = release / name
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(APP / name, destination)
    atomic_json(release / "installation.json", installation)


def accept(release):
    """Exercise the real candidate before any live release pointer changes."""
    runtime = release / "runtime"
    record = read_json(runtime / "runtime.json")
    data = Path(read_json(release / "installation.json")["data_dir"])
    privacy.require_support(release, privacy.read_mode(data), record)
    preparer().check_runtime(runtime / "claude", runtime, record["version"])
    native_execution = record.get("execution") == "native-v1"
    native = native_execution or (runtime / "libtermux-musl-dns.so").is_file()
    proxy_options = [] if native else ["--with-proxy"]
    checks = [("tools", "device-smoke.py", proxy_options),
              ("self_spawn_tools", "device-smoke.py", [*proxy_options, "--process-wrapper"]),
              ("background_tools", "device-smoke.py", [*proxy_options, "--background"]),
              ("process_wrapper", "process-wrapper-smoke.py", [])]
    if native:
        checks.insert(0, ("network", "network-smoke.py", []))
        checks.insert(0, ("dns", "dns-smoke.py", []))
    if native_execution:
        checks.insert(0, ("native_execution", "native-exec-smoke.py", []))
    if record.get("release_lease") == 1:
        checks.insert(0, ("release_lease", "release-lease-smoke.py", []))
    if privacy.supported(release, record):
        checks += [("privacy", "privacy-smoke.py", []),
                   ("privacy_project", "privacy-dataflow.py", ["--project-check"]),
                   ("privacy_collectors", "privacy-dataflow.py", ["--collector-check"]),
                   ("privacy_requests", "privacy-dataflow.py", ["--request-check"]),
                   ("privacy_raw_body_control", "device-smoke.py", ["--raw-body-test"]),
                   ("privacy_tools", "device-smoke.py", ["--privacy", "optional-off"]),
                   ("privacy_background", "device-smoke.py", ["--privacy", "optional-off", "--background"])]
    reports = {}
    fixture = fixture_runner()
    for name, script, options in checks:
        report = release / (name + ".json")
        command = [sys.executable, str(release / "tests" / script), "--root", str(runtime), "--report", str(report), *options]
        # Also wrap historical test scripts when validating a rollback target.
        result = fixture.run(command, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError("Candidate rejected by " + name + ":\n" + result.stdout[-6000:] + result.stderr[-2000:])
        reports[name] = read_json(report)
        reports[name]["fixture"] = result.fixture
        if not result.fixture.get("ready") or not result.fixture.get("all_descendants_reaped"):
            raise RuntimeError("Candidate test lacks a completed fixture boundary: " + name)
        atomic_json(report, reports[name])
        print("Passed:", name, flush=True)
    atomic_json(release / "acceptance.json", {"version": record["version"], "tested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "real_model": False, "checks": reports})


def seal(release):
    prepare = preparer()
    files = {}
    for path in sorted(release.rglob("*")):
        relative = path.relative_to(release)
        if not path.is_file() or "__pycache__" in relative.parts:
            continue
        if relative.parts[0] == "runtime" and str(relative) not in (
            "runtime/claude", "runtime/claude-dispatch", "runtime/termux-shell", "runtime/termux-http-proxy", "runtime/libtermux-musl-dns.so", "runtime/libtermux-musl-compat.so", "runtime/libtermux-bionic-exec.so", "runtime/lib/ld-musl-aarch64.so.1", "runtime/runtime.json"):
            continue
        if relative.name not in ("files.json", "acceptance.json", "tools.json", "self_spawn_tools.json", "background_tools.json", "process_wrapper.json", "dns.json", "network.json", "native_execution.json", "release_lease.json", "privacy.json", "privacy_project.json", "privacy_collectors.json", "privacy_requests.json", "privacy_raw_body_control.json", "privacy_tools.json", "privacy_background.json"):
            files[str(relative)] = prepare.digest(path).hex()
    atomic_json(release / "files.json", files)


def promote(data, release, validator=accept):
    validator(release)
    old = release_at(data, "current", required=False)
    prior = (data / "previous").readlink() if (data / "previous").is_symlink() else None
    rotated = old and old != release
    try:
        if rotated:
            atomic_link(data / "previous", old.relative_to(data))
        atomic_link(data / "current", release.relative_to(data))
    except Exception:
        if rotated:
            if prior is None:
                (data / "previous").unlink(missing_ok=True)
            else:
                atomic_link(data / "previous", prior)
        raise


def promote_with_settings(data, release, config, settings):
    validate_release(release)
    path = config / "settings.json"
    previous = read_json(path) if path.exists() else None
    atomic_json(path, settings)
    try:
        promote(data, release, validator=lambda _: None)
    except Exception:
        # Restore our own write, but never replace an intervening user edit.
        if path.exists() and read_json(path) == settings:
            if previous is None:
                path.unlink()
            else:
                atomic_json(path, previous)
        raise


def install(data, config, bin_dir, version):
    check_requirements()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise RuntimeError("Specify an exact numeric Claude Code version")
    if privacy.read_mode(data) == "optional-off" and version not in privacy.profiles(APP):
        raise RuntimeError("Requested Claude version has no verified optional-off profile; no download or build started")
    check_links(data, bin_dir)
    check_settings(config, data)
    current = release_at(data, "current", required=False)
    if current:
        stored = read_json(current / "installation.json")
        if stored["config_dir"] != str(config) or stored["bin_dir"] != str(bin_dir):
            raise RuntimeError("Keep this installation's existing config and bin directories when updating")
    if (data / "releases").is_dir():
        from termux_storage import collect
        atomic_json(data / "gc-status.json", collect(data, apply=True))
    if shutil.disk_usage(data).free < 512 * 1024 * 1024:
        raise RuntimeError("At least 512 MiB of free space is required to stage an update; the current version was kept")
    release = data / "releases" / (version + "-" + uuid.uuid4().hex[:10])
    release.mkdir(parents=True, mode=0o700)
    installed = False
    try:
        installation = {"schema": 1, "data_dir": str(data), "config_dir": str(config), "bin_dir": str(bin_dir)}
        snapshot(release, installation)
        privacy.select_profile(release, version)
        runtime = release / "runtime"
        runtime.mkdir(mode=0o700)
        preparer().prepare(runtime, version, shared=data / "cache")
        accept(release)
        seal(release)
        # Re-read after testing to preserve edits made while downloading.
        settings = check_settings(config, data)
        settings = execution_settings(settings, data, native=True)
        config.mkdir(parents=True, exist_ok=True, mode=0o700)
        bin_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        promote_with_settings(data, release, config, settings)
        installed = True
        for path, target in command_links(data, bin_dir).items():
            atomic_link(path, target)
        from termux_storage import collect
        atomic_json(data / "gc-status.json", collect(data, apply=True))
        print("Installed Claude Code", version, "at", release)
        print("Configuration:", config)
        print("Command:", bin_dir / "claude")
        if bin_dir not in [Path(p).absolute() for p in os.get_exec_path() if p]:
            print("This bin directory is not in PATH. Use the absolute command path or add the directory to your shell PATH.")
    finally:
        if not installed:
            shutil.rmtree(release)


def rollback(data):
    current = release_at(data, "current")
    previous = release_at(data, "previous")
    validate_release(previous)
    # Recheck tools: a changed Termux environment may also affect older builds.
    accept(previous)
    manifest = read_json(previous / "installation.json")
    config = Path(manifest["config_dir"])
    native = read_json(previous / "runtime/runtime.json").get("execution") == "native-v1"
    settings = check_settings(config, data, native=native)
    promote_with_settings(data, previous, config, execution_settings(settings, data, native))
    print("Rolled back to", read_json(previous / "runtime/runtime.json")["version"])


def doctor(data):
    import termux_update as updates
    release = release_at(data, "current")
    record = validate_release(release)
    installation = read_json(release / "installation.json")
    config = Path(installation["config_dir"])
    bin_dir = Path(installation["bin_dir"])
    native = record.get("execution") == "native-v1"
    settings = check_settings(config, data, native=native)
    if not native and settings.get("env", {}).get("CLAUDE_CODE_PROCESS_WRAPPER") != wrapper_value(data):
        raise RuntimeError("The persistent process-wrapper setting is missing")
    for path, target in command_links(data, bin_dir).items():
        if not path.is_symlink() or path.readlink() != target:
            raise RuntimeError("Command link needs repair: " + str(path))
    preparer().check_runtime(release / "runtime/claude", release / "runtime", record["version"])
    report = {"version": record["version"], "release": str(release), "config_dir": str(config),
              "default_dns": "native Android resolver" if native or (release / "runtime/libtermux-musl-dns.so").is_file() else "HTTP CONNECT helper",
              "execution": "native ELF dependency" if native else "legacy wrappers",
              "file_integrity": "passed", "startup": "passed", "command_links": "passed", "process_wrapper_setting": "not required" if native else "passed",
              "previous": str(release_at(data, "previous", required=False) or "none"),
              "real_account_tested": False, "updates": updates.status(data), "privacy": privacy.status(data, release)}
    print(json.dumps(report, indent=2))


def main():
    import termux_update as updates
    values = defaults()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("install", "update", "rollback", "doctor", "test", "updates", "auto-update", "_auto-update", "privacy", "gc"))
    parser.add_argument("mode", nargs="?", choices=("on", "off", "standard", "optional-off", "status"))
    parser.add_argument("--data-dir", type=Path, default=values["data_dir"])
    parser.add_argument("--config-dir", type=Path, default=values["config_dir"])
    parser.add_argument("--bin-dir", type=Path, default=values["bin_dir"])
    parser.add_argument("--version")
    parser.add_argument("--channel", choices=("latest", "stable"))
    parser.add_argument("--check", action="store_true", help="Check available updates without installing")
    parser.add_argument("--apply", action="store_true", help="Remove eligible inactive releases (gc only)")
    parser.add_argument("--with-proxy", action="store_true", help="Also build and test the diagnostic proxy (test only)")
    parser.add_argument("--interval", type=int, help="Seconds between automatic checks (60..86400)")
    parser.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.apply and args.command != "gc":
        parser.error("--apply is only valid for gc")
    if args.with_proxy and args.command != "test":
        parser.error("--with-proxy is only valid for test")
    data, config, bin_dir = [p.expanduser().resolve() for p in (args.data_dir, args.config_dir, args.bin_dir)]
    if args.command == "auto-update" and args.mode not in ("on", "off"):
        parser.error("auto-update requires on or off")
    if args.command == "privacy" and args.mode not in (None, "status", "standard", "optional-off"):
        parser.error("privacy accepts status, standard or optional-off")
    if args.mode and args.command not in ("auto-update", "privacy"):
        parser.error("mode is only valid for auto-update or privacy")
    if args.version and args.channel:
        parser.error("choose --version or --channel")
    if args.interval is not None and (args.command != "auto-update" or not 60 <= args.interval <= 86400):
        parser.error("--interval is only valid for auto-update and must be 60..86400")
    if args.check and args.command != "update":
        parser.error("--check is only valid for update")
    if (args.version or args.channel) and args.command not in ("install", "update"):
        parser.error("--version/--channel is only valid for install or update")
    if args.command in ("install", "update"):
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not data.is_dir():
        parser.error("No managed installation exists at " + str(data))
    if args.command == "updates":
        print(json.dumps(updates.status(data), indent=2))
        return
    if args.lock_fd is not None:
        if not os.path.samestat(os.fstat(args.lock_fd), (data / "install.lock").stat()):
            parser.error("Inherited lock does not belong to this installation")
        lock = os.fdopen(os.dup(args.lock_fd), "a")
    else:
        lock = (data / "install.lock").open("a")
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if args.command == "_auto-update" else 0))
        except BlockingIOError:
            return
        # A waiting command may have started from a release replaced by another
        # updater. Re-enter the current manager while retaining the same lock.
        current = release_at(data, "current", required=False)
        if args.command != "install" and current and current != APP:
            command = [sys.executable, str(current / "bin/claude-termux-manage"), *sys.argv[1:]]
            if args.lock_fd is None:
                command += ["--lock-fd", str(lock.fileno())]
            result = subprocess.run(command, pass_fds=(args.lock_fd if args.lock_fd is not None else lock.fileno(),))
            if result.returncode:
                raise RuntimeError("Current manager exited with code " + str(result.returncode))
            return
        if args.command == "install":
            install(data, config, bin_dir, updates.resolve_version(args.version or args.channel or "latest"))
            if not (data / "update-settings.json").exists():
                settings = updates.settings(data)
                settings["channel"] = args.channel or "latest"
                atomic_json(data / "update-settings.json", settings)
        elif args.command in ("update", "_auto-update"):
            updates.run(sys.modules[__name__], data, config, bin_dir, lock.fileno(),
                        requested=args.version, channel=args.channel, check=args.check,
                        background=args.command == "_auto-update")
        elif args.command == "rollback":
            rollback(data)
            settings = updates.settings(data)
            settings["paused"] = True
            atomic_json(data / "update-settings.json", settings)
            print("Automatic updates paused after rollback. Run claude update to resume.")
        elif args.command == "auto-update":
            settings = updates.settings(data)
            settings["automatic"] = args.mode == "on"
            if settings["automatic"]:
                settings["paused"] = False
            if args.interval is not None:
                settings["interval_seconds"] = args.interval
            atomic_json(data / "update-settings.json", settings)
            print(json.dumps(settings, indent=2))
        elif args.command == "doctor":
            doctor(data)
        elif args.command == "gc":
            from termux_storage import collect
            print(json.dumps(collect(data, args.apply), indent=2))
        elif args.command == "privacy":
            release = release_at(data, "current")
            validate_release(release)
            result = privacy.status(data, release) if args.mode in (None, "status") else privacy.set_mode(data, release, args.mode)
            print(json.dumps(result, indent=2))
        else:
            release = release_at(data, "current")
            validate_release(release)
            accept(release)
            if args.with_proxy:
                subprocess.run([sys.executable, str(release / "tests/proxy-smoke.py"), "--root", str(release / "runtime")], check=True)


def entry():
    try:
        main()
    except (OSError, ValueError, RuntimeError, UpdateTrustError, subprocess.SubprocessError, tarfile.TarError) as error:
        sys.exit("claude-termux-manage: " + str(error))
