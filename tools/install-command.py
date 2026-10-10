#!/usr/bin/env python3
"""Compatibility entry point: install a standalone release and its commands."""
from pathlib import Path
import os
import sys

script = Path(__file__).resolve().with_name("install-user.py")
os.execv(sys.executable, [sys.executable, str(script), *sys.argv[1:]])
