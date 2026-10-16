#!/usr/bin/env python3
"""Cleanup must keep live releases and reclaim idle previous versions."""
import fcntl
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
from termux_storage import collect, hold_release


class StorageTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        self.releases = self.data / "releases"
        self.releases.mkdir()

    def release(self, name, lease=True):
        p = self.releases / name
        (p / "runtime").mkdir(parents=True)
        (p / "installation.json").write_text(json.dumps({"data_dir": str(self.data)}))
        (p / "runtime/runtime.json").write_text(json.dumps({"root": str(p / "runtime"), "release_lease": int(lease)}))
        (p / "runtime/release.lease").touch()
        (p / "runtime/claude").write_bytes(b"synthetic")
        return p

    def test_idle_previous_is_removed_and_preview_preserves_all_links(self):
        for name in ("current", "previous", "unused"):
            p = self.release(name)
            if name != "unused":
                (self.data / name).symlink_to(p)
        legacy = self.release("legacy", False)
        outside = self.data / "outside"
        outside.mkdir()
        (self.releases / "link").symlink_to(outside)
        preview = collect(self.data)
        self.assertEqual([r["release"] for r in preview["eligible"]], ["previous", "unused"])
        self.assertTrue((self.releases / "unused").exists())
        self.assertTrue((self.data / "previous").is_symlink())
        result = collect(self.data, True)
        self.assertEqual(result["removed"], ["previous", "unused"])
        self.assertTrue(result["previous_cleared"])
        self.assertTrue(legacy.exists() and outside.exists())
        self.assertTrue((self.data / "current").is_dir())
        self.assertFalse((self.data / "previous").is_symlink())

    def test_live_lock_prevents_removal_until_last_holder_exits(self):
        p = self.release("live")
        (self.data / "previous").symlink_to(p)
        with (p / "runtime/release.lease").open() as lease:
            fcntl.flock(lease, fcntl.LOCK_SH)
            self.assertEqual(collect(self.data, True)["removed"], [])
            self.assertTrue(p.exists())
            self.assertTrue((self.data / "previous").is_symlink())
        self.assertEqual(collect(self.data, True)["removed"], ["live"])
        self.assertFalse((self.data / "previous").is_symlink())

    def test_python_manager_holds_lease_until_its_last_read_finishes(self):
        p = self.release("manager")
        with hold_release(p):
            self.assertEqual(collect(self.data, True)["removed"], [])
            self.assertEqual((p / "runtime/claude").read_bytes(), b"synthetic")
        self.assertEqual(collect(self.data, True)["removed"], ["manager"])

    def test_legacy_parent_is_protected_only_for_current_install_transaction(self):
        p = self.release("parent")
        (self.data / "previous").symlink_to(p)
        result = collect(self.data, True, protected=[p])
        self.assertEqual(result["removed"], [])
        self.assertTrue((self.data / "previous").is_symlink())
        self.assertEqual(collect(self.data, True)["removed"], ["parent"])

    def test_stale_internal_previous_is_cleared_only_when_applying(self):
        pointer = self.data / "previous"
        pointer.symlink_to(self.releases / "gone")
        collect(self.data)
        self.assertTrue(pointer.is_symlink())
        self.assertTrue(collect(self.data, True)["previous_cleared"])
        self.assertFalse(pointer.is_symlink())

    def test_external_previous_pointer_refuses_cleanup(self):
        p = self.release("unused")
        (self.data / "previous").symlink_to(self.data / "outside")
        with self.assertRaisesRegex(RuntimeError, "external previous"):
            collect(self.data, True)
        self.assertTrue(p.exists())

    def test_failure_to_remove_previous_restores_directory(self):
        p = self.release("previous")
        pointer = self.data / "previous"
        pointer.symlink_to(p)
        with patch.object(Path, "unlink", side_effect=PermissionError("fixture pointer failure")):
            result = collect(self.data, True)
        self.assertEqual(result["removed"], [])
        self.assertTrue(pointer.is_dir())
        self.assertEqual((p / "runtime/claude").read_bytes(), b"synthetic")
        self.assertFalse((self.data / "gc-previous").exists())

    def test_python_manager_refuses_a_release_locked_for_deletion(self):
        p = self.release("deleting")
        with (p / "runtime/release.lease").open() as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError), hold_release(p):
                self.fail("Manager entered during deletion")

    def test_unknown_or_missing_lease_is_retained(self):
        p = self.release("broken")
        (p / "runtime/release.lease").unlink()
        self.assertEqual(collect(self.data, True)["removed"], [])


if __name__ == "__main__":
    unittest.main()
