#!/usr/bin/env python3
"""Update policy, failure isolation, archive trust and background launch checks."""
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "libexec"))
import termux_manage as manage
import termux_update as updates
import termux_launch as launch


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        self.config, self.bin = self.data / "config", self.data / "bin"
        self.old = self.release("old", "2.0.0")
        manage.atomic_link(self.data / "current", self.old.relative_to(self.data))
        self.lock = (self.data / "install.lock").open("a")
        self.addCleanup(self.lock.close)
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        self.addCleanup(patch.stopall)
        self.output = io.StringIO()
        patch("sys.stdout", self.output).start()
        patch.object(updates, "adapter_manifest", return_value={"schema": 1, "version": "1.0.0", "sha256": "0" * 64}).start()
        patch.object(updates, "resolve_version", return_value="2.1.0").start()

    def release(self, name, version, adapter="1.0.0"):
        release = self.data / "releases" / name
        (release / "runtime").mkdir(parents=True)
        manage.atomic_json(release / "installation.json", {"data_dir": str(self.data), "config_dir": str(self.config), "bin_dir": str(self.bin)})
        manage.atomic_json(release / "runtime/runtime.json", {"version": version, "root": str(release / "runtime")})
        manage.atomic_json(release / "adapter.json", {"schema": 1, "version": adapter})
        (release / "runtime/claude").write_bytes(b"test-binary")
        manage.seal(release)
        return release

    def run_update(self, **kwargs):
        return updates.run(manage, self.data, self.config, self.bin, self.lock.fileno(), **kwargs)

    def successful_install(self, data, config, bin_dir, version):
        candidate = self.release("new", version)
        manage.promote(data, candidate, validator=manage.validate_release)

    def test_bare_update_uses_official_latest_and_preserves_previous(self):
        with patch.object(manage, "install", side_effect=self.successful_install):
            self.run_update()
        updates.resolve_version.assert_called_once_with("latest")
        self.assertEqual(manage.release_at(self.data, "previous"), self.old)
        self.assertEqual(updates.status(self.data)["installed_claude"], "2.1.0")
        self.assertEqual(updates.status(self.data)["last_attempt"]["result"], "updated")

    def test_noop_neither_reinstalls_nor_loses_rollback(self):
        updates.resolve_version.return_value = "2.0.0"
        with patch.object(manage, "install") as install:
            self.run_update()
        install.assert_not_called()
        self.assertEqual(manage.release_at(self.data, "current"), self.old)
        self.assertFalse((self.data / "previous").exists())

    def test_failed_candidate_keeps_release_and_throttles_retries(self):
        with patch.object(manage, "install", side_effect=RuntimeError("candidate tools failed")):
            with self.assertRaisesRegex(RuntimeError, "tools failed"):
                self.run_update()
        self.assertEqual(manage.release_at(self.data, "current"), self.old)
        report = updates.status(self.data)["last_attempt"]
        self.assertEqual(report["result"], "failed")
        self.assertFalse(updates.due(self.data, report["checked_at"] + 10))
        self.assertTrue(updates.due(self.data, report["checked_at"] + 3601))

    def test_network_failure_is_recorded_and_background_does_not_raise(self):
        updates.resolve_version.side_effect = OSError("offline")
        self.run_update(background=True)
        self.assertEqual(manage.release_at(self.data, "current"), self.old)
        self.assertEqual(updates.status(self.data)["last_attempt"]["error"], "offline")
        self.assertIn("offline", (self.data / "update.log").read_text())

    def test_github_outage_does_not_block_compatible_official_update(self):
        updates.adapter_manifest.side_effect = OSError("GitHub unavailable")
        with patch.object(manage, "install", side_effect=self.successful_install):
            self.run_update()
        self.assertEqual(updates.status(self.data)["installed_claude"], "2.1.0")
        self.assertEqual(updates.status(self.data)["last_attempt"]["result"], "partial")

    def test_check_only_changes_no_installation_settings_or_attempt_status(self):
        with patch.object(manage, "install") as install:
            self.run_update(check=True, channel="stable")
        install.assert_not_called()
        self.assertEqual(manage.release_at(self.data, "current"), self.old)
        self.assertFalse((self.data / "update-settings.json").exists())
        self.assertFalse((self.data / "update-status.json").exists())
        self.assertEqual(json.loads(self.output.getvalue())["result"], "available")

    def test_channel_never_downgrades_but_exact_version_can_pin(self):
        updates.resolve_version.return_value = "1.9.0"
        with patch.object(manage, "install") as install:
            self.run_update(channel="stable")
        install.assert_not_called()
        self.assertEqual(updates.settings(self.data)["channel"], "stable")
        with patch.object(manage, "install", side_effect=self.successful_install):
            self.run_update(requested="1.9.0")
        self.assertEqual(updates.status(self.data)["installed_claude"], "1.9.0")
        self.assertEqual(updates.settings(self.data)["pinned_version"], "1.9.0")

    def test_background_honors_pin_and_manual_update_clears_it(self):
        manage.atomic_json(self.data / "update-settings.json", dict(updates.DEFAULTS, pinned_version="2.0.0"))
        updates.resolve_version.return_value = "2.0.0"
        self.run_update(background=True)
        updates.resolve_version.assert_called_with("2.0.0")
        self.assertEqual(updates.settings(self.data)["pinned_version"], "2.0.0")
        self.run_update()
        updates.resolve_version.assert_called_with("latest")
        self.assertIsNone(updates.settings(self.data)["pinned_version"])

    def test_rollback_pause_blocks_background_until_explicit_update(self):
        manage.atomic_json(self.data / "update-settings.json", dict(updates.DEFAULTS, paused=True))
        self.run_update(background=True)
        updates.resolve_version.assert_not_called()
        updates.resolve_version.return_value = "2.0.0"
        self.run_update()
        self.assertFalse(updates.settings(self.data)["paused"])

    def test_disabled_automatic_updates_stay_disabled_after_manual_update(self):
        manage.atomic_json(self.data / "update-settings.json", dict(updates.DEFAULTS, automatic=False))
        self.run_update(background=True)
        updates.resolve_version.assert_not_called()
        updates.resolve_version.return_value = "2.0.0"
        self.run_update()
        self.assertFalse(updates.settings(self.data)["automatic"])

    def test_corrupt_adapter_download_cannot_change_current(self):
        updates.adapter_manifest.return_value["version"] = "1.1.0"
        with patch.object(updates, "fetch", return_value=b"corrupt archive"):
            with self.assertRaisesRegex(RuntimeError, "checksum"):
                self.run_update()
        self.assertEqual(manage.release_at(self.data, "current"), self.old)
        self.assertFalse(list(self.data.glob("adapter-*")))

    def test_adapter_installer_inherits_same_lock_and_staging_is_removed(self):
        updates.adapter_manifest.return_value["version"] = "1.1.0"
        marker = self.data / "child-checked-lock"
        def fixture(manifest, destination):
            (destination / "tools").mkdir()
            (destination / "tools/install-user.py").write_text(
                "import fcntl,sys\nfrom pathlib import Path\n"
                "fd=int(sys.argv[sys.argv.index('--lock-fd')+1])\n"
                "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
                "Path(" + repr(str(marker)) + ").write_text('shared lock')\n"
                "sys.path.insert(0," + repr(str(REPO / "libexec")) + ")\n"
                "import termux_manage as m\n"
                "data=Path(" + repr(str(self.data)) + ")\n"
                "r=data/'releases/new-adapter'; (r/'runtime').mkdir(parents=True)\n"
                "m.atomic_json(r/'installation.json', {'data_dir':str(data)})\n"
                "m.atomic_json(r/'runtime/runtime.json', {'version':'2.1.0','root':str(r/'runtime')})\n"
                "m.atomic_json(r/'adapter.json', {'schema':1,'version':'1.1.0'})\n"
                "m.seal(r); m.promote(data,r,validator=m.validate_release)\n")
        with patch.object(updates, "unpack_adapter", side_effect=fixture):
            self.run_update()
        self.assertEqual(marker.read_text(), "shared lock")
        self.assertFalse(list(self.data.glob("adapter-*")))
        self.assertEqual(updates.status(self.data)["installed_adapter"], "1.1.0")

    def test_installer_exit_zero_without_activation_is_not_success(self):
        with patch.object(manage, "install"):
            with self.assertRaisesRegex(RuntimeError, "did not activate"):
                self.run_update()
        self.assertEqual(updates.status(self.data)["last_attempt"]["result"], "failed")

    def test_clock_rollback_does_not_disable_checks_forever(self):
        manage.atomic_json(self.data / "update-status.json", {"checked_at": 10000})
        self.assertTrue(updates.due(self.data, now=1000))

    def test_second_background_attempt_skips_a_busy_installation_lock(self):
        with patch.object(sys, "argv", ["manager", "_auto-update", "--data-dir", str(self.data)]):
            started = time.monotonic()
            manage.main()
        self.assertLess(time.monotonic() - started, 1)
        updates.resolve_version.assert_not_called()

    def test_actual_rollback_command_pauses_automatic_updates(self):
        previous = self.release("previous", "1.9.0")
        manage.atomic_link(self.data / "previous", previous.relative_to(self.data))
        prepare = manage.preparer()
        with patch.object(manage, "APP", self.old), patch.object(manage, "preparer", return_value=prepare), patch.object(manage, "accept"), patch.object(sys, "argv", [
                "manager", "rollback", "--data-dir", str(self.data), "--lock-fd", str(self.lock.fileno())]):
            manage.main()
        self.assertEqual(manage.release_at(self.data, "current"), previous)
        self.assertTrue(updates.settings(self.data)["paused"])


class ArchiveAndDiscoveryTests(unittest.TestCase):
    def test_official_channel_uses_musl_package_and_checks_identity(self):
        with patch.object(updates, "fetch", return_value=json.dumps({"name": updates.PACKAGE, "version": "2.1.296"}).encode()) as fetch:
            self.assertEqual(updates.resolve_version("latest"), "2.1.296")
            self.assertTrue(fetch.call_args.args[0].endswith(updates.PACKAGE + "/latest"))
        with patch.object(updates, "fetch", return_value=b'{"name":"unrelated","version":"2.1.296"}'):
            with self.assertRaisesRegex(RuntimeError, "identity"):
                updates.resolve_version("latest")

    def test_invalid_selectors_do_not_make_network_requests(self):
        with patch.object(updates, "fetch") as fetch:
            for selector in ("../../bad", "next", "2.0.0;sh", "", None):
                with self.assertRaises(ValueError):
                    updates.resolve_version(selector)
            fetch.assert_not_called()

    def archive(self, entries):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as stream:
            for name, content, kind in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.size = len(content)
                stream.addfile(member, io.BytesIO(content))
        return output.getvalue()

    def test_archive_rejects_traversal_absolute_links_and_duplicate_paths(self):
        for name, kind in (("../escape", tarfile.REGTYPE), ("/escape", tarfile.REGTYPE), ("tools/link", tarfile.SYMTYPE), ("tools/hardlink", tarfile.LNKTYPE), ("tools//file", tarfile.REGTYPE)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                archive = self.archive([(name, b"", kind)])
                manifest = {"version": "1.0.0", "sha256": hashlib.sha256(archive).hexdigest()}
                with patch.object(updates, "fetch", return_value=archive):
                    with self.assertRaisesRegex(RuntimeError, "Unsafe"):
                        updates.unpack_adapter(manifest, Path(directory))
        with tempfile.TemporaryDirectory() as directory:
            archive = self.archive([("tools/file", b"", tarfile.REGTYPE)] * 2)
            with patch.object(updates, "fetch", return_value=archive):
                with self.assertRaisesRegex(RuntimeError, "Unsafe"):
                    updates.unpack_adapter({"version": "1.0.0", "sha256": hashlib.sha256(archive).hexdigest()}, Path(directory))

    def test_release_package_is_reproducible_and_extracts_without_a_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            for name in ("one", "two"):
                subprocess.run([sys.executable, str(REPO / "tools/package-adapter.py"), "--output", str(directory / name)], check=True, capture_output=True)
            one = (directory / "one/claude-termux-adapter.tar.gz").read_bytes()
            self.assertEqual(one, (directory / "two/claude-termux-adapter.tar.gz").read_bytes())
            manifest = json.loads((directory / "one/update.json").read_text())
            source = directory / "extracted"
            source.mkdir()
            with patch.object(updates, "fetch", return_value=one):
                updates.unpack_adapter(manifest, source)
            self.assertTrue((source / "bin/claude-termux").is_file())
            self.assertFalse((source / ".git").exists())
            self.assertFalse((source / "runtime").exists())


class LaunchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        self.installation = {"data_dir": str(self.data)}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {}, clear=True).start()
        patch("sys.stdin.isatty", return_value=True).start()
        patch("sys.stdout.isatty", return_value=True).start()
        patch.object(updates, "process_identity", return_value="123").start()
        self.spawn = patch.object(updates.subprocess, "Popen").start()
        self.fetch = patch.object(updates, "fetch", side_effect=AssertionError("network on launch path")).start()

    def test_interactive_launch_detaches_without_network_or_credentials(self):
        os.environ["ANTHROPIC_API_KEY"] = "fixture-not-a-real-key"
        updates.start_monitor(self.installation, [])
        self.fetch.assert_not_called()
        self.spawn.assert_called_once()
        self.assertTrue(self.spawn.call_args.kwargs["start_new_session"])
        self.assertNotIn("ANTHROPIC_API_KEY", self.spawn.call_args.kwargs["env"])

    def test_script_help_version_and_nested_launches_do_not_spawn(self):
        for args in (["--version"], ["--help"], ["-p", "prompt"], ["auth", "status"], ["--bg", "prompt"]):
            updates.start_monitor(self.installation, args)
        for name, value in (("CLAUDE_TERMUX_AUTO_UPDATE", "0"), ("DISABLE_AUTOUPDATER", "1"), ("DISABLE_UPDATES", "1"), ("CLAUDE_TERMUX_SESSION", "1")):
            with patch.dict(os.environ, {name: value}):
                updates.start_monitor(self.installation, [])
        self.spawn.assert_not_called()

    def test_watcher_stops_on_parent_exit_without_checking_updates(self):
        updates.process_identity.return_value = None
        with patch.object(updates, "due") as due:
            updates.watch(self.data, 123, "123")
        due.assert_not_called()

    def test_claude_update_routes_before_runtime_or_proxy_start(self):
        (self.data / "installation.json").write_text(json.dumps(self.installation))
        with patch.object(launch, "APP", self.data), patch.object(sys, "argv", ["claude", "update", "--check"]), patch.object(os, "execv", side_effect=SystemExit(0)) as execute:
            with self.assertRaises(SystemExit):
                launch.main()
        self.assertEqual(execute.call_args.args[1][2:], ["update", "--check"])
        self.spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
