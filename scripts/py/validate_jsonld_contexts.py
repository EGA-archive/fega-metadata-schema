#!/usr/bin/env python3
"""Validate JSON-LD contexts in FEGA valid examples using rdflib.

Resolves all context references locally (no network calls) and parses each
example's data as RDF, checking that the graph contains at least one triple
and at least one rdf:type triple.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import rdflib
from rdflib.namespace import RDF

try:
    from fega_tools.cli_utils import (
        add_root_argument,
        add_summary_arguments,
        add_verbosity_argument,
        emit_summary,
    )
    from fega_tools.io import clone_json, load_json
    from fega_tools.logging_utils import configure_logging, log_suite_status
    from fega_tools.jsonld_utils import (
        find_repo_root,
        build_id_to_path_map,
        materialize_context,
        resolve_ref,
    )
    from fega_tools.validation_common import (
        DEFAULT_ROOT,
        INVALID_STATUS,
        SCRIPT_ERROR_STATUS,
        VALID_STATUS,
        aggregate_validation_counts,
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

SUMMARY_FILENAME = "jsonld_summary.json"


# ---------------------------------------------------------------------------
# Per-file validation
# ---------------------------------------------------------------------------

def _context_url_is_acceptable(
    context: Any,
    schema_ref: str,
    example_path: Path,
    id_to_path_map: Dict[str, Path],
) -> bool:
    """Return whether a context resolves to the schema's effective context file."""
    if not isinstance(context, str):
        return False
    if context == schema_ref:
        return True
    schema_path = id_to_path_map.get(schema_ref)
    if schema_path is None:
        return False
    try:
        schema = load_json(schema_path)
        schema_context = schema.get("@context")
        expected_path = (
            resolve_ref(schema_context, schema_path, id_to_path_map)
            if isinstance(schema_context, str)
            else schema_path.with_name("context.jsonld")
        )
        actual_path = resolve_ref(context, example_path, id_to_path_map)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return False
    return actual_path.resolve() == expected_path.resolve()


def validate_file_jsonld(
    path: Path,
    id_to_path_map: Dict[str, Path],
) -> Dict[str, Any]:
    """Validate one example file and return a result record."""
    result: Dict[str, Any] = {"file": str(path)}

    try:
        document = load_wrapped_example(path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        result.update({"status": SCRIPT_ERROR_STATUS, "errors": [str(exc)]})
        return result

    data = document["data"]
    schema = document["schema"]

    if not isinstance(data, dict):
        result.update({"status": SCRIPT_ERROR_STATUS, "errors": ["'data' must be a JSON object"]})
        return result

    errors: List[str] = []

    # Step 2: schema.$ref
    schema_ref: str = schema.get("$ref", "") if isinstance(schema, dict) else ""
    if not schema_ref:
        errors.append("Missing schema.$ref")

    # Step 3: data.@context present
    context = data.get("@context")
    if context is None:
        errors.append("Missing data.@context")

    # Step 4: data.@context value check
    if context is not None and schema_ref and not _context_url_is_acceptable(
        context, schema_ref, path, id_to_path_map
    ):
        errors.append(
            f"data.@context '{context}' does not match schema.$ref '{schema_ref}' "
            f"nor the expected context.jsonld URL"
        )

    # Step 5: data.@type
    if "@type" not in data:
        errors.append("Missing data.@type")

    if errors:
        result.update({"status": INVALID_STATUS, "errors": errors})
        return result

    # Step 6: Materialize context locally.
    try:
        materialized_ctx = materialize_context(context, path, id_to_path_map)
    except (FileNotFoundError, ValueError, OSError, json.JSONDecodeError) as exc:
        result.update(
            {"status": INVALID_STATUS, "errors": [f"Context materialization failed: {exc}"]}
        )
        return result

    # Step 7: RDF parse – replace @context with materialized version.
    data_copy: Dict[str, Any] = clone_json(data)
    data_copy["@context"] = materialized_ctx

    try:
        graph = rdflib.Dataset()
        graph.parse(data=json.dumps(data_copy), format="json-ld", base=schema_ref)
    except Exception as exc:  # noqa: BLE001 – rdflib raises diverse exceptions
        result.update({"status": INVALID_STATUS, "errors": [f"RDF parse failed: {exc}"]})
        return result

    triples = list(graph.quads((None, None, None, None)))
    if not triples:
        result.update({"status": INVALID_STATUS, "errors": ["RDF graph contains no triples"]})
        return result

    rdf_type_triples = [quad for quad in triples if quad[1] == RDF.type]
    if not rdf_type_triples:
        result.update(
            {"status": INVALID_STATUS, "errors": ["RDF graph contains no rdf:type triples"]}
        )
        return result

    result.update(
        {
            "status": VALID_STATUS,
            "n_triples": len(triples),
            "n_type_triples": len(rdf_type_triples),
        }
    )
    return result


# ---------------------------------------------------------------------------
# Summarization helpers
# ---------------------------------------------------------------------------

def summarize_entity(
    entity_dir: Path,
    id_to_path_map: Dict[str, Path],
    coverage_gaps: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Validate and summarise valid examples for one entity."""
    valid_dir = entity_dir / "examples" / "valid"
    files = find_example_files(entity_dir, "valid")

    entity_coverage_gaps = [g for g in coverage_gaps if g.get("entity") == entity_dir.name]

    results: List[Dict[str, Any]] = []
    for path in files:
        file_result = validate_file_jsonld(path, id_to_path_map)
        results.append(file_result)
        outcome = "passed" if file_result["status"] == VALID_STATUS else "failed"
        LOGGER.debug("Validated '%s' -> %s", path.name, outcome)

    summary = summarize_validation_results(
        results, VALID_STATUS, coverage_gaps=entity_coverage_gaps
    )
    summary.update({"entity": entity_dir.name, "input_path": str(valid_dir)})
    return summary


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------

def validate_jsonld_contexts(
    root: Path,
    entity: Optional[str],
    repo_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run JSON-LD context smoke tests for all valid examples under *root*.

    Parameters
    ----------
    root:       Entity schema root (default: schemas/entities).
    entity:     Restrict to one entity directory by name.
    repo_root:  Repository root used to resolve local schema paths. When
                *None*, auto-detected by walking up from *root*.
    """
    if repo_root is None:
        repo_root = find_repo_root(root.resolve())

    id_to_path_map = build_id_to_path_map(repo_root)
    LOGGER.debug("Loaded %d entries in schema/context map", len(id_to_path_map))

    entity_dirs = find_entity_dirs(root, entity)
    if not entity_dirs:
        raise FileNotFoundError(f"No entity schema directories found under {root}")

    coverage_gaps = find_example_coverage_gaps(entity_dirs, ("valid",))
    for gap in coverage_gaps:
        LOGGER.error(
            "Coverage gap for %s: %s — all valid examples must include @context",
            gap["entity"],
            format_coverage_gap(gap),
        )

    entity_summaries = [
        summarize_entity(entity_dir, id_to_path_map, coverage_gaps)
        for entity_dir in entity_dirs
    ]

    totals = aggregate_validation_counts(entity_summaries)
    overall_passed = all(s["passed"] for s in entity_summaries)

    input_paths = [s["input_path"] for s in entity_summaries]

    return {
        "timestamp": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds"),
        "root": str(root),
        "passed": overall_passed,
        "entity": entity,
        "entity_names": [path.name for path in entity_dirs],
        "total_valid_files": totals["total_files"],
        **totals,
        "valid_examples_passed": overall_passed,
        "input_paths": input_paths,
        "coverage_gaps": coverage_gaps,
        "files": entity_summaries,
    }


# ---------------------------------------------------------------------------
# Logging / output
# ---------------------------------------------------------------------------

def _log_results(summary: Dict[str, Any]) -> None:
    """Emit INFO-level result lines for the validation run."""
    passed_count = summary["validation_passed"]
    total_count = summary["total_valid_files"]
    LOGGER.info("%d / %d valid files passed JSON-LD context checks", passed_count, total_count)

    log_suite_status(LOGGER, summary["passed"])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def make_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(
        prog="validate_jsonld_contexts",
        description=(
            "Validate JSON-LD contexts in FEGA valid examples.\n"
            "Resolves all context references locally; does not require Biovalidator."
        ),
        epilog=(
            "Examples:\n"
            "  validate_jsonld_contexts --entity cohort\n"
            "  validate_jsonld_contexts --root schemas/entities --summary-dir ."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_root_argument(parser, default=DEFAULT_ROOT)
    parser.add_argument(
        "--entity",
        help="Validate one entity by directory name, e.g. 'cohort'.",
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
        summary = validate_jsonld_contexts(args.root, args.entity)
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
