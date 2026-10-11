#!/usr/bin/env python3
"""Policy persistence and rejection must preserve working releases and settings."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "libexec"))
import termux_manage as manage
import termux_privacy as privacy


class PrivacyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name)
        self.profile = json.loads((REPO / "libexec/privacy-profile.json").read_text())

    def candidate(self, name="new", supported=True):
        root = self.data / "releases" / name
        (root / "runtime").mkdir(parents=True)
        (root / "libexec").mkdir()
        (root / "installation.json").write_text(json.dumps({"data_dir": str(self.data)}))
        (root / "libexec/privacy-profile.json").write_text(json.dumps(self.profile))
        (root / "runtime/runtime.json").write_text(json.dumps({
            "version": self.profile["claude"] if supported else "99.0.0",
            "privacy": "optional-v1", "original_sha256": self.profile["original_sha256"],
        }))
        return root

    def test_absent_mode_preserves_existing_preference(self):
        self.assertEqual(privacy.read_mode(self.data), "standard")

    def test_unknown_mode_and_symlink_are_rejected(self):
        path = self.data / "privacy-mode"
        path.write_text("optional-of\n")
        with self.assertRaises(RuntimeError):
            privacy.read_mode(self.data)
        path.unlink()
        path.symlink_to(self.data / "missing")
        with self.assertRaises(RuntimeError):
            privacy.read_mode(self.data)

    def test_new_binary_is_not_inferred_safe_from_version(self):
        root = self.candidate()
        record = json.loads((root / "runtime/runtime.json").read_text())
        record["original_sha256"] = "0" * 64
        self.assertFalse(privacy.supported(root, record))
        with self.assertRaisesRegex(RuntimeError, "no verified"):
            privacy.require_support(root, "optional-off", record)

    def test_failed_upgrade_keeps_current_previous_and_policy(self):
        old, current, candidate = self.candidate("old"), self.candidate("current"), self.candidate(supported=False)
        manage.atomic_link(self.data / "current", current.relative_to(self.data))
        manage.atomic_link(self.data / "previous", old.relative_to(self.data))
        (self.data / "privacy-mode").write_text("optional-off\n")
        with self.assertRaisesRegex(RuntimeError, "no verified"):
            manage.promote(self.data, candidate)
        self.assertEqual((self.data / "current").resolve(), current)
        self.assertEqual((self.data / "previous").resolve(), old)
        self.assertEqual(privacy.read_mode(self.data), "optional-off")

    def test_legacy_rollback_is_rejected_in_protected_mode(self):
        legacy = self.candidate()
        (legacy / "libexec/privacy-profile.json").unlink()
        with self.assertRaises(RuntimeError):
            privacy.require_support(legacy, "optional-off")
        privacy.require_support(legacy, "standard")

    def test_failed_selftest_does_not_enable_policy(self):
        root = self.candidate()
        with patch.object(privacy.subprocess, "run") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = "synthetic failure"
            run.return_value.stderr = ""
            with self.assertRaisesRegex(RuntimeError, "self-test failed"):
                privacy.set_mode(self.data, root, "optional-off")
        self.assertEqual(privacy.read_mode(self.data), "standard")

    def test_policy_selection_is_atomic_and_does_not_change_claude_settings(self):
        root = self.candidate()
        settings = self.data / "settings.json"
        settings.write_text('{"env":{"DISABLE_ERROR_REPORTING":"1"},"model":"sonnet"}')
        before = settings.read_bytes()
        with patch.object(privacy.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(privacy.set_mode(self.data, root, "optional-off")["mode"], "optional-off")
        self.assertEqual(settings.read_bytes(), before)
        self.assertEqual((self.data / "privacy-mode").stat().st_mode & 0o777, 0o600)
        self.assertEqual(privacy.set_mode(self.data, root, "standard")["mode"], "standard")
        self.assertEqual(settings.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
