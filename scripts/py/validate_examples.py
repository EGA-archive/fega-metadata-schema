#!/usr/bin/env python3
"""Validate FEGA example suites against a running Biovalidator endpoint."""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Sequence

try:
    from fega_tools.biovalidator import (
        DEFAULT_VALIDATOR_URL,
        assert_validator_reachable,
        validate_document,
    )
    from fega_tools.cli_utils import (
        add_root_argument,
        add_summary_arguments,
        add_verbosity_argument,
        emit_summary,
    )
    from fega_tools.logging_utils import configure_logging, log_suite_status
    from fega_tools.validation_common import (
        CATEGORIES,
        DEFAULT_ROOT,
        SCRIPT_ERROR_STATUS,
        aggregate_category_summaries,
        coverage_gaps_for_entity_category,
        expected_status_for,
        find_entity_dirs,
        find_example_files,
        find_example_coverage_gaps,
        format_coverage_gap,
        load_wrapped_example,
        summarize_validation_results,
    )
except ModuleNotFoundError as exc:
    msg = (
        "ERROR: The helper package 'fega_tools' is not importable.\n"
        "Make sure you have installed the repo in editable mode first. Run this from the repository root:\n"
        "    pip install -e ."
    )
    raise ModuleNotFoundError(msg) from exc


LOGGER = logging.getLogger(Path(__file__).stem)

SUMMARY_FILENAME = "summary.json"


def validate_file(path: Path, validator_url: str) -> Dict[str, Any]:
    """Validate one example file and return a result record."""
    result: Dict[str, Any] = {"file": str(path)}

    try:
        document = load_wrapped_example(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        result.update({"status": SCRIPT_ERROR_STATUS, "errors": [str(exc)]})
        return result

    result.update(validate_document(document, validator_url))
    return result


def summarize_category(
    entity_dir: Path,
    category: str,
    validator_url: str,
    coverage_gaps: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Validate one entity's examples for a category and summarize the result."""
    category_dir = entity_dir / "examples" / category
    files = find_example_files(entity_dir, category)
    expected_status = expected_status_for(category)
    results = []
    for path in files:
        result = validate_file(path, validator_url)
        results.append(result)
        outcome = "passed" if result["status"] == expected_status else "failed"
        LOGGER.debug("Validated '%s' [expected: %s] -> %s", path.name, category, outcome)
    summary = summarize_validation_results(
        results,
        expected_status,
        coverage_gaps=coverage_gaps_for_entity_category(
            coverage_gaps, entity_dir.name, category
        ),
    )
    summary.update({"expectation": category, "input_path": str(category_dir)})
    return summary


def summarize_entity(
    entity_dir: Path,
    validator_url: str,
    coverage_gaps: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Validate and summarize all example categories for one entity."""
    return {
        "entity": entity_dir.name,
        "categories": {
            category: summarize_category(
                entity_dir,
                category,
                validator_url,
                coverage_gaps,
            )
            for category in CATEGORIES
        },
    }


def validate_examples(
    root: Path,
    entity: str | None,
    validator_url: str,
) -> Dict[str, Any]:
    """Validate valid and invalid example suites under an entity root."""
    assert_validator_reachable(validator_url)

    entity_dirs = find_entity_dirs(root, entity)
    if not entity_dirs:
        raise FileNotFoundError(f"No entity schema directories found under {root}")

    coverage_gaps = find_example_coverage_gaps(entity_dirs, CATEGORIES)
    for gap in coverage_gaps:
        LOGGER.warning(
            "Coverage gap for %s: %s",
            gap["entity"],
            format_coverage_gap(gap),
        )

    file_summaries = [
        summarize_entity(entity_dir, validator_url, coverage_gaps)
        for entity_dir in entity_dirs
    ]
    totals = aggregate_category_summaries(file_summaries)
    category_totals = totals.pop("category_totals")
    input_paths = [
        entity_summary["categories"][category]["input_path"]
        for entity_summary in file_summaries
        for category in CATEGORIES
    ]
    valid_examples_passed = category_totals["valid"]["passed"]
    invalid_examples_passed = category_totals["invalid"]["passed"]

    return {
        "timestamp": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds"),
        "validator_url": validator_url,
        "root": str(root),
        "passed": valid_examples_passed and invalid_examples_passed,
        "entity": entity,
        "entity_names": [path.name for path in entity_dirs],
        "total_valid_files": category_totals["valid"]["total_files"],
        "total_invalid_files": category_totals["invalid"]["total_files"],
        **totals,
        "valid_examples_passed": valid_examples_passed,
        "invalid_examples_passed": invalid_examples_passed,
        "input_paths": input_paths,
        "category_totals": category_totals,
        "coverage_gaps": coverage_gaps,
        "files": file_summaries,
    }


def _log_results(summary: Dict[str, Any]) -> None:
    """Emit INFO-level result lines for the validation run."""
    cat = summary["category_totals"]
    valid_passed = cat["valid"]["validation_passed"]
    valid_total = cat["valid"]["total_files"]
    invalid_passed = cat["invalid"]["validation_failed"]
    invalid_total = cat["invalid"]["total_files"]

    LOGGER.info("%d / %d valid files passed validation", valid_passed, valid_total)
    LOGGER.info("%d / %d invalid files passed validation", invalid_passed, invalid_total)

    log_suite_status(LOGGER, summary["passed"])


def make_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for the validation runner."""
    parser = argparse.ArgumentParser(
        prog="validate_examples",
        description="Validate FEGA valid/invalid examples using a Biovalidator endpoint.",
        epilog=(
            "Examples:\n"
            "  validate_examples --entity cohort\n"
            "  validate_examples --root schemas/entities --summary-dir ."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_root_argument(parser, default=DEFAULT_ROOT)
    parser.add_argument(
        "--entity",
        help="Validate one entity by directory name, e.g. 'cohort'.",
    )
    parser.add_argument(
        "--url",
        "-u",
        dest="validator_url",
        default=DEFAULT_VALIDATOR_URL,
        help=f"Biovalidator /validate endpoint (default: {DEFAULT_VALIDATOR_URL})",
    )
    add_summary_arguments(parser, SUMMARY_FILENAME)
    add_verbosity_argument(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Run the command-line interface and exit with the suite status code."""
    parser = make_arg_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbosity)

    try:
        summary = validate_examples(args.root, args.entity, args.validator_url)
    except (FileNotFoundError, RuntimeError) as exc:
        LOGGER.error(str(exc))
        sys.exit(2)

    _log_results(summary)

    emit_summary(
        summary,
        summary_dir=args.summary_dir,
        summary_filename=SUMMARY_FILENAME,
        print_summary=args.print_summary,
    )

    sys.exit(0 if summary["passed"] else 1)


if __name__ == "__main__":
    main()
