#!/usr/bin/env python3
"""Install a tested standalone release; the checkout can then be removed."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libexec"))
from termux_manage import entry

sys.argv.insert(1, "install")
entry()
