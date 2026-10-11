"""Authenticate release manifests against locally pinned public keys."""
from pathlib import Path
import os
import subprocess
import tempfile

APP = Path(__file__).resolve().parents[1]


class UpdateTrustError(Exception):
    """An authentication failure must never become an unsigned update fallback."""


def verify_manifest(message, signature):
    with tempfile.TemporaryDirectory(prefix="claude-release-verify-") as directory:
        root = Path(directory)
        (root / "manifest").write_bytes(message)
        (root / "signature").write_bytes(signature)
        result = subprocess.run(["node", str(APP / "libexec/release-signature.cjs"), "verify", "-",
                                 str(root / "manifest"), str(root / "signature")], capture_output=True, text=True, timeout=15,
                                env={key: value for key, value in os.environ.items() if key not in ("NODE_OPTIONS", "NODE_PATH")})
        if result.returncode:
            raise UpdateTrustError("Adapter release authentication failed; current installation retained")
