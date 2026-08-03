"""Shared helpers for FEGA validation CLI scripts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Sequence

from fega_tools.io import collect_candidate_json, load_json_object

DEFAULT_ROOT = Path("schemas/entities")

VALID_STATUS = "validation_passed"
INVALID_STATUS = "validation_failed"
REQUEST_ERROR_STATUS = "request_error"
UNKNOWN_STATUS = "unknown_response"
SCRIPT_ERROR_STATUS = "script_error"

CATEGORIES = ("valid", "invalid")
BASIC_COUNT_KEYS = (
    "total_files",
    "completed_runs",
    "validation_passed",
    "validation_failed",
    "script_errors",
)
BIVALIDATOR_COUNT_KEYS = (
    "total_files",
    "completed_runs",
    "validation_passed",
    "validation_failed",
    "request_errors",
    "unknown_responses",
    "script_errors",
)
VALIDATION_COUNT_KEYS = BIVALIDATOR_COUNT_KEYS
RESULT_STATUSES = (
    VALID_STATUS,
    INVALID_STATUS,
    REQUEST_ERROR_STATUS,
    UNKNOWN_STATUS,
    SCRIPT_ERROR_STATUS,
)


def find_entity_dirs(
    root: Path,
    entity: str | None,
    *,
    all_entities: bool = False,
    require_explicit: bool = False,
    require_schema: bool = False,
) -> List[Path]:
    """Return selected entity directories under an entity schema root."""
    if entity:
        entity_dir = root / entity
        if not entity_dir.is_dir():
            raise FileNotFoundError(f"Entity directory not found: {entity_dir}")
        schema_path = entity_dir / "schema.json"
        if require_schema and not schema_path.is_file():
            raise FileNotFoundError(f"Entity schema not found: {schema_path}")
        return [entity_dir]

    if require_explicit and not all_entities:
        raise ValueError("Choose exactly one suite scope: --entity NAME or --all-entities")

    if not root.is_dir():
        raise FileNotFoundError(f"Entity root not found: {root}")

    return sorted(
        path
        for path in root.iterdir()
        if path.is_dir() and (path / "schema.json").is_file()
    )


def find_example_coverage_gaps(
    entity_dirs: Sequence[Path],
    categories: Sequence[str],
) -> List[Dict[str, Any]]:
    """Report missing or empty example directories for the requested categories."""
    gaps: List[Dict[str, Any]] = []

    for entity_dir in entity_dirs:
        missing: List[str] = []
        empty: List[str] = []

        for category in categories:
            category_dir = entity_dir / "examples" / category
            if not category_dir.is_dir():
                missing.append(category)
            elif not collect_candidate_json([category_dir]):
                empty.append(category)

        if missing or empty:
            gaps.append({"entity": entity_dir.name, "missing": missing, "empty": empty})

    return gaps


def find_example_files(entity_dir: Path, category: str) -> List[Path]:
    """Return JSON examples for one entity/category, or an empty list."""
    category_dir = entity_dir / "examples" / category
    return collect_candidate_json([category_dir]) if category_dir.is_dir() else []


def expected_status_for(expectation: str) -> str:
    """Return the validation status expected for a valid/invalid category."""
    if expectation == "valid":
        return VALID_STATUS
    if expectation == "invalid":
        return INVALID_STATUS
    raise ValueError(f"Unknown example expectation: {expectation}")


def coverage_gap_applies_to(gap: Dict[str, Any], category: str) -> bool:
    """Return whether a coverage gap affects one example category."""
    return category in gap.get("missing", []) or category in gap.get("empty", [])


def coverage_gaps_for_entity_category(
    coverage_gaps: Sequence[Dict[str, Any]],
    entity: str,
    category: str,
) -> List[Dict[str, Any]]:
    """Return coverage gaps for one entity/category pair."""
    return [
        gap
        for gap in coverage_gaps
        if gap.get("entity") == entity and coverage_gap_applies_to(gap, category)
    ]


def load_wrapped_example(path: Path) -> Dict[str, Any]:
    """Load a wrapped FEGA example and require top-level data/schema keys."""
    document = load_json_object(path)
    if not {"data", "schema"}.issubset(document):
        raise ValueError("Expected a JSON object containing both 'data' and 'schema' keys")

    return document


def empty_counts(count_keys: Sequence[str]) -> Dict[str, int]:
    """Create a zero-filled validation counter block."""
    return {key: 0 for key in count_keys}


def add_counts(
    target: Dict[str, int],
    source: Dict[str, Any],
    count_keys: Sequence[str],
) -> None:
    """Add validation counters from one summary into another."""
    for key in count_keys:
        target[key] += source.get(key, 0)


def summarize_validation_results(
    results: Sequence[Dict[str, Any]],
    expected_status: str,
    *,
    coverage_gaps: Sequence[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    """Build the shared counter and pass/fail block for file results."""
    if expected_status not in {VALID_STATUS, INVALID_STATUS}:
        raise ValueError(f"Unsupported expected status: {expected_status}")

    status_counts = empty_counts(RESULT_STATUSES)
    for result in results:
        status = result.get("status")
        if status not in status_counts:
            raise ValueError(f"Unknown validation result status: {status}")
        status_counts[status] += 1

    expectation_failed_files = [
        result.get("file", "")
        for result in results
        if result.get("status") != expected_status
    ]
    summary: Dict[str, Any] = {
        **empty_counts(VALIDATION_COUNT_KEYS),
        "coverage_gaps": list(coverage_gaps),
        "files": list(results),
        "expectation_failed_files": expectation_failed_files,
        "n_total_files": len(results),
        "n_failed_files": len(expectation_failed_files),
    }
    summary.update(
        {
            "total_files": len(results),
            "completed_runs": status_counts[VALID_STATUS]
            + status_counts[INVALID_STATUS],
            "validation_passed": status_counts[VALID_STATUS],
            "validation_failed": status_counts[INVALID_STATUS],
            "request_errors": status_counts[REQUEST_ERROR_STATUS],
            "unknown_responses": status_counts[UNKNOWN_STATUS],
            "script_errors": status_counts[SCRIPT_ERROR_STATUS],
        }
    )
    summary["passed"] = _validation_results_passed(summary, expected_status)
    return summary


def _validation_results_passed(summary: Dict[str, Any], expected_status: str) -> bool:
    """Return whether a summarized result set satisfies its expectation."""
    expected_count = (
        summary["validation_passed"]
        if expected_status == VALID_STATUS
        else summary["validation_failed"]
    )
    return (
        summary["total_files"] > 0
        and summary["completed_runs"] == summary["total_files"]
        and expected_count == summary["total_files"]
        and summary["validation_passed"] + summary["validation_failed"]
        == summary["total_files"]
        and summary["request_errors"] == 0
        and summary["unknown_responses"] == 0
        and summary["script_errors"] == 0
        and not summary.get("coverage_gaps")
    )


def aggregate_validation_counts(
    summaries: Sequence[Dict[str, Any]],
) -> Dict[str, int]:
    """Aggregate standard validation counters across summary records."""
    totals = empty_counts(VALIDATION_COUNT_KEYS)
    for summary in summaries:
        add_counts(totals, summary, VALIDATION_COUNT_KEYS)
    return totals


def aggregate_category_summaries(
    entity_summaries: Sequence[Dict[str, Any]],
    categories: Sequence[str] = CATEGORIES,
) -> Dict[str, Any]:
    """Aggregate nested entity/category validation summaries."""
    category_totals: Dict[str, Dict[str, Any]] = {
        category: {
            "expectation": category,
            "coverage_gaps": [],
            **empty_counts(VALIDATION_COUNT_KEYS),
        }
        for category in categories
    }
    totals = empty_counts(VALIDATION_COUNT_KEYS)

    for entity_summary in entity_summaries:
        for category in categories:
            category_summary = entity_summary["categories"][category]
            add_counts(category_totals[category], category_summary, VALIDATION_COUNT_KEYS)
            add_counts(totals, category_summary, VALIDATION_COUNT_KEYS)
            category_totals[category]["coverage_gaps"].extend(
                category_summary.get("coverage_gaps", [])
            )

    for category in categories:
        category_totals[category]["passed"] = _validation_results_passed(
            category_totals[category], expected_status_for(category)
        )

    totals["category_totals"] = category_totals
    return totals


def format_coverage_gap(gap: Dict[str, Any]) -> str:
    """Format missing/empty example categories for a log message."""
    details: List[str] = []
    if gap.get("missing"):
        details.append(f"missing {', '.join(gap['missing'])} examples")
    if gap.get("empty"):
        details.append(f"empty {', '.join(gap['empty'])} examples")
    return "; ".join(details)


def write_json_summary(summary: Dict[str, Any], summary_dir: Path, filename: str) -> None:
    """Write a machine-readable JSON summary file to an artifact directory."""
    summary_dir.mkdir(parents=True, exist_ok=True)
    with (summary_dir / filename).open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
        handle.write("\n")
