#!/usr/bin/env python3
"""Validate, collect, preview, or write pull-request release notes."""
from __future__ import annotations

import sys
from pathlib import Path

# Permit running this file directly from a source checkout without installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fega_tools.release_notes import _cli


if __name__ == "__main__":
    raise SystemExit(_cli())
