from __future__ import annotations

import json

from fega_tools.validation_common import (
    BASIC_COUNT_KEYS,
    add_counts,
    aggregate_category_summaries,
    coverage_gaps_for_entity_category,
    empty_counts,
    expected_status_for,
    find_entity_dirs,
    find_example_files,
    find_example_coverage_gaps,
    load_wrapped_example,
    summarize_validation_results,
)


def test_count_helpers_create_and_update_counters() -> None:
    """Check that the counter helpers create and accumulate totals correctly."""
    counts = empty_counts(BASIC_COUNT_KEYS)
    add_counts(counts, {"total_files": 2, "script_errors": 1}, BASIC_COUNT_KEYS)

    assert counts["total_files"] == 2
    assert counts["script_errors"] == 1
    assert counts["validation_passed"] == 0


def test_find_example_coverage_gaps_reports_missing_and_empty_dirs(tmp_path) -> None:
    """Check that missing and empty example directories are reported."""
    entity = tmp_path / "cohort"
    (entity / "schema.json").parent.mkdir(parents=True)
    (entity / "schema.json").write_text("{}", encoding="utf-8")
    (entity / "examples" / "valid").mkdir(parents=True)

    gaps = find_example_coverage_gaps([entity], ("valid", "invalid"))

    assert gaps == [{"entity": "cohort", "missing": ["invalid"], "empty": ["valid"]}]
    assert coverage_gaps_for_entity_category(gaps, "cohort", "valid") == gaps
    assert coverage_gaps_for_entity_category(gaps, "cohort", "invalid") == gaps
    assert coverage_gaps_for_entity_category(gaps, "cohort", "other") == []


def test_find_entity_dirs_and_load_wrapped_example(tmp_path) -> None:
    """Check entity discovery and loading of wrapped example documents."""
    entity = tmp_path / "dataset"
    entity.mkdir()
    (entity / "schema.json").write_text("{}", encoding="utf-8")
    example = tmp_path / "example.json"
    example.write_text(
        json.dumps({"schema": {"$ref": "schema.json"}, "data": {"@type": "x"}}),
        encoding="utf-8",
    )

    assert find_entity_dirs(tmp_path, None) == [entity]
    assert find_entity_dirs(tmp_path, "dataset", require_schema=True) == [entity]
    assert load_wrapped_example(example)["data"] == {"@type": "x"}


def test_example_file_and_result_helpers(tmp_path) -> None:
    entity = tmp_path / "cohort"
    valid_dir = entity / "examples" / "valid"
    valid_dir.mkdir(parents=True)
    example = valid_dir / "example.json"
    example.write_text("{}", encoding="utf-8")

    results = [{"file": str(example), "status": "validation_passed"}]
    summary = summarize_validation_results(results, expected_status_for("valid"))

    assert find_example_files(entity, "valid") == [example.resolve()]
    assert summary["passed"] is True
    assert summary["total_files"] == 1
    assert summary["expectation_failed_files"] == []


def test_result_helpers_require_expected_status_and_report_operational_errors() -> None:
    invalid_summary = summarize_validation_results(
        [{"file": "bad.json", "status": "validation_passed"}],
        expected_status_for("invalid"),
    )
    error_summary = summarize_validation_results(
        [{"file": "bad.json", "status": "script_error"}],
        expected_status_for("valid"),
    )

    assert invalid_summary["passed"] is False
    assert invalid_summary["n_failed_files"] == 1
    assert error_summary["script_errors"] == 1
    assert error_summary["passed"] is False


def test_category_aggregation_preserves_per_category_totals() -> None:
    entity_summaries = [
        {
            "categories": {
                "valid": summarize_validation_results(
                    [{"file": "valid.json", "status": "validation_passed"}],
                    "validation_passed",
                ),
                "invalid": summarize_validation_results(
                    [{"file": "invalid.json", "status": "validation_failed"}],
                    "validation_failed",
                ),
            }
        }
    ]

    totals = aggregate_category_summaries(entity_summaries)

    assert totals["total_files"] == 2
    assert totals["category_totals"]["valid"]["passed"] is True
    assert totals["category_totals"]["invalid"]["passed"] is True
    assert entity_summaries[0]["categories"]["invalid"]["n_failed_files"] == 0
