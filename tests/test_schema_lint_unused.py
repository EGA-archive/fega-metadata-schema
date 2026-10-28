"""Advisory definition usage checks across selected and unselected schemas."""

from __future__ import annotations

import json

import pytest

from fega_tools.schema_lint import lint_schemas
from scripts.py.lint_schemas import main


def write(root, name, schema):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema))
    return path


def text_schema(**extra):
    return {
        "type": "string",
        "title": "Text",
        "minLength": 1,
        "examples": ["text"],
        **extra,
    }


def warning_paths(root, selected, **options):
    warnings = []
    lint_schemas(selected, root=root, warnings=warnings, **options)
    assert all(w.rule == "unused-definition" for w in warnings)
    return {(w.file, w.path) for w in warnings}


def test_same_file_references_ignore_annotations_and_distinguish_names(tmp_path):
    file = write(
        tmp_path,
        "schemas/schema.json",
        {
            "$defs": {
                "used": text_schema(),
                "unused": text_schema(),
                "alias": {"$ref": "#/$defs/used"},
            },
            "properties": {"value": {"$ref": "#/$defs/alias"}},
            "examples": [{"$ref": "#/$defs/unused"}],
            "default": {"$ref": "#/$defs/unused"},
        },
    )
    assert warning_paths(tmp_path, [file]) == {(file, ("$defs", "unused"))}


@pytest.mark.parametrize(
    "reference",
    [
        "common/schema.json#/$defs/a~1b~0c",
        "https://example.org/common#text",
        "https://example.org/nested",
        "common/schema.json#/$defs/nested/properties/value",
    ],
)
def test_unselected_consumers_use_ids_anchors_and_nested_locations(tmp_path, reference):
    common = write(
        tmp_path,
        "schemas/common/schema.json",
        {
            "$id": "https://example.org/common",
            "$defs": {
                "a/b~c": {"$anchor": "text", **text_schema()},
                "nested": {
                    "$id": "https://example.org/nested",
                    "properties": {"value": text_schema()},
                },
                "unused": text_schema(),
            },
        },
    )
    write(tmp_path, "schemas/consumer.schema.json", {"$ref": reference})
    paths = warning_paths(tmp_path, [common])
    used = "nested" if "nested" in reference else "a/b~c"
    assert (common, ("$defs", used)) not in paths
    assert (common, ("$defs", "unused")) in paths
    assert len(paths) == 2


def test_dynamic_anchor_reference_counts(tmp_path):
    file = write(
        tmp_path,
        "schemas/schema.json",
        {
            "$defs": {"used": {"$dynamicAnchor": "text", **text_schema()}},
            "properties": {"value": {"$dynamicRef": "#text"}},
        },
    )
    assert not warning_paths(tmp_path, [file])


def test_recursive_definitions_and_cycles_are_not_reachability_analysis(tmp_path):
    file = write(
        tmp_path,
        "schemas/schema.json",
        {
            "$defs": {
                "self": {"properties": {"child": {"$ref": "#/$defs/self"}}},
                "first": {"$ref": "#/$defs/second"},
                "second": {"$ref": "#/$defs/first"},
            },
        },
    )
    assert warning_paths(tmp_path, [file]) == {(file, ("$defs", "self"))}


def test_nested_defs_are_checked_without_marking_all_children_used(tmp_path):
    file = write(
        tmp_path,
        "schemas/schema.json",
        {
            "$defs": {"outer": {"$defs": {"inner": text_schema()}}},
            "properties": {"value": {"$ref": "#/$defs/outer"}},
        },
    )
    assert warning_paths(tmp_path, [file]) == {
        (file, ("$defs", "outer", "$defs", "inner"))
    }


@pytest.mark.parametrize("value", [True, False])
def test_boolean_defs_are_distinguished_and_pointer_escapes_decoded(tmp_path, value):
    file = write(
        tmp_path,
        "schemas/schema.json",
        {
            "$defs": {"a/b~ c": value, "unused": value},
            "properties": {"value": {"$ref": "#/$defs/a~1b~0%20c"}},
        },
    )
    assert warning_paths(tmp_path, [file]) == {(file, ("$defs", "unused"))}


def test_standards_and_examples_do_not_count_as_consumers_even_with_wider_scope(
    tmp_path,
):
    file = write(tmp_path, "schemas/schema.json", {"$defs": {"unused": text_schema()}})
    write(
        tmp_path,
        "standards/external.schema.json",
        {"$ref": "../schemas/schema.json#/$defs/unused"},
    )
    write(
        tmp_path, "schemas/examples/data.json", {"$ref": "../schema.json#/$defs/unused"}
    )
    assert warning_paths(tmp_path, [file], resolution_root=tmp_path) == {
        (file, ("$defs", "unused"))
    }


def test_resolution_boundary_is_respected_and_sibling_warnings_not_reported(tmp_path):
    file = write(
        tmp_path, "schemas/common/schema.json", {"$defs": {"unused": text_schema()}}
    )
    write(
        tmp_path,
        "schemas/consumer.schema.json",
        {
            "$defs": {"other": text_schema()},
            "$ref": "common/schema.json#/$defs/unused",
        },
    )
    assert not warning_paths(tmp_path, [file])
    assert warning_paths(
        tmp_path, [file], resolution_root=tmp_path / "schemas/common"
    ) == {(file, ("$defs", "unused"))}


@pytest.mark.parametrize(
    "broken",
    [
        "{broken",
        "42",
        '{"$id": 42}',
        '{"$defs": []}',
        '{"$schema": 12}',
        '{"$defs": {"bad": {"$id": 42}}}',
    ],
)
def test_broken_refs_and_unselected_json_do_not_stop_usage_check(tmp_path, broken):
    file = write(
        tmp_path,
        "schemas/schema.json",
        {
            "$defs": {"unused": text_schema()},
            "properties": {"value": {"$ref": "#/$defs/missing"}},
        },
    )
    (tmp_path / "schemas/broken.schema.json").write_text(broken)
    assert warning_paths(tmp_path, [file]) == {(file, ("$defs", "unused"))}


@pytest.mark.parametrize("verbosity", [[], ["-v"], ["-vv"]])
def test_warning_only_cli_succeeds_and_details_require_vv(tmp_path, capsys, verbosity):
    file = write(
        tmp_path,
        "schemas/schema.json",
        {
            "type": "object",
            "title": "Root",
            "$defs": {"unused": text_schema()},
        },
    )
    before = file.read_bytes()
    assert main(["--root", str(tmp_path), "--color", "never", *verbosity]) == 0
    output = capsys.readouterr().out
    assert "Linted 1 schema(s): 0 violation(s); 1 warning(s)." in output
    assert ("[unused-definition]" in output) == (verbosity == ["-vv"])
    assert ("Path: #/$defs/unused" in output) == (verbosity == ["-vv"])
    assert file.read_bytes() == before


def test_errors_still_fail_with_warnings(tmp_path, capsys):
    write(
        tmp_path,
        "schemas/schema.json",
        {"type": "object", "$defs": {"unused": text_schema()}},
    )
    assert main(["--root", str(tmp_path), "-vv"]) == 1
    output = capsys.readouterr().out
    assert "1 violation(s); 1 warning(s)" in output
    assert "[documentation]" in output and "[unused-definition]" in output
