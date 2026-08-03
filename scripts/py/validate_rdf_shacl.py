#!/usr/bin/env python3
"""Validate FEGA example suites against RDF/SHACL shapes."""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence, Set, Tuple

try:
    from fega_tools.cli_utils import (
        add_root_argument,
        add_summary_arguments,
        add_verbosity_argument,
        emit_summary,
    )
    from fega_tools.io import clone_json
    from fega_tools.jsonld_utils import (
        build_id_to_path_map,
        find_repo_root,
        materialize_context,
    )
    from fega_tools.logging_utils import configure_logging, log_suite_status
    from fega_tools.rdf_utils import (
        collect_candidate_rdf,
        extract_types_from_graph,
        jsonld_to_rdf_graph,
        load_rdf_from_file,
        validate_against_shacl,
    )
    from fega_tools.validation_common import (
        CATEGORIES,
        DEFAULT_ROOT,
        INVALID_STATUS,
        SCRIPT_ERROR_STATUS,
        VALID_STATUS,
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
        "    pip install -e .\n"
        "Also ensure dependencies are installed:\n"
        "    pip install -r requirements.txt"
    )
    raise ModuleNotFoundError(msg) from exc


LOGGER = logging.getLogger(Path(__file__).stem)


SUMMARY_FILENAME = "shacl_summary.json"


def materialize_data_context(
    jsonld_data: Dict[str, Any],
    input_path: Path,
    id_to_path_map: Dict[str, Path],
) -> Dict[str, Any]:
    """Return JSON-LD data with any repository context URL resolved locally."""
    context = jsonld_data.get("@context")
    if context is None:
        return jsonld_data

    data_copy = clone_json(jsonld_data)
    data_copy["@context"] = materialize_context(context, input_path, id_to_path_map)
    return data_copy


def root_has_required_type(
    jsonld_data: Dict[str, Any], required_root_type: str
) -> bool:
    """Return whether the top-level JSON-LD node has the required RDF type."""
    try:
        from pyld import jsonld

        expanded = jsonld.expand(jsonld_data)
    except Exception as exc:
        raise ValueError(f"Failed to expand JSON-LD while checking root type: {exc}") from exc

    return any(
        required_root_type in node.get("@type", [])
        for node in expanded
        if isinstance(node, dict)
    )


def _build_violation_summary(violations: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build a concise, deduplicated violation summary."""
    if not violations:
        return []

    message_counts: Dict[str, int] = {}
    unique_violations: List[Dict[str, Any]] = []

    for violation in violations:
        message = violation.get("message", "unknown")
        if message == "unknown" and "constraint_type" not in violation:
            continue

        key = message if message != "unknown" else violation.get("constraint_type", "unknown")
        message_counts[key] = message_counts.get(key, 0) + 1
        if message_counts[key] == 1:
            unique_violations.append(
                {
                    "message": message,
                    "constraint_type": violation.get("constraint_type", ""),
                    "count": 1,
                }
            )

    for violation in unique_violations:
        key = violation["message"] if violation["message"] != "unknown" else violation["constraint_type"]
        violation["count"] = message_counts[key]

    return unique_violations[:10]


def build_error_messages(violations: Sequence[Dict[str, Any]]) -> List[str]:
    """Return concise human-readable error messages for a SHACL failure."""
    unique_messages: Dict[str, int] = {}
    for violation in violations:
        message = violation.get("message", "")
        if message and message != "unknown":
            unique_messages[message] = unique_messages.get(message, 0) + 1

    error_messages = []
    for message, count in sorted(unique_messages.items(), key=lambda item: item[1], reverse=True):
        error_messages.append(f"{message} ({count})" if count > 1 else message)

    return error_messages if error_messages else ["Validation failed"]


def extract_expected_types_from_shapes(shape_graphs: Sequence[Any]) -> Set[str]:
    """Extract expected RDF types from SHACL shape target classes."""
    from rdflib import Namespace

    sh = Namespace("http://www.w3.org/ns/shacl#")
    expected_types: Set[str] = set()

    for graph in shape_graphs:
        for target_class in graph.objects(predicate=sh.targetClass):
            expected_types.add(str(target_class))

    return expected_types


def build_type_metrics(
    documents: Sequence[Dict[str, Any]],
    expected_types: Set[str],
) -> Dict[str, Any]:
    """Build metrics about RDF types found in documents vs expected by shapes."""
    types_found: Set[str] = set()

    for document in documents:
        for typ in document.get("types", []):
            types_found.add(typ)

    types_missing = expected_types - types_found
    types_unexpected = types_found - expected_types
    coverage = len(types_found & expected_types) / len(expected_types) if expected_types else 1.0

    return {
        "types_found": sorted(types_found),
        "types_expected": sorted(expected_types),
        "types_missing": sorted(types_missing),
        "types_unexpected": sorted(types_unexpected),
        "type_coverage": f"{coverage * 100:.1f}%",
    }


def load_shapes(shapes_paths: Sequence[Path]) -> Tuple[List[Path], List[Any], Any, Set[str]]:
    """Discover, parse, and merge SHACL shape files."""
    from rdflib import Graph

    shape_files = collect_candidate_rdf(list(shapes_paths))
    if not shape_files:
        raise FileNotFoundError("No RDF shape files were found under the given inputs")

    shape_graphs = []
    for shape_path in shape_files:
        try:
            LOGGER.debug("Loading shapes from '%s'", shape_path)
            shape_graphs.append(load_rdf_from_file(shape_path))
        except ValueError as exc:
            raise RuntimeError(f"Failed to load shape file '{shape_path}': {exc}") from exc

    merged_shapes = Graph()
    for graph in shape_graphs:
        merged_shapes += graph

    expected_types = extract_expected_types_from_shapes(shape_graphs)
    LOGGER.info("Discovered %d RDF shape file(s)", len(shape_files))
    LOGGER.debug("Expected types from shapes: %s", expected_types)

    return shape_files, shape_graphs, merged_shapes, expected_types


def validate_file_shacl(
    path: Path,
    shapes_graph: Any,
    id_to_path_map: Dict[str, Path],
    required_root_type: str | None = None,
) -> Dict[str, Any]:
    """Validate one wrapped EGA metadata file against merged SHACL shapes."""
    result: Dict[str, Any] = {"file": str(path)}

    try:
        document = load_wrapped_example(path)
        jsonld_data = materialize_data_context(document["data"], path, id_to_path_map)
        data_graph = jsonld_to_rdf_graph(jsonld_data)
        found_types = sorted(extract_types_from_graph(data_graph))
        if required_root_type:
            if not root_has_required_type(jsonld_data, required_root_type):
                result.update(
                    {
                        "status": INVALID_STATUS,
                        "conforms": False,
                        "types": found_types,
                        "n_violations": 0,
                        "violation_summary": [],
                        "errors": [
                            f"Required root RDF type not found: {required_root_type}"
                        ],
                    }
                )
                return result
        conforms, report_text, report_dict = validate_against_shacl(
            data_graph=data_graph,
            shapes_graph=shapes_graph,
        )
    except (OSError, json.JSONDecodeError, ValueError, KeyError) as exc:
        result.update(
            {
                "status": SCRIPT_ERROR_STATUS,
                "errors": [str(exc)],
                "conforms": False,
                "types": [],
                "n_violations": 0,
                "violation_summary": [],
            }
        )
        return result

    violations = report_dict.get("violations", [])
    status = VALID_STATUS if conforms else INVALID_STATUS
    result.update(
        {
            "status": status,
            "conforms": conforms,
            "types": found_types,
            "n_violations": len(violations),
            "violation_summary": _build_violation_summary(violations),
            "shacl_report": report_text,
        }
    )

    if not conforms:
        result["errors"] = build_error_messages(violations)

    return result


def summarize_category(
    entity_dir: Path,
    category: str,
    shapes_graph: Any,
    id_to_path_map: Dict[str, Path],
    expected_types: Set[str],
    coverage_gaps: Sequence[Dict[str, Any]],
    required_root_type: str | None,
) -> Dict[str, Any]:
    """Validate one entity's examples for one category and summarize results."""
    category_dir = entity_dir / "examples" / category
    files = find_example_files(entity_dir, category)
    expected_status = expected_status_for(category)
    results = []

    for path in files:
        result = validate_file_shacl(
            path, shapes_graph, id_to_path_map, required_root_type
        )
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
    summary.update(
        {
            "expectation": category,
            "input_path": str(category_dir),
            "type_metrics": build_type_metrics(results, expected_types),
        }
    )
    return summary


def summarize_entity(
    entity_dir: Path,
    shapes_graph: Any,
    id_to_path_map: Dict[str, Path],
    expected_types: Set[str],
    coverage_gaps: Sequence[Dict[str, Any]],
    required_root_type: str | None,
) -> Dict[str, Any]:
    """Validate and summarize all SHACL example categories for one entity."""
    return {
        "entity": entity_dir.name,
        "categories": {
            category: summarize_category(
                entity_dir,
                category,
                shapes_graph,
                id_to_path_map,
                expected_types,
                coverage_gaps,
                required_root_type,
            )
            for category in CATEGORIES
        },
    }


def validate_rdf_shacl(
    root: Path,
    entity: str | None,
    shapes_paths: Sequence[Path],
    *,
    all_entities: bool = False,
    include_shacl_reports: bool = False,
    required_root_type: str | None = None,
) -> Dict[str, Any]:
    """Validate valid and invalid FEGA examples against RDF/SHACL shapes."""
    entity_dirs = find_entity_dirs(
        root,
        entity,
        all_entities=all_entities,
        require_explicit=True,
    )
    if not entity_dirs:
        raise FileNotFoundError(f"No entity schema directories found under {root}")

    repo_root = find_repo_root(entity_dirs[0].resolve())
    id_to_path_map = build_id_to_path_map(repo_root)
    shape_files, _shape_graphs, merged_shapes, expected_types = load_shapes(shapes_paths)

    coverage_gaps = find_example_coverage_gaps(entity_dirs, CATEGORIES)
    for gap in coverage_gaps:
        LOGGER.warning(
            "Coverage gap for %s: %s", gap["entity"], format_coverage_gap(gap)
        )

    file_summaries = [
        summarize_entity(
            entity_dir,
            merged_shapes,
            id_to_path_map,
            expected_types,
            coverage_gaps,
            required_root_type,
        )
        for entity_dir in entity_dirs
    ]
    totals = aggregate_category_summaries(file_summaries)
    category_totals = totals.pop("category_totals")
    valid_examples_passed = category_totals["valid"]["passed"]
    invalid_examples_passed = category_totals["invalid"]["passed"]
    input_paths = [
        entity_summary["categories"][category]["input_path"]
        for entity_summary in file_summaries
        for category in CATEGORIES
    ]

    summary: Dict[str, Any] = {
        "timestamp": _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="seconds"),
        "root": str(root),
        "entity": entity,
        "entity_names": [path.name for path in entity_dirs],
        "shapes_paths": [str(path) for path in shapes_paths],
        "shape_files": [str(path) for path in shape_files],
        "passed": valid_examples_passed and invalid_examples_passed,
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

    if not include_shacl_reports:
        for entity_summary in summary["files"]:
            for category in CATEGORIES:
                for result in entity_summary["categories"][category]["files"]:
                    result.pop("shacl_report", None)

    return summary


def _log_results(summary: Dict[str, Any]) -> None:
    """Emit INFO-level result lines for the validation run."""
    category_totals = summary["category_totals"]
    valid_passed = category_totals["valid"]["validation_passed"]
    valid_total = category_totals["valid"]["total_files"]
    invalid_passed = category_totals["invalid"]["validation_failed"]
    invalid_total = category_totals["invalid"]["total_files"]

    LOGGER.info("%d / %d valid files passed SHACL validation", valid_passed, valid_total)
    LOGGER.info("%d / %d invalid files failed SHACL validation", invalid_passed, invalid_total)

    log_suite_status(LOGGER, summary["passed"])


def make_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line parser for RDF/SHACL suite validation."""
    parser = argparse.ArgumentParser(
        prog="validate_rdf_shacl",
        description="Validate FEGA valid/invalid example suites against RDF/SHACL shapes.",
        epilog=(
            "Examples:\n"
            "  validate_rdf_shacl --root schemas/entities --entity dataset "
            "--shapes standards/rdf/healthdcat-ap/release-6.0.0/shacl/non-public-shapes-v6.ttl -v\n"
            "  validate_rdf_shacl --root schemas/entities --all-entities "
            "--shapes standards/rdf/healthdcat-ap"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    add_root_argument(parser, default=DEFAULT_ROOT)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "--entity",
        help="Validate one entity by directory name, e.g. 'dataset'.",
    )
    selection.add_argument(
        "--all-entities",
        action="store_true",
        help="Validate every entity directory under --root.",
    )
    parser.add_argument(
        "--shapes",
        "-s",
        dest="shapes",
        nargs="+",
        type=Path,
        required=True,
        help="SHACL shape files or directories (TTL, RDF/XML, JSON-LD, etc.).",
    )
    add_summary_arguments(parser, SUMMARY_FILENAME)
    parser.add_argument(
        "--shacl-report",
        action="store_true",
        help="Include raw pySHACL validation reports in the JSON summary.",
    )
    parser.add_argument(
        "--required-root-type",
        help="Require a root RDF node with this type IRI before SHACL conformance.",
    )
    add_verbosity_argument(parser)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Run the command-line interface and exit with the suite status."""
    parser = make_arg_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbosity)

    try:
        summary = validate_rdf_shacl(
            args.root,
            args.entity,
            args.shapes,
            all_entities=args.all_entities,
            include_shacl_reports=args.shacl_report,
            required_root_type=args.required_root_type,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
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
