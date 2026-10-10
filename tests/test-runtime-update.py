#!/usr/bin/env python3
"""Regression checks for rejecting bad runtimes and retaining a rollback target."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("prepare_runtime", REPO / "tools/prepare-runtime.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "tmp").mkdir()

    def fixture(self, name, version, fail_init=False):
        path = self.root / name
        # Use the host Python path directly, so the fixture also works without
        # Termux's preload (exactly as check_runtime launches real candidates).
        import sys
        path.write_text("#!" + sys.executable + "\nimport sys\n"
                        + "if sys.argv[1] == '--version': print(" + repr(version + " (Claude Code)") + ")\n"
                        + ("if sys.argv[1] == '--init-only': sys.exit(42)\n" if fail_init else ""))
        path.chmod(0o700)
        return path

    def record(self, path, version):
        return {"version": version, "patched_sha256": prepare.digest(path).hex()}

    def installed(self):
        old = self.fixture("claude", "1.0.0")
        record = self.record(old, "1.0.0")
        (self.root / "runtime.json").write_text(json.dumps(record))
        return old, record

    def test_version_success_but_init_failure_keeps_working_binary(self):
        old, old_record = self.installed()
        bad = self.fixture("claude.candidate", "2.0.0", fail_init=True)
        with self.assertRaisesRegex(RuntimeError, "--init-only"):
            prepare.activate(self.root, bad, self.record(bad, "2.0.0"))
        self.assertEqual(prepare.digest(old).hex(), old_record["patched_sha256"])
        self.assertEqual(json.loads((self.root / "runtime.json").read_text()), old_record)
        self.assertFalse((self.root / "claude.prev").exists())

    def test_activate_and_rollback_preserve_both_versions(self):
        _, old_record = self.installed()
        new = self.fixture("claude.candidate", "2.0.0")
        new_record = self.record(new, "2.0.0")
        prepare.activate(self.root, new, new_record)
        self.assertEqual(prepare.digest(self.root / "claude.prev").hex(), old_record["patched_sha256"])
        prepare.rollback(self.root)
        self.assertEqual(prepare.digest(self.root / "claude").hex(), old_record["patched_sha256"])
        self.assertEqual(prepare.digest(self.root / "claude.prev").hex(), new_record["patched_sha256"])

    def test_checksum_mismatch_does_not_publish_download(self):
        source = self.root / "source"
        source.write_bytes(b"wrong binary")
        destination = self.root / "download"
        with self.assertRaisesRegex(RuntimeError, "checksum"):
            prepare.download(source.as_uri(), destination, bytes(32))
        self.assertFalse(destination.exists())
        self.assertFalse((self.root / "download.part").exists())


if __name__ == "__main__":
    unittest.main()
