from __future__ import annotations

import logging

import pytest

from fega_tools.io import (
    collect_candidate_files,
    collect_candidate_json,
    clone_json,
    load_json_object,
)


def test_collect_candidate_files_is_recursive_case_insensitive_and_deduplicated(tmp_path) -> None:
    """Check that candidate file discovery is recursive, insensitive, and deduplicated."""
    nested = tmp_path / "nested"
    nested.mkdir()
    lower = nested / "lower.json"
    upper = nested / "UPPER.JSON"
    jsonld = nested / "document.JSONLD"
    ignored = nested / "ignored.txt"
    for path in (lower, upper, jsonld, ignored):
        path.write_text("{}", encoding="utf-8")

    result = collect_candidate_files(
        [tmp_path, lower], {"json", ".JSONLD"}
    )

    assert result == sorted({lower.resolve(), upper.resolve(), jsonld.resolve()})


def test_collect_candidate_json_keeps_jsonld_opt_in(tmp_path) -> None:
    """Check that JSON-LD discovery remains opt-in."""
    json_path = tmp_path / "document.json"
    jsonld_path = tmp_path / "document.jsonld"
    json_path.write_text("{}", encoding="utf-8")
    jsonld_path.write_text("{}", encoding="utf-8")

    assert collect_candidate_json([tmp_path]) == [json_path.resolve()]
    assert collect_candidate_json([tmp_path], include_jsonld=True) == sorted(
        [json_path.resolve(), jsonld_path.resolve()]
    )


def test_collect_candidate_files_missing_path_policy(tmp_path, caplog) -> None:
    """Check that missing paths can warn or raise according to policy."""
    missing = tmp_path / "missing"

    with caplog.at_level(logging.WARNING):
        assert collect_candidate_files([missing], {".json"}) == []
    assert str(missing) in caplog.text

    with pytest.raises(FileNotFoundError, match="Path not found"):
        collect_candidate_files([missing], {".json"}, missing="error")


def test_json_loading_and_cloning_helpers(tmp_path) -> None:
    """Check that JSON loading and cloning keep the original independent."""
    path = tmp_path / "value.json"
    path.write_text('{"nested": {"value": 1}}', encoding="utf-8")

    loaded = load_json_object(path)
    copied = clone_json(loaded)
    copied["nested"]["value"] = 2

    assert loaded["nested"]["value"] == 1
    assert copied["nested"]["value"] == 2
