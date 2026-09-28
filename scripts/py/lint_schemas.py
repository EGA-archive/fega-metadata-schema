#!/usr/bin/env python3
"""Read-only authoring checks for EGA JSON Schemas."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Sequence

# Match the release CLIs: prefer this checkout over an older installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fega_tools.cli_utils import add_verbosity_argument, help_with_example
from fega_tools.jsonld_utils import find_repo_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Lint EGA schema authoring conventions using local references only."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help=help_with_example(
            "Schema files, schema directories, or repository directory (default: schemas/)",
            "python scripts/py/lint_schemas.py schemas/common schemas/graph/schema.json -v",
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=find_repo_root(Path.cwd()),
        help=help_with_example(
            "Repository root used for local reference lookup", "--root ."
        ),
    )
    parser.add_argument(
        "--resolution-root",
        type=Path,
        default=Path("schemas"),
        metavar="PATH",
        help=help_with_example(
            "Resolve references only inside this directory, relative to --root "
            "unless absolute (default: schemas); use . to include standards",
            "--resolution-root schemas",
        ),
    )
    add_verbosity_argument(
        parser,
        help_text="Print every violation, including its file, JSON Pointer and rule.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # argparse handles --help before importing optional validation dependencies.
    try:
        from fega_tools.schema_lint import RULES, discover_schemas, lint_schemas
    except ModuleNotFoundError as exc:
        print(
            f"Cannot start schema lint: missing Python module {exc.name!r}. "
            "Activate the project virtual environment and run "
            "python -m pip install -r requirements.txt -e .",
            file=sys.stderr,
        )
        return 2
    try:
        files = discover_schemas(args.paths or [args.root / "schemas"])
        if not files:
            print(
                "No JSON Schemas selected. Use schema.json / *.schema.json directories or explicit JSON files."
            )
            return 2
        skipped_references: set[str] = set()
        issues = lint_schemas(
            files,
            root=args.root,
            resolution_root=args.resolution_root,
            skipped_references=skipped_references,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Cannot lint schemas: {exc}")
        return 2
    if args.verbosity:
        print(
            "Checks: JSON parsing, schema structure, references, "
            + ", ".join(rule.__name__ for rule in RULES)
        )
        for issue in issues:
            print(issue)
        for ref in sorted(skipped_references):
            print(f"[skipped-reference] {ref}")
    if skipped_references:
        print(
            f"Skipped {len(skipped_references)} out-of-scope reference(s); "
            "dependent example checks may be incomplete. "
            "Use --resolution-root . to include repository dependencies."
        )
    print(f"Linted {len(files)} schema(s): {len(issues)} violation(s).")
    if issues:
        print(
            "; ".join(
                f"{rule}: {count}"
                for rule, count in sorted(
                    Counter(issue.rule for issue in issues).items()
                )
            )
        )
        if not args.verbosity:
            print("Use -v to display every violation.")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
