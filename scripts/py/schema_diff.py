#!/usr/bin/env python3
"""Compare JSON Schema files or directory trees conservatively."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from fega_tools.io import collect_candidate_json, load_json_object
from fega_tools.schema_diff import Severity, compare_schemas
from fega_tools.cli_utils import help_with_example


def find_schema_files(path: Path) -> dict[str, Path]:
    if path.is_file():
        return {path.name: path}
    if path.is_dir():
        root = path.resolve()
        return {
            candidate.relative_to(root).as_posix(): candidate
            for candidate in collect_candidate_json([root])
            if candidate.name == "schema.json" or candidate.name.endswith(".schema.json")
        }
    return {}


def compare_sets(old_path: Path, new_path: Path) -> dict[str, Any]:
    old_files = find_schema_files(old_path)
    new_files = find_schema_files(new_path)
    if not old_files and not new_files:
        raise ValueError("No schema files found in either input")

    results: list[dict[str, Any]] = []
    severities: list[Severity] = []
    for relative in sorted(set(old_files) | set(new_files)):
        if relative not in old_files:
            severity = Severity.MINOR
            detail = {"severity": severity.label(), "changes": [{
                "path": "/", "severity": severity.label(), "message": "schema file added"
            }]}
        elif relative not in new_files:
            severity = Severity.MAJOR
            detail = {"severity": severity.label(), "changes": [{
                "path": "/", "severity": severity.label(), "message": "schema file removed"
            }]}
        else:
            old = load_json_object(old_files[relative])
            new = load_json_object(new_files[relative])
            diff = compare_schemas(old, new)
            severity = diff.severity
            detail = diff.as_dict()
        severities.append(severity)
        results.append({"file": relative, **detail})

    overall = max(severities, default=Severity.SAME)
    return {
        "overall_status": overall.label(),
        "inputs": {"old": str(old_path), "new": str(new_path)},
        "results": results,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare JSON Schema files or trees. Example: schema_diff old new")
    parser.add_argument("old", type=Path, help=help_with_example("Old schema file or directory", "schemas-old"))
    parser.add_argument("new", type=Path, help=help_with_example("New schema file or directory", "schemas-new"))
    parser.add_argument("-o", "--output", type=Path, metavar="PATH", help=help_with_example("Write JSON report", "--output report.json"))
    parser.add_argument(
        "--fail-on",
        choices=("never", "unknown", "major", "change"),
        default="unknown",
        help=help_with_example("Exit non-zero at or above this change threshold", "--fail-on major"),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help=help_with_example("Print the full report", "-v"))
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    try:
        report = compare_sets(args.old, args.new)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    if args.verbose or not args.output:
        print(rendered, end="")

    severity = Severity.parse(report["overall_status"])
    should_fail = {
        "never": False,
        "unknown": severity == Severity.UNKNOWN,
        "major": severity in {Severity.MAJOR, Severity.UNKNOWN},
        "change": severity != Severity.SAME,
    }[args.fail_on]
    raise SystemExit(1 if should_fail else 0)


if __name__ == "__main__":
    main()
