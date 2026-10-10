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
                manage.check_settings(config, self.data)

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
        reports = ("acceptance.json", "tools.json", "self_spawn_tools.json", "background_tools.json", "process_wrapper.json")
        for name in reports:
            manage.atomic_json(release / name, {"run": 1})
        manage.seal(release)
        # `test` and `rollback` rewrite reports with new paths and timings.
        for name in reports:
            manage.atomic_json(release / name, {"run": 2})
        self.assertEqual(manage.validate_release(release)["version"], "1.0.0")


if __name__ == "__main__":
    unittest.main()
