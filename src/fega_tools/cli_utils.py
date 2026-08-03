"""Shared command-line helpers for FEGA validation scripts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict

from fega_tools.validation_common import DEFAULT_ROOT, write_json_summary


def add_root_argument(
    parser: argparse.ArgumentParser,
    *,
    default: Path = DEFAULT_ROOT,
) -> None:
    """Add the standard schema-root argument to a parser."""
    parser.add_argument(
        "--root",
        type=Path,
        default=default,
        help=f"Entity schema root (default: {default})",
    )


def add_summary_arguments(
    parser: argparse.ArgumentParser,
    summary_filename: str,
) -> None:
    """Add standard summary directory and stdout options to a parser."""
    parser.add_argument(
        "--summary-dir",
        type=Path,
        help=f"Optional directory where {summary_filename} is written.",
    )
    parser.add_argument(
        "--print-summary",
        action="store_true",
        default=False,
        help="Print the full JSON summary to stdout (default: off).",
    )


def add_verbosity_argument(
    parser: argparse.ArgumentParser,
    *,
    help_text: str = "Increase log verbosity by adding more 'v's.",
) -> None:
    """Add the standard repeatable verbosity argument to a parser."""
    parser.add_argument(
        "--verbosity",
        "-v",
        action="count",
        default=0,
        help=help_text,
    )


def emit_summary(
    summary: Dict[str, Any],
    *,
    summary_dir: Path | None,
    summary_filename: str,
    print_summary: bool,
) -> None:
    """Write an optional artifact and/or print a JSON summary."""
    if summary_dir:
        write_json_summary(summary, summary_dir, summary_filename)
    if print_summary:
        json.dump(summary, sys.stdout, indent=2)
        sys.stdout.write("\n")
