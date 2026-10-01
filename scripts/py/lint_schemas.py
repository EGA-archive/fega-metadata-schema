#!/usr/bin/env python3
"""Read-only authoring checks for EGA JSON Schemas."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

# Match the release CLIs: prefer this checkout over an older installed package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from fega_tools.cli_utils import add_verbosity_argument, help_with_example
from fega_tools.jsonld_utils import find_repo_root


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for schema linting."""
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
        "--check-api",
        action="store_true",
        help=help_with_example(
            "Validate examples involving custom keywords through a running Biovalidator endpoint",
            "--check-api",
        ),
    )
    parser.add_argument(
        "--validator-url",
        help=help_with_example(
            "Biovalidator /validate URL for --check-api (default: http://localhost:3020/validate)",
            "--check-api --validator-url http://localhost:3020/validate",
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
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help=help_with_example(
            "Colour output (default: auto for terminals; respects NO_COLOR). "
            "An explicit always/never overrides the environment",
            "--color never",
        ),
    )
    add_verbosity_argument(
        parser,
        help_text="Print every violation and warning counts with -v; also list warnings and skipped checks with -vv.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run schema linting and return a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.validator_url and not args.check_api:
        parser.error("--validator-url requires --check-api")
    # argparse handles --help before importing optional validation dependencies.
    try:
        from fega_tools.schema_lint import RULES, discover_schemas, lint_schemas
        from fega_tools.biovalidator import DEFAULT_VALIDATOR_URL
        from fega_tools.schema_lint.reporting import print_report
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
        skipped_examples: set[str] = set()
        warnings = []
        issues = lint_schemas(
            files,
            root=args.root,
            resolution_root=args.resolution_root,
            skipped_references=skipped_references,
            validator_url=(
                (args.validator_url or DEFAULT_VALIDATOR_URL)
                if args.check_api
                else None
            ),
            skipped_examples=skipped_examples,
            warnings=warnings,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Cannot lint schemas: {exc}")
        return 2
    print_report(
        issues,
        file_count=len(files),
        root=args.root,
        stream=sys.stdout,
        verbosity=args.verbosity,
        color=args.color,
        warnings=warnings,
        checks=["JSON parsing", "schema structure", "references"]
        + [rule.__name__.replace("_", " ") for rule in RULES]
        + ["unused definitions (warning)"],
        skipped_references=skipped_references,
        skipped_examples=skipped_examples,
        validator_url=(
            (args.validator_url or DEFAULT_VALIDATOR_URL) if args.check_api else None
        ),
    )
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
