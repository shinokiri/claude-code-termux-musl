#!/usr/bin/env python3
"""Check release switching, unrelated commands and existing user settings."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
import termux_manage as manage


class ManagedInstallTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        (self.data / "releases").mkdir()

    def release(self, name):
        release = self.data / "releases" / name
        release.mkdir()
        manage.atomic_json(release / "installation.json", {"data_dir": str(self.data)})
        return release

    def test_rejected_tools_keep_current_and_previous(self):
        old, current, candidate = [self.release(name) for name in ("old", "current", "candidate")]
        manage.atomic_link(self.data / "current", current.relative_to(self.data))
        manage.atomic_link(self.data / "previous", old.relative_to(self.data))
        def reject(_):
            raise RuntimeError("Bash tool failed")
        with self.assertRaisesRegex(RuntimeError, "Bash"):
            manage.promote(self.data, candidate, validator=reject)
        self.assertEqual(manage.release_at(self.data, "current"), current)
        self.assertEqual(manage.release_at(self.data, "previous"), old)

    def test_promotion_switches_whole_release_and_retains_previous(self):
        first, second = self.release("first"), self.release("second")
        for release in (first, second):
            (release / "adapter.py").write_text(release.name)
            (release / "claude").write_text(release.name)
        manage.promote(self.data, first, validator=lambda _: None)
        manage.promote(self.data, second, validator=lambda _: None)
        for name in ("adapter.py", "claude"):
            self.assertEqual((self.data / "current" / name).read_text(), "second")
            self.assertEqual((self.data / "previous" / name).read_text(), "first")

    def test_outside_release_link_is_rejected(self):
        (self.data / "current").symlink_to(self.data.parent)
        with self.assertRaisesRegex(RuntimeError, "outside"):
            manage.release_at(self.data, "current")

    def test_unrelated_command_is_not_replaced(self):
        commands = self.data / "bin"
        commands.mkdir()
        (commands / "claude").write_text("existing user executable")
        with self.assertRaisesRegex(RuntimeError, "another installation"):
            manage.check_links(self.data, commands)
        self.assertEqual((commands / "claude").read_text(), "existing user executable")

    def test_existing_settings_preserved_and_wrapper_conflict_rejected(self):
        config = self.data / "config"
        config.mkdir()
        original = {"model": "sonnet", "env": {"MY_SETTING": "keep"}, "permissions": {"allow": ["Read"]}}
        manage.atomic_json(config / "settings.json", original)
        with patch.dict("os.environ", {}, clear=True):
            settings = manage.check_settings(config, self.data)
            settings["env"]["CLAUDE_CODE_PROCESS_WRAPPER"] = manage.wrapper_value(self.data)
            manage.atomic_json(config / "settings.json", settings)
            checked = manage.check_settings(config, self.data)
            self.assertEqual(checked["permissions"], original["permissions"])
            self.assertEqual(checked["env"]["MY_SETTING"], "keep")
            settings["processWrapper"] = "/a/corporate/wrapper"
            manage.atomic_json(config / "settings.json", settings)
            with self.assertRaisesRegex(RuntimeError, "existing Claude process wrapper"):
                manage.check_settings(config, self.data, native=False)

    def test_native_migration_and_rollback_preserve_privacy_and_custom_settings(self):
        original = {"env": {"CLAUDE_CODE_PROCESS_WRAPPER": manage.wrapper_value(self.data),
                            "DISABLE_ERROR_REPORTING": "1", "DISABLE_FEEDBACK_COMMAND": "1",
                            "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1"}, "model": "sonnet"}
        native = manage.execution_settings(original, self.data, native=True)
        self.assertNotIn("CLAUDE_CODE_PROCESS_WRAPPER", native["env"])
        self.assertNotIn("DISABLE_TELEMETRY", native["env"])
        self.assertEqual(manage.execution_settings(native, self.data, native=False), original)
        custom = {"env": {"CLAUDE_CODE_PROCESS_WRAPPER": "/custom/wrapper"}, "processWrapper": "/another/wrapper"}
        self.assertEqual(manage.execution_settings(custom, self.data, native=True), custom)

    def test_failed_switch_restores_configuration_and_both_pointers(self):
        old, current, candidate = [self.release(name) for name in ("old", "current", "candidate")]
        manage.atomic_link(self.data / "current", current.relative_to(self.data))
        manage.atomic_link(self.data / "previous", old.relative_to(self.data))
        config = self.data / "config"
        original = {"env": {"CLAUDE_CODE_PROCESS_WRAPPER": manage.wrapper_value(self.data), "DISABLE_ERROR_REPORTING": "1"}}
        manage.atomic_json(config / "settings.json", original)
        real_link = manage.atomic_link
        def fail_current(path, target):
            if path.name == "current":
                raise OSError("simulated switch failure")
            real_link(path, target)
        with patch.object(manage, "validate_release"), patch.object(manage, "atomic_link", side_effect=fail_current):
            with self.assertRaisesRegex(OSError, "switch failure"):
                manage.promote_with_settings(self.data, candidate, config, manage.execution_settings(original, self.data, True))
        self.assertEqual(manage.read_json(config / "settings.json"), original)
        self.assertEqual(manage.release_at(self.data, "current"), current)
        self.assertEqual(manage.release_at(self.data, "previous"), old)

    def test_legacy_manager_forward_switch_configuration_is_repaired(self):
        config = self.data / "config"
        original = {"env": {"CLAUDE_CODE_PROCESS_WRAPPER": manage.wrapper_value(self.data), "DISABLE_ERROR_REPORTING": "1"}}
        manage.atomic_json(config / "settings.json", original)
        manage.migrate_legacy_settings(config, self.data)
        self.assertEqual(manage.read_json(config / "settings.json"), {"env": {"DISABLE_ERROR_REPORTING": "1"}})

    def test_checksum_change_is_detected_before_rollback(self):
        release = self.release("candidate")
        runtime = release / "runtime"
        runtime.mkdir()
        (runtime / "claude").write_bytes(b"accepted binary")
        manage.atomic_json(runtime / "runtime.json", {"version": "1.0.0", "root": str(runtime)})
        manage.seal(release)
        (runtime / "claude").write_bytes(b"corrupted binary")
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            manage.validate_release(release)

    def test_refreshing_acceptance_reports_keeps_release_integrity(self):
        release = self.release("candidate")
        runtime = release / "runtime"
        runtime.mkdir()
        (runtime / "claude").write_bytes(b"accepted binary")
        manage.atomic_json(runtime / "runtime.json", {"version": "1.0.0", "root": str(runtime)})
        reports = ("acceptance.json", "tools.json", "self_spawn_tools.json", "background_tools.json", "process_wrapper.json", "dns.json", "network.json", "native_execution.json")
        for name in reports:
            manage.atomic_json(release / name, {"run": 1})
        manage.seal(release)
        # `test` and `rollback` rewrite reports with new paths and timings.
        for name in reports:
            manage.atomic_json(release / name, {"run": 2})
        self.assertEqual(manage.validate_release(release)["version"], "1.0.0")

    def test_native_library_checksums_are_checked(self):
        for index, name in enumerate(("libtermux-musl-dns.so", "libtermux-musl-compat.so", "libtermux-bionic-exec.so")):
            with self.subTest(library=name):
                release = self.release("native-" + str(index))
                runtime = release / "runtime"
                runtime.mkdir()
                (runtime / "claude").write_bytes(b"accepted binary")
                library = runtime / name
                library.write_bytes(b"accepted native library")
                manage.atomic_json(runtime / "runtime.json", {"version": "1.0.0", "root": str(runtime)})
                manage.seal(release)
                library.write_bytes(b"corrupted library")
                with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                    manage.validate_release(release)


if __name__ == "__main__":
    unittest.main()
