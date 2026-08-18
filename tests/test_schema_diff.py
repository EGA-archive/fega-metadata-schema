from __future__ import annotations

import pytest

from fega_tools.schema_diff import Severity, compare_schemas, validate_meta_enums


def _object(properties=None, required=None, **keywords):
    schema = {"type": "object", "properties": properties or {}}
    if required is not None:
        schema["required"] = required
    schema.update(keywords)
    return schema


def test_optional_property_on_open_object_is_unknown_and_required_is_major() -> None:
    old = _object()
    optional = _object({"name": {"type": "string"}})
    required = _object({"name": {"type": "string"}}, ["name"])
    assert compare_schemas(old, optional).severity == Severity.UNKNOWN
    assert compare_schemas(old, required).severity == Severity.MAJOR


def test_optional_property_on_closed_object_is_minor() -> None:
    old = _object(additionalProperties=False)
    new = _object({"name": {"type": "string"}}, additionalProperties=False)
    assert compare_schemas(old, new).severity == Severity.MINOR


@pytest.mark.parametrize(
    "old, expected",
    [
        (_object({"name": {"type": "string"}}), Severity.MINOR),
        (_object({"name": {"type": "string"}}, additionalProperties=False), Severity.MAJOR),
        (_object({"name": {"type": "string"}}, unevaluatedProperties=False), Severity.MAJOR),
        (_object({"name": {"type": "string"}}, additionalProperties={"type": "string"}), Severity.UNKNOWN),
        (_object({"name": {"type": "string"}}, unevaluatedProperties=False, allOf=[{}]), Severity.UNKNOWN),
    ],
)
def test_declared_property_removal_respects_object_closure(old, expected) -> None:
    assert compare_schemas(old, _object()).severity == expected


def test_required_list_removal_remains_minor_when_property_is_retained() -> None:
    old = _object({"name": {"type": "string"}}, ["name"])
    new = _object({"name": {"type": "string"}}, [])
    assert compare_schemas(old, new).severity == Severity.MINOR


def test_unconstrained_optional_property_on_open_object_is_same() -> None:
    old = _object()
    new = _object({"name": True})
    assert compare_schemas(old, new).severity == Severity.SAME


def test_required_removal_and_enum_expansion_are_minor() -> None:
    assert compare_schemas(_object({"x": {}}, ["x"]), _object({"x": {}}, [])).severity == Severity.MINOR
    assert compare_schemas({"enum": ["a"]}, {"enum": ["a", "b"]}).severity == Severity.MINOR


def test_enum_removal_and_tighter_bounds_are_major() -> None:
    assert compare_schemas({"enum": ["a", "b"]}, {"enum": ["a"]}).severity == Severity.MAJOR
    assert compare_schemas({"minimum": 1}, {"minimum": 2}).severity == Severity.MAJOR
    assert compare_schemas({"maxItems": 5}, {"maxItems": 4}).severity == Severity.MAJOR


def test_release_metadata_and_set_order_are_ignored() -> None:
    old = {
        "$id": "https://raw.githubusercontent.com/EGA-archive/fega-metadata-schema/main/x.json",
        "meta:version": "1.0.0",
        "type": ["string", "null"],
        "required": ["a", "b"],
    }
    new = {
        "$id": "https://raw.githubusercontent.com/EGA-archive/fega-metadata-schema/v2.0.0/x.json",
        "meta:version": "1.1.0",
        "type": ["null", "string"],
        "required": ["b", "a"],
    }
    assert compare_schemas(old, new).severity == Severity.SAME


def test_schema_diff_ignores_ref_only_changes_but_not_repository_changes() -> None:
    old = {"$id": "https://raw.githubusercontent.com/owner/repo/refs/heads/main/x.json"}
    ref_changed = {"$id": "https://raw.githubusercontent.com/owner/repo/refs/tags/v1.0.0/x.json"}
    repository_changed = {"$id": "https://raw.githubusercontent.com/fork/repo/refs/tags/v1.0.0/x.json"}
    assert compare_schemas(old, ref_changed).severity == Severity.SAME
    assert compare_schemas(old, repository_changed).severity == Severity.UNKNOWN


def test_composition_and_pattern_changes_are_unknown() -> None:
    assert compare_schemas({"pattern": "a"}, {"pattern": "b"}).severity == Severity.UNKNOWN
    assert compare_schemas({"oneOf": [{"type": "string"}]}, {"oneOf": [{"type": "number"}]}).severity == Severity.UNKNOWN


def test_annotations_are_patch() -> None:
    assert compare_schemas({"description": "old"}, {"description": "new"}).severity == Severity.PATCH


def test_meta_enum_keys_must_match_enum() -> None:
    valid = {"enum": ["a", "b"], "meta:enum": {"a": "A", "b": "B"}}
    invalid = {"enum": ["a", "b"], "meta:enum": {"a": "A", "c": "C"}}
    assert validate_meta_enums(valid) == []
    errors = validate_meta_enums({"properties": {"status": invalid}})
    assert len(errors) == 1
    assert "missing=['b']" in errors[0]
    assert "extra=['c']" in errors[0]
