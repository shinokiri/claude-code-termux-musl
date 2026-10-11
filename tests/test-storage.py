#!/usr/bin/env python3
"""Cleanup must preserve current/previous, live descendants and legacy installs."""
import fcntl
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
from termux_storage import collect


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

    def test_only_unreferenced_leased_releases_are_removed(self):
        for name in ("current", "previous", "unused"):
            p = self.release(name)
            if name != "unused":
                (self.data / name).symlink_to(p)
        legacy = self.release("legacy", False)
        outside = self.data / "outside"
        outside.mkdir()
        (self.releases / "link").symlink_to(outside)
        preview = collect(self.data)
        self.assertEqual([r["release"] for r in preview["eligible"]], ["unused"])
        self.assertTrue((self.releases / "unused").exists())
        result = collect(self.data, True)
        self.assertEqual(result["removed"], ["unused"])
        self.assertTrue(legacy.exists() and outside.exists())
        self.assertTrue((self.data / "current").is_dir() and (self.data / "previous").is_dir())

    def test_live_lock_prevents_removal_until_last_holder_exits(self):
        p = self.release("live")
        with (p / "runtime/release.lease").open() as lease:
            fcntl.flock(lease, fcntl.LOCK_SH)
            self.assertEqual(collect(self.data, True)["removed"], [])
            self.assertTrue(p.exists())
        self.assertEqual(collect(self.data, True)["removed"], ["live"])

    def test_unknown_or_missing_lease_is_retained(self):
        p = self.release("broken")
        (p / "runtime/release.lease").unlink()
        self.assertEqual(collect(self.data, True)["removed"], [])


if __name__ == "__main__":
    unittest.main()
