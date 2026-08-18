#!/usr/bin/env python3
"""Run the small, testable phases used by release workflows.

Example: ``python scripts/py/release_workflow.py -h``.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fega_tools.release_workflow import main


if __name__ == "__main__":
    raise SystemExit(main())
