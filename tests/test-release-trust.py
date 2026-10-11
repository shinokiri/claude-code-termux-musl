#!/usr/bin/env python3
"""Test actual Ed25519 signatures, tampering and refusal to update unsigned."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "libexec"))
import termux_trust as trust
import termux_update as updates


class SignatureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "libexec").mkdir()
        self.verifier = self.root / "libexec/release-signature.cjs"
        shutil.copyfile(REPO / "libexec/release-signature.cjs", self.verifier)
        subprocess.run(["node", "-e", """
const fs=require('node:fs'),c=require('node:crypto'),p=process.argv[1];
const pair=c.generateKeyPairSync('ed25519');
fs.writeFileSync(p+'/key.pem',pair.privateKey.export({type:'pkcs8',format:'pem'}),{mode:0o600});
fs.writeFileSync(p+'/libexec/release-keys.json',JSON.stringify({keys:[{id:'fixture',public_key:pair.publicKey.export({type:'spki',format:'pem'})}]}));
""", str(self.root)], check=True)
        self.message = b'{"schema":1,"version":"1.4.0","sha256":"' + b"0" * 64 + b'"}\n'
        (self.root / "manifest").write_bytes(self.message)
        subprocess.run(["node", str(self.verifier), "sign", str(self.root / "key.pem"),
                        str(self.root / "manifest"), str(self.root / "signature")], check=True)
        self.signature = (self.root / "signature").read_bytes()
        self.addCleanup(patch.stopall)
        patch.object(trust, "APP", self.root).start()

    def test_real_signature_authenticates_exact_bytes(self):
        trust.verify_manifest(self.message, self.signature)
        for modified in (self.message + b" ", self.message.replace(b"1.4.0", b"1.4.1")):
            with self.assertRaises(trust.UpdateTrustError):
                trust.verify_manifest(modified, self.signature)

    def test_verifier_ignores_ambient_node_preload(self):
        with patch.dict(os.environ, {"NODE_OPTIONS": "--require /nonexistent-untrusted-preload.js"}):
            trust.verify_manifest(self.message, self.signature)

    def test_unknown_key_invalid_signature_and_unsigned_are_rejected(self):
        envelope = json.loads(self.signature)
        envelope["key_id"] = "attacker"
        for signature in (json.dumps(envelope).encode(), b"", b"{}", self.signature.replace(b"ed25519", b"rsa")):
            with self.assertRaises(trust.UpdateTrustError):
                trust.verify_manifest(self.message, signature)

    def test_discovery_requires_signature_before_accepting_manifest(self):
        with patch.object(updates, "fetch", side_effect=[self.message, self.signature]):
            self.assertEqual(updates.adapter_manifest()["version"], "1.4.0")
        with patch.object(updates, "fetch", side_effect=[self.message, OSError("missing signature")]):
            with self.assertRaises(trust.UpdateTrustError):
                updates.adapter_manifest()


if __name__ == "__main__":
    unittest.main()
