#!/usr/bin/env python3
"""Validate schema-driven JSON-LD context and frame coverage."""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

try:
    from fega_tools.cli_utils import (
        add_root_argument,
        add_summary_arguments,
        add_verbosity_argument,
        emit_summary,
        help_with_example,
    )
    from fega_tools.jsonld_coverage import validate_jsonld_coverage
    from fega_tools.logging_utils import configure_logging, log_suite_status
    from fega_tools.validation_common import DEFAULT_ROOT
except ModuleNotFoundError as exc:
    msg = (
        "ERROR: The helper package 'fega_tools' is not importable.\n"
        "Make sure you have installed the repo in editable mode first. Run this from the repository root:\n"
        "    pip install -e ."
    )
    raise ModuleNotFoundError(msg) from exc


LOGGER = logging.getLogger(Path(__file__).stem)


SUMMARY_FILENAME = "jsonld_coverage_summary.json"


def _format_term_paths(terms: Sequence[str], path_map: Dict[str, Any]) -> str:
    """Format missing terms with schema paths for concise logs."""
    parts = []
    for term in terms:
        paths = path_map.get(term)
        if paths:
            parts.append(f"{term} ({', '.join(paths)})")
        else:
            parts.append(term)
    return ", ".join(parts)


def _log_entity_failures(summary: Dict[str, Any]) -> None:
    """Log concise entity-level failure details."""
    for entity_result in summary.get("files", []):
        if entity_result.get("passed"):
            continue
        entity = entity_result.get("entity", "<unknown>")
        if entity_result.get("missing_context_terms"):
            LOGGER.error(
                "%s missing context term(s): %s",
                entity,
                _format_term_paths(
                    entity_result["missing_context_terms"],
                    entity_result.get("missing_context_term_paths", {}),
                ),
            )
        if entity_result.get("missing_frame_keys"):
            LOGGER.error(
                "%s missing frame key(s): %s",
                entity,
                _format_term_paths(
                    entity_result["missing_frame_keys"],
                    entity_result.get("missing_frame_key_paths", {}),
                ),
            )
        if entity_result.get("unknown_frame_keys"):
            LOGGER.error(
                "%s unknown extra frame key(s): %s",
                entity,
                ", ".join(entity_result["unknown_frame_keys"]),
            )
        for error in entity_result.get("context_errors", []):
            LOGGER.error("%s context error: %s", entity, error)
        for error in entity_result.get("frame_errors", []):
            LOGGER.error("%s frame error: %s", entity, error)
        for error in entity_result.get("script_errors", []):
            LOGGER.error("%s script error: %s", entity, error)


def _log_results(summary: Dict[str, Any]) -> None:
    """Emit a concise suite-level summary."""
    LOGGER.info(
        "%d / %d entities passed JSON-LD coverage checks",
        summary["passed_entities"],
        summary["total_entities"],
    )
    LOGGER.info(
        "Missing context terms: %d; missing frame keys: %d; unknown extra frame keys: %d",
        summary["missing_context_terms"],
        summary["missing_frame_keys"],
        summary["unknown_frame_keys"],
    )
    if summary["script_errors"]:
        LOGGER.info("Script/setup errors: %d", summary["script_errors"])
    log_suite_status(LOGGER, summary["passed"])
    if not summary["passed"]:
        _log_entity_failures(summary)


def make_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="validate_jsonld_coverage",
        description=(
            "Validate that direct schema properties are covered by JSON-LD "
            "contexts and frames."
        ),
        epilog=(
            "Examples:\n"
            "  validate_jsonld_coverage --entity cohort\n"
            "  validate_jsonld_coverage --root schemas/entities --summary-dir ."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_root_argument(parser, default=DEFAULT_ROOT)
    parser.add_argument(
        "--entity",
        help=help_with_example("Validate one entity by directory name", "--entity cohort"),
    )
    add_summary_arguments(parser, SUMMARY_FILENAME)
    add_verbosity_argument(parser)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Run the CLI and exit with the suite status code."""
    parser = make_arg_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbosity)

    try:
        summary = validate_jsonld_coverage(args.root, args.entity)
    except (FileNotFoundError, RuntimeError) as exc:
        LOGGER.error(str(exc))
        sys.exit(2)

    summary["timestamp"] = _dt.datetime.now(tz=_dt.timezone.utc).isoformat(
        timespec="seconds"
    )

    _log_results(summary)

    emit_summary(
        summary,
        summary_dir=args.summary_dir,
        summary_filename=SUMMARY_FILENAME,
        print_summary=args.print_summary,
    )

    if summary["script_errors"]:
        sys.exit(2)
    sys.exit(0 if summary["passed"] else 1)


if __name__ == "__main__":
    main()
