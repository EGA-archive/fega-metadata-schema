from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


SCRIPT_ROOT = Path(__file__).resolve().parents[1] / "scripts" / "py"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from modify_ids import _collect_candidates, _read_text, _write_text, main


def test_collect_candidates_supports_jsonld_and_preserves_output_layout(tmp_path) -> None:
    """Check that JSON and JSON-LD candidates retain their output paths."""
    source_root = tmp_path / "schemas"
    nested = source_root / "entities" / "cohort"
    nested.mkdir(parents=True)
    json_path = nested / "example.JSON"
    jsonld_path = nested / "context.jsonld"
    json_path.write_text("{}", encoding="utf-8")
    jsonld_path.write_text("{}", encoding="utf-8")

    candidates = _collect_candidates([source_root])

    assert candidates == [
        (json_path.resolve(), Path("schemas/entities/cohort/example.JSON")),
        (jsonld_path.resolve(), Path("schemas/entities/cohort/context.jsonld")),
    ]


def test_collect_candidates_rejects_output_path_collisions(tmp_path) -> None:
    """Check that conflicting input paths are rejected."""
    first = tmp_path / "first" / "same"
    second = tmp_path / "second" / "same"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "document.json").write_text("{}", encoding="utf-8")
    (second / "document.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="Multiple inputs map"):
        _collect_candidates([first, second])


def test_collect_candidates_rejects_overlapping_inputs(tmp_path) -> None:
    """Check that one source cannot map to two output-relative paths."""
    source_root = tmp_path / "schemas"
    source_root.mkdir()
    child = source_root / "document.json"
    child.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="maps to multiple output paths"):
        _collect_candidates([source_root, child])


def test_modify_ids_text_helpers_preserve_newlines(tmp_path) -> None:
    """Check that text helpers preserve existing line endings."""
    source = tmp_path / "source.json"
    target = tmp_path / "nested" / "target.json"
    content = '{\r\n  "value": 1\r\n}\r\n'
    source.write_bytes(content.encode("utf-8"))

    assert _read_text(source) == content
    _write_text(target, content)
    assert target.read_bytes() == content.encode("utf-8")


def test_main_rewrites_escaped_urls_from_parsed_json(tmp_path, capsys) -> None:
    """Check that escaped URL syntax is rewritten through the parsed tree."""
    source = tmp_path / "escaped.json"
    url = "https://raw.githubusercontent.com/old-owner/repo/main/schema.json"
    escaped_json = json.dumps({"$ref": url}).replace("/", "\\/")
    source.write_bytes((escaped_json.replace("\n", "\r\n") + "\r\n").encode())

    with pytest.raises(SystemExit) as exit_info:
        main([str(source), "--branch", "main", "dev", "--in-place"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out == ""
    assert "\r\n" in source.read_bytes().decode()
    assert json.loads(source.read_text(encoding="utf-8"))["$ref"].split("/")[5] == "dev"


def test_main_no_match_returns_failure_without_normal_output(tmp_path, capsys) -> None:
    """Check that a no-match run keeps the pre-refactor CLI contract."""
    source = tmp_path / "document.json"
    source.write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit) as exit_info:
        main([str(source), "--branch", "missing", "dev"])

    assert exit_info.value.code == 1
    assert capsys.readouterr().out == ""


def test_main_verbose_matching_dry_run_prints_summary(tmp_path, capsys) -> None:
    """Check that a matching dry run succeeds and reports its summary."""
    source = tmp_path / "document.json"
    source.write_text(
        json.dumps(
            {"$ref": "https://raw.githubusercontent.com/old-owner/repo/main/schema.json"}
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exit_info:
        main([str(source), "--branch", "main", "dev", "-v"])

    assert exit_info.value.code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["n_modified"] == 1
    assert summary["modified_files"] == [str(source.resolve())]
