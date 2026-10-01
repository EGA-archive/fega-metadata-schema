from __future__ import annotations

import json
from pathlib import Path

import pytest

from fega_tools.schema_lint import discover_schemas, lint_schemas
from fega_tools.schema_lint.rules import KEYWORD_ORDER
from scripts.py.lint_schemas import main

ROOT = Path(__file__).resolve().parents[1]
DIALECT = "https://json-schema.org/draft/2020-12/schema"


def ordered(schema):
    """Return a schema with keys sorted by the linter's keyword order."""
    ranks = {key: index for index, key in enumerate(KEYWORD_ORDER)}
    return dict(sorted(schema.items(), key=lambda item: ranks.get(item[0], -1)))


def string(**extra):
    """Return a basic documented string schema."""
    return ordered(
        dict(
            type="string",
            title="Text",
            description="Some text.",
            minLength=1,
            examples=["abc"],
            **extra,
        )
    )


def write(root, schema, name="schema.json"):
    """Write a schema fixture and return its path."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema), encoding="utf-8")
    return path


def lint(root, schema):
    """Lint a schema written under the test directory."""
    return lint_schemas([write(root, schema)], root=root)


def rules(issues):
    """Return the rule names reported by a lint run."""
    return {issue.rule for issue in issues}


def test_clean_schema_and_boolean_constraints(tmp_path):
    """Check valid schemas and boolean schemas have no issues."""
    assert not lint(tmp_path, string())
    assert not lint(tmp_path, True)
    assert not lint(tmp_path, False)


def test_description_is_optional_but_title_is_required(tmp_path):
    """Omitting a description is valid; omitting a title still fails."""
    schema = string()
    del schema["description"]
    assert not lint(tmp_path, schema)
    del schema["title"]
    issues = lint(tmp_path, schema)
    assert [(issue.rule, issue.path) for issue in issues] == [
        ("documentation", ("title",))
    ]


@pytest.mark.parametrize(
    "description, expected_rule",
    [("", "documentation"), ("   ", "documentation"), (42, "schema-structure")],
)
def test_supplied_description_must_be_nonempty_text(
    tmp_path, description, expected_rule
):
    """Optional descriptions must still be useful when supplied."""
    schema = string()
    schema["description"] = description
    assert expected_rule in rules(lint(tmp_path, schema))


@pytest.mark.parametrize(
    "filename",
    [
        "schemas/entities/DAC/schema.json",
        "schemas/entities/dataset/schema.json",
        "schemas/common/schema.json",
        "schemas/graph/schema.json",
        "schemas/entities/DAC/nested/schema.json",
        "schemas/entities/DAC/other.schema.json",
    ],
)
def test_schema_file_roots_can_omit_examples(tmp_path, filename):
    """File roots need no examples, but their properties and definitions do."""
    nested = string()
    del nested["examples"]
    selected = write(
        tmp_path,
        {
            "type": "object",
            "title": "Entity",
            "properties": {"name": nested},
            "$defs": {"text": nested},
        },
        filename,
    )
    issues = lint_schemas([selected], root=tmp_path)
    expected = {("properties", "name", "examples"), ("$defs", "text", "examples")}
    assert {issue.path for issue in issues if issue.rule == "documentation"} == expected


@pytest.mark.parametrize(
    "examples, expected_rule", [([], "documentation"), ([42], "example-validation")]
)
@pytest.mark.parametrize(
    "filename", ["schemas/entities/DAC/schema.json", "schemas/common/schema.json"]
)
def test_supplied_root_examples_are_checked(
    tmp_path, examples, expected_rule, filename
):
    """The root exemption does not permit empty arrays or invalid examples."""
    selected = write(
        tmp_path,
        {"type": "object", "title": "Entity", "examples": examples},
        filename,
    )
    assert expected_rule in rules(lint_schemas([selected], root=tmp_path))


@pytest.mark.parametrize("values", [[""], [{}], [[]], ["", {}, []], [{}, {}]])
@pytest.mark.parametrize("location", ["root", "properties", "$defs", "reference"])
def test_examples_cannot_contain_only_empty_values(tmp_path, values, location):
    """Empty-only examples provide no documentation, including on reuse nodes."""
    schema = {"title": "Examples", "examples": values}
    path = ("examples",)
    if location in {"properties", "$defs"}:
        schema = {"type": "object", "title": "Container", location: {"value": schema}}
        path = (location, "value", "examples")
    elif location == "reference":
        write(tmp_path, {}, "base.schema.json")
        schema["$ref"] = "base.schema.json"
    issues = lint(tmp_path, schema)
    assert [
        (issue.path, issue.message) for issue in issues if issue.rule == "documentation"
    ] == [
        (
            path,
            "examples must include at least one value other than an empty string, object, or array.",
        )
    ]


@pytest.mark.parametrize("value", ["text", {"name": "value"}, [1], 0, False, None])
def test_empty_examples_can_accompany_other_values(tmp_path, value):
    """Mixed examples are allowed; false, zero and null are not empty containers."""
    issues = lint(tmp_path, {"title": "Examples", "examples": ["", {}, [], value]})
    assert "documentation" not in rules(issues)
    assert "example-validation" not in rules(issues)


def test_mixed_examples_still_validate_against_schema(tmp_path):
    """An informative example does not excuse another example's invalid value."""
    schema = string()
    schema["examples"] = ["", "text"]
    issues = lint(tmp_path, schema)
    assert "documentation" not in rules(issues)
    assert {issue.path for issue in issues if issue.rule == "example-validation"} == {
        ("examples", 0)
    }


def test_entity_root_still_needs_title(tmp_path):
    """Only examples are optional at an independent entity root."""
    selected = write(tmp_path, {"type": "object"}, "schemas/entities/DAC/schema.json")
    issues = lint_schemas([selected], root=tmp_path)
    assert [(issue.rule, issue.path) for issue in issues] == [
        ("documentation", ("title",))
    ]


def test_definitions_fragments_and_annotation_data(tmp_path):
    """Check annotations and schema fragments are handled separately."""
    schema = ordered(
        {
            "type": "object",
            "title": "Object",
            "description": "An object.",
            "properties": {
                "bad_name": {"type": "string"},
                "@id": {"$ref": "#/$defs/text"},
            },
            "$defs": {"text": string()},
            "if": {"required": ["bad_name"]},
            "then": {"required": ["@id"]},
            "examples": [
                {"bad_name": "text", "@id": "x", "examples": {"$ref": "missing"}}
            ],
            "@context": {"foo": {"$ref": "not-a-schema"}},
        }
    )
    issues = lint(tmp_path, schema)
    assert rules(issues) == {"documentation", "nonempty-values", "property-name"}
    assert all(
        issue.path[:1] not in {("if",), ("then",), ("@context",), ("examples",)}
        for issue in issues
    )


@pytest.mark.parametrize("extra", [{}, {"title": "Reuse"}, {"examples": ["ok"]}])
def test_pure_reference_inherits_metadata_but_siblings_need_allof(tmp_path, extra):
    """Check pure references inherit metadata and reject sibling constraints."""
    write(tmp_path, string(), "base.schema.json")
    issues = lint(tmp_path, ordered({"$ref": "base.schema.json", **extra}))
    assert rules(issues) == ({"reference-style"} if extra else set())


@pytest.mark.parametrize(
    "schema, expected",
    [
        ({"allOf": [{"$ref": "base.schema.json"}]}, {"reference-style"}),
        ({"allOf": [{"title": "Inherited text", "$ref": "base.schema.json"}]}, set()),
        ({"title": "Text alias", "allOf": [{"$ref": "base.schema.json"}]}, set()),
        (
            {"$ref": "base.schema.json", "maxLength": 2},
            {"reference-style", "documentation"},
        ),
        (
            {"maxLength": 2, "allOf": [{"$ref": "base.schema.json"}]},
            {"documentation"},
        ),
    ],
)
def test_reference_representation_and_constrained_definitions(
    tmp_path, schema, expected
):
    """Check references use the supported representation."""
    write(tmp_path, string(), "base.schema.json")
    assert rules(lint(tmp_path, schema)) == expected


@pytest.mark.parametrize("declared", ["string", ["string", "null"], "array"])
def test_type_can_be_inherited_transitively(tmp_path, declared):
    """Constrained definitions can inherit types through relative reference chains."""
    write(tmp_path, {"$ref": "base.schema.json#/$defs/value"}, "defs/alias.schema.json")
    write(
        tmp_path,
        {"$defs": {"value": {"type": declared, "$ref": "alias.schema.json"}}},
        "defs/base.schema.json",
    )
    issues = lint(
        tmp_path,
        {"maxLength": 5, "allOf": [{"$ref": "defs/alias.schema.json"}]},
    )
    assert "explicit-type" not in rules(issues)
    assert "documentation" in rules(issues)


@pytest.mark.parametrize("transitive", [False, True])
def test_out_of_scope_type_does_not_count(tmp_path, transitive):
    """Type inheritance respects the same boundary as inherited size constraints."""
    write(tmp_path, string(), "standards/base.schema.json")
    ref = "../standards/base.schema.json"
    if transitive:
        write(tmp_path, {"$ref": ref}, "schemas/alias.schema.json")
        ref = "alias.schema.json"
    selected = write(
        tmp_path,
        {"maxLength": 5, "allOf": [{"$ref": ref}]},
        "schemas/schema.json",
    )
    assert "explicit-type" in rules(lint_schemas([selected], root=tmp_path))
    assert "explicit-type" not in rules(
        lint_schemas([selected], root=tmp_path, resolution_root=Path("."))
    )


@pytest.mark.parametrize("keyword", ["allOf", "anyOf", "oneOf"])
@pytest.mark.parametrize("via_ref", [False, True])
def test_type_can_appear_in_a_composition_branch(tmp_path, keyword, via_ref):
    """One typed leaf suffices, even alongside an untyped alternative."""
    branch = {"anyOf": [{"oneOf": [{"type": "string"}, {"type": "array"}]}]}
    if via_ref:
        write(tmp_path, branch, "defs/typed.schema.json")
        write(tmp_path, {"$ref": "typed.schema.json"}, "defs/alias.schema.json")
        branch = {"$ref": "defs/alias.schema.json"}
    issues = lint(tmp_path, {"maxLength": 5, keyword: [{}, branch]})
    assert "explicit-type" not in rules(issues)


def test_contact_point_union_types_and_examples(tmp_path):
    """The @type property needs no redundant type next to its alternatives."""
    issues = lint(
        tmp_path,
        {
            "type": "object",
            "properties": {
                "@type": {
                    "anyOf": [
                        {"type": "string", "const": "cv:ContactPoint"},
                        {
                            "type": "array",
                            "title": "Contact point type collection",
                            "uniqueItems": True,
                            "minItems": 2,
                            "contains": {"const": "cv:ContactPoint"},
                            "examples": [["cv:ContactPoint", "vcard:Kind"]],
                        },
                    ],
                    "examples": [
                        "cv:ContactPoint",
                        ["cv:ContactPoint", "vcard:Kind"],
                        42,
                    ],
                }
            },
        },
    )
    assert "explicit-type" not in rules(issues)
    assert {issue.path for issue in issues if issue.rule == "example-validation"} == {
        ("properties", "@type", "examples", 2)
    }


@pytest.mark.parametrize("keyword", ["anyOf", "oneOf"])
def test_composed_type_respects_resolution_scope(tmp_path, keyword):
    """Alternatives cannot import type declarations across the resolution boundary."""
    write(tmp_path, {"type": "string"}, "standards/base.schema.json")
    selected = write(
        tmp_path,
        {keyword: [{"$ref": "../standards/base.schema.json"}, {}]},
        "schemas/schema.json",
    )
    assert "explicit-type" in rules(lint_schemas([selected], root=tmp_path))
    assert "explicit-type" not in rules(
        lint_schemas([selected], root=tmp_path, resolution_root=Path("."))
    )


@pytest.mark.parametrize("typed", [False, True])
def test_composed_type_cycles_terminate(tmp_path, typed):
    """Recursive alternatives neither hang nor hide typed sibling branches."""
    branches = [{"allOf": [{"$ref": "#"}]}]
    if typed:
        branches.append({"type": "string"})
    issues = lint(tmp_path, {"anyOf": branches})
    assert ("explicit-type" in rules(issues)) is not typed


@pytest.mark.parametrize(
    "branch",
    [
        {"properties": {"name": {"type": "string"}}},
        {"items": {"type": "string"}},
        {"$defs": {"unused": {"type": "string"}}},
        {"const": "text"},
    ],
)
def test_composed_type_does_not_use_unrelated_locations(tmp_path, branch):
    """Nested instance types and inferred types do not document this node's type."""
    issues = lint(tmp_path, {"anyOf": [branch]})
    assert any(issue.rule == "explicit-type" and issue.path == () for issue in issues)


@pytest.mark.parametrize("keyword", ["if", "then", "else", "not"])
def test_conditional_type_does_not_count(tmp_path, keyword):
    """Conditionals and negation do not supply a type for the definition."""
    write(tmp_path, string(), "base.schema.json")
    ref = {"$ref": "base.schema.json"}
    assert "explicit-type" in rules(lint(tmp_path, {keyword: ref}))


@pytest.mark.parametrize("missing", [False, True])
def test_reference_without_inherited_type_still_fails(tmp_path, missing):
    """Neither untyped cycles nor broken references satisfy the type requirement."""
    if not missing:
        write(tmp_path, {"$ref": "base.schema.json"}, "base.schema.json")
    issues = lint(tmp_path, {"maxLength": 5, "allOf": [{"$ref": "base.schema.json"}]})
    assert "explicit-type" in rules(issues)
    assert ("reference-resolution" in rules(issues)) == missing


def test_inherited_type_stays_at_the_correct_instance_location(tmp_path):
    """Parent reuse can supply property types without leaking types to siblings."""
    write(
        tmp_path,
        {"type": "object", "properties": {"name": string()}},
        "base.schema.json",
    )
    issues = lint(
        tmp_path,
        {
            "properties": {"name": {"maxLength": 5}, "otherName": {"maxLength": 5}},
            "allOf": [{"$ref": "base.schema.json"}],
        },
    )
    assert {issue.path for issue in issues if issue.rule == "explicit-type"} == {
        ("properties", "otherName")
    }


def test_type_union_order_and_generic_keywords(tmp_path):
    """Check union ordering and generic keywords are validated."""
    issues = lint(
        tmp_path,
        {
            "examples": ["abc"],
            "description": "D",
            "type": ["array", "string"],
            "title": "T",
        },
    )
    assert rules(issues) == {"keyword-order", "nonempty-values"}
    assert len([issue for issue in issues if issue.rule == "nonempty-values"]) == 3
    # Unpositioned keywords must not change recognized-keyword relative order.
    assert not lint(
        tmp_path, {"x-note": "Note", **string(), "x-extension": {"examples": []}}
    )


@pytest.mark.parametrize("kind", ["string", "array"])
@pytest.mark.parametrize("reuse", ["$ref", "allOf"])
@pytest.mark.parametrize("weaker_local_bounds", [False, True])
def test_type_bounds_can_be_inherited(tmp_path, kind, reuse, weaker_local_bounds):
    """Referenced bounds remain effective even with weaker local constraints."""
    bounds = (
        {"minLength": 2} if kind == "string" else {"minItems": 2, "uniqueItems": True}
    )
    write(tmp_path, {"type": kind, **bounds}, "base.schema.json")
    schema = {"type": kind}
    if reuse == "$ref":
        schema["$ref"] = "base.schema.json"
    else:
        schema["allOf"] = [{"$ref": "base.schema.json"}]
    if weaker_local_bounds:
        schema.update(
            {"minLength": 0}
            if kind == "string"
            else {"minItems": 0, "uniqueItems": False}
        )
    issues = lint(tmp_path, schema)
    assert "nonempty-values" not in rules(issues)
    if reuse == "$ref":
        assert "reference-style" in rules(issues)


def test_type_bounds_follow_transitive_relative_refs_and_cycles(tmp_path):
    """Nested references preserve URI scope and cyclic reuse terminates."""
    write(
        tmp_path, {"$ref": "second.schema.json#/$defs/text"}, "base/first.schema.json"
    )
    write(
        tmp_path,
        {"$defs": {"text": {"minLength": 1, "$ref": "first.schema.json"}}},
        "base/second.schema.json",
    )
    issues = lint(
        tmp_path, {"type": "string", "allOf": [{"$ref": "base/first.schema.json"}]}
    )
    assert "nonempty-values" not in rules(issues)
    assert "reference-resolution" not in rules(issues)


@pytest.mark.parametrize("transitive", [False, True])
def test_out_of_scope_type_bounds_do_not_count(tmp_path, transitive):
    """Standard constraints count only when the resolution boundary includes them."""
    write(
        tmp_path,
        {"type": "array", "minItems": 1, "uniqueItems": True},
        "standards/base.schema.json",
    )
    ref = "../standards/base.schema.json"
    if transitive:
        write(tmp_path, {"$ref": ref}, "schemas/base.schema.json")
        ref = "base.schema.json"
    selected = write(
        tmp_path, {"type": "array", "allOf": [{"$ref": ref}]}, "schemas/schema.json"
    )
    skipped = set()
    issues = lint_schemas([selected], root=tmp_path, skipped_references=skipped)
    assert {issue.path for issue in issues if issue.rule == "nonempty-values"} == {
        ("minItems",),
        ("uniqueItems",),
    }
    assert skipped
    wide_issues = lint_schemas([selected], root=tmp_path, resolution_root=Path("."))
    assert "nonempty-values" not in rules(wide_issues)


@pytest.mark.parametrize("keyword", ["anyOf", "oneOf", "if", "then", "else", "not"])
def test_conditional_type_bounds_do_not_count(tmp_path, keyword):
    """A constraint in a conditional or alternative is not guaranteed to apply."""
    write(tmp_path, {"minItems": 1, "uniqueItems": True}, "base.schema.json")
    ref = {"$ref": "base.schema.json"}
    condition = [ref, {}] if keyword in {"anyOf", "oneOf"} else ref
    issues = lint(tmp_path, {"type": "array", keyword: condition})
    assert {issue.path for issue in issues if issue.rule == "nonempty-values"} == {
        ("minItems",),
        ("uniqueItems",),
    }


def test_inherited_property_bounds_stay_at_the_correct_instance_location(tmp_path):
    """Parent inheritance can constrain a property, but unrelated bounds cannot."""
    write(
        tmp_path,
        {
            "minLength": 3,
            "minItems": 2,
            "uniqueItems": True,
            "properties": {"name": {"minLength": 1}},
        },
        "base.schema.json",
    )
    issues = lint(
        tmp_path,
        {
            "allOf": [{"$ref": "base.schema.json"}],
            "properties": {
                "name": {"type": "string"},
                "otherName": {"type": "string"},
                "members": {
                    "type": "array",
                    "items": {"minItems": 1, "uniqueItems": True},
                },
            },
        },
    )
    assert {issue.path for issue in issues if issue.rule == "nonempty-values"} == {
        ("properties", "otherName", "minLength"),
        ("properties", "members", "minItems"),
        ("properties", "members", "uniqueItems"),
    }


def test_inherited_bounds_must_meet_each_requirement(tmp_path):
    """An inherited minimum does not also imply unique array items."""
    write(tmp_path, {"minItems": 1, "uniqueItems": False}, "base.schema.json")
    issues = lint(tmp_path, {"type": "array", "allOf": [{"$ref": "base.schema.json"}]})
    assert {issue.path for issue in issues if issue.rule == "nonempty-values"} == {
        ("uniqueItems",)
    }


def test_broken_inheritance_does_not_hide_other_known_bounds(tmp_path):
    """Report broken references while retaining effective constraints elsewhere."""
    issues = lint(
        tmp_path,
        {
            "type": "string",
            "allOf": [
                {"$ref": "missing.schema.json"},
                {"minLength": 1},
            ],
        },
    )
    assert "reference-resolution" in rules(issues)
    assert "nonempty-values" not in rules(issues)


def test_dac_jurisdiction_inherits_relationship_array_bounds():
    """DAC jurisdiction need not repeat relationshipArray's minItems/uniqueItems."""
    issues = lint_schemas([ROOT / "schemas/entities/DAC/schema.json"], root=ROOT)
    assert not [
        issue
        for issue in issues
        if issue.rule == "nonempty-values"
        and issue.path[:2] == ("properties", "jurisdiction")
    ]


def test_cross_file_anchor_pointer_and_nested_id(tmp_path):
    """Check references resolve through anchors and JSON Pointers."""
    base = write(
        tmp_path,
        {
            "$schema": DIALECT,
            "$id": "https://example.test/base",
            "$defs": {
                "a/b~c": {"$anchor": "text", **string()},
                "nested": {
                    "$id": "nested/",
                    "$defs": {"inner": string()},
                    "$ref": "#/$defs/inner",
                },
            },
        },
        "base.schema.json",
    )
    for ref in (
        "base.schema.json#/$defs/a~1b~0c",
        "https://example.test/base#text",
        "base.schema.json#/$defs/nested",
    ):
        issues = lint(tmp_path, {"$ref": ref, "examples": [4, "valid"]})
        assert "reference-resolution" not in rules(issues), issues
        assert len([i for i in issues if i.rule == "example-validation"]) == 1
    assert base.exists()


def test_refs_in_unused_definitions_and_transitive_dependencies(tmp_path):
    """Check references are checked in unused and transitive definitions."""
    write(tmp_path, {"$defs": {"unused": {"$ref": "missing.json"}}}, "base.schema.json")
    issues = lint(tmp_path, {"$ref": "base.schema.json"})
    assert rules(issues) == {"reference-resolution"}
    assert "missing.json" in issues[0].message
    assert "base.schema.json" in issues[0].message


@pytest.mark.parametrize(
    "ref",
    [
        "missing.json",
        "#/$defs/missing",
        "#missingAnchor",
        "#/title",
        "https://unavailable.example/schema",
    ],
)
def test_broken_refs_fail_without_network(tmp_path, ref):
    """Check unresolved references are reported without network access."""
    issues = lint(tmp_path, {"title": "A", "$ref": ref})
    assert "reference-resolution" in rules(issues)


def test_recursive_schemas_and_nonproductive_cycles(tmp_path):
    """Check recursive schemas are handled without false reference errors."""
    schema = {
        "type": "object",
        "properties": {"child": {"$ref": "#"}},
        "examples": [{"child": {}}],
    }
    assert "reference-resolution" not in rules(lint(tmp_path, schema))
    issues = lint(tmp_path, {"$ref": "#", "examples": ["a"]})
    assert "example-validation" in rules(issues)


def test_graph_restriction_examples_inherit_id_and_label_constraints(tmp_path):
    """Check inherited graph restrictions validate their examples."""
    write(
        tmp_path,
        {
            "$id": "https://example.test/ontology",
            "type": "object",
            "properties": {
                "id": {"type": "string", "pattern": "^TERM:[0-9]+$"},
                "label": {"type": "string", "minLength": 1},
            },
        },
        "ontology.schema.json",
    )
    schema = {
        "allOf": [{"$ref": "https://example.test/ontology"}],
        "properties": {
            "id": {
                "graphRestriction": {"ontologies": ["test"]},
                "examples": ["bad", "TERM:12"],
            },
            "label": {
                "graphRestriction": {"queryFields": ["label"]},
                "examples": ["", "A label", 42],
            },
        },
    }
    issues = [
        issue for issue in lint(tmp_path, schema) if issue.rule == "example-validation"
    ]
    assert {issue.path for issue in issues} == {
        ("properties", "id", "examples", 0),
        ("properties", "label", "examples", 0),
        ("properties", "label", "examples", 2),
    }


def test_inheritance_preserves_original_relative_ref_scope(tmp_path):
    """Check inherited constraints keep their original reference scope."""
    write(
        tmp_path,
        {"$defs": {"text": {"type": "string", "minLength": 3}}},
        "base/types.schema.json",
    )
    write(
        tmp_path,
        {"properties": {"value": {"$ref": "types.schema.json#/$defs/text"}}},
        "base/schema.json",
    )
    schema = {
        "allOf": [{"$ref": "base/schema.json"}],
        "properties": {"value": {"examples": ["a", "abc"]}},
    }
    errors = [i for i in lint(tmp_path, schema) if i.rule == "example-validation"]
    assert len(errors) == 1 and "too short" in errors[0].message


def test_conditional_alternatives_and_formats_use_jsonschema(tmp_path):
    """Check conditional schemas and format examples are validated."""
    schema = {
        "type": "object",
        "if": {"required": ["flag"]},
        "then": {"required": ["value"]},
        "examples": [{}, {"flag": True}],
    }
    errors = [i for i in lint(tmp_path, schema) if i.rule == "example-validation"]
    assert len(errors) == 1 and errors[0].path == ("examples", 1)
    schema = string(format="date")
    schema["examples"] = ["2026-02-30", "2026-02-28"]
    assert (
        len([i for i in lint(tmp_path, schema) if i.rule == "example-validation"]) == 1
    )


def mapping():
    """Return a valid example mapping."""
    return {
        "predicate_id": "skos:exactMatch",
        "object_id": "schema:name",
        "mapping_justification": "semapv:ManualMappingCuration",
        "author_id": ["orcid:1234"],
        "creator_id": ["https://example.org/person"],
        "mapping_date": "2026-09-28",
    }


def test_sssom_implicit_subject_valid_values_and_optional_extensions(tmp_path):
    """Check valid mappings allow optional extension values."""
    entry = mapping()
    entry.update(confidence=0.8, comment="Curated")
    assert not lint(tmp_path, string(**{"meta:sssomMappings": [entry]}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("predicate_id", "exact match"),
        ("object_id", ""),
        ("mapping_justification", 3),
        ("author_id", "orcid:1234"),
        ("creator_id", []),
        ("reviewer_id", [""]),
        ("confidence", "high"),
        ("confidence", True),
        ("confidence", 1.1),
        ("mapping_date", "2026-02-30"),
        ("mapping_date", "20260928"),
        ("subject_match_field", [3]),
    ],
)
def test_sssom_invalid_values(tmp_path, field, value):
    """Check invalid mapping values are reported."""
    entry = mapping()
    entry[field] = value
    issues = lint(tmp_path, string(**{"meta:sssomMappings": [entry]}))
    assert any(i.rule == "sssom" and i.path[-1] == field for i in issues)


def test_sssom_required_fields_order_and_shape(tmp_path):
    """Check mapping fields are required, ordered, and object-shaped."""
    entries = [dict(reversed(list(mapping().items()))), {}, "wrong"]
    issues = lint(tmp_path, string(**{"meta:sssomMappings": entries}))
    assert rules(issues) == {"sssom", "sssom-order"}
    assert len([i for i in issues if "Missing mandatory" in i.message]) == 5


def test_bad_files_do_not_stop_other_checks(tmp_path):
    """Check one bad file does not stop the remaining checks."""
    duplicate = tmp_path / "duplicate.schema.json"
    duplicate.write_text('{"title":"a","title":"b"}')
    broken = tmp_path / "broken.schema.json"
    broken.write_text('{"type":')
    wrong = write(tmp_path, {"type": 12, "required": "a"}, "wrong.schema.json")
    scalar = write(tmp_path, 42, "scalar.schema.json")
    valid = write(tmp_path, {"type": "string"})
    issues = lint_schemas([duplicate, broken, wrong, scalar, valid], root=tmp_path)
    assert {i.file for i in issues} == {duplicate, broken, wrong, scalar, valid}
    assert {"json", "schema-structure", "documentation"} <= rules(issues)


def test_discovery_cli_exit_codes_and_no_modifications(tmp_path, capsys):
    """Check schema discovery, exit codes, and read-only behavior."""
    common = write(tmp_path, string(), "schemas/common/schema.json")
    profile = write(tmp_path, string(), "schemas/graph/test.schema.json")
    write(tmp_path, {}, "schemas/common/examples/data.json")
    write(tmp_path, {}, "standards/standard.schema.json")
    assert discover_schemas([tmp_path]) == [common, profile]
    assert discover_schemas([common, tmp_path / "schemas"]) == [common, profile]
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert main(["--root", str(tmp_path)]) == 0
    assert all(p.read_bytes() == content for p, content in before.items())
    common.write_text('{"type": "string"}')
    assert main([str(common), "--root", str(tmp_path), "-v"]) == 1
    report = capsys.readouterr().out
    assert all(f"[{rule}]" in report for rule in ("documentation", "nonempty-values"))
    assert main([str(tmp_path / "absent")]) == 2


def test_repository_collection_resolves_without_modification():
    """Check repository schemas resolve without changing files."""
    files = discover_schemas([ROOT / "schemas"])
    before = {path: path.read_bytes() for path in files}
    issues = lint_schemas(files, root=ROOT)
    assert "reference-resolution" not in rules(issues)
    assert all(path.read_bytes() == contents for path, contents in before.items())


@pytest.mark.parametrize(
    "format_name,bad,good",
    [
        ("uri", "not a URI", "https://example.org/resource"),
        ("date-time", "2026-09-28", "2026-09-28T14:30:00Z"),
    ],
)
def test_optional_format_dependencies_are_active(tmp_path, format_name, bad, good):
    """Check optional format validators are active when installed."""
    schema = string(format=format_name)
    schema["examples"] = [bad, good]
    errors = [
        issue for issue in lint(tmp_path, schema) if issue.rule == "example-validation"
    ]
    assert len(errors) == 1 and errors[0].path == ("examples", 0)


def test_local_dependency_with_arbitrary_filename_and_canonical_id(tmp_path):
    """Check local references can use canonical IDs and arbitrary filenames."""
    write(tmp_path, {"$id": "https://example.test/text", **string()}, "text.json")
    issues = lint(tmp_path, {"$ref": "text.json", "examples": ["abc", 4]})
    assert "reference-resolution" not in rules(issues)
    errors = [issue for issue in issues if issue.rule == "example-validation"]
    assert len(errors) == 1 and errors[0].path == ("examples", 1)


@pytest.mark.parametrize(
    "reference_kind", ["relative", "file-uri", "canonical", "symlink"]
)
def test_resolution_boundary_never_reads_excluded_artifacts(
    tmp_path, monkeypatch, reference_kind
):
    """Check excluded references never open files outside the boundary."""
    standard = write(tmp_path, {"$ref": "missing.json"}, "standards/base.schema.json")
    standard.write_text("not even valid JSON")
    schema_dir = tmp_path / "schemas"
    schema_dir.mkdir()
    if reference_kind == "relative":
        ref = "../standards/base.schema.json"
    elif reference_kind == "file-uri":
        ref = standard.as_uri()
    elif reference_kind == "canonical":
        ref = "https://raw.githubusercontent.com/EGA-archive/fega-metadata-schema/main/standards/base.schema.json"
    else:
        alias = schema_dir / "alias.schema.json"
        alias.symlink_to(standard)
        ref = "alias.schema.json"
    selected = write(
        tmp_path,
        {"title": "Inherited", "allOf": [{"$ref": ref}], "examples": ["abc"]},
        "schemas/schema.json",
    )
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        """Open a path only when it stays inside the allowed test area."""
        assert not path.resolve().is_relative_to(tmp_path / "standards"), path
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    skipped = set()
    assert not lint_schemas([selected], root=tmp_path, skipped_references=skipped)
    assert len(skipped) == 1


def test_wider_resolution_checks_standard_examples_and_transitive_refs(tmp_path):
    """Check a wider root validates external schemas and transitive references."""
    standard = write(
        tmp_path,
        {
            "type": "string",
            "minLength": 5,
            "$defs": {
                "unused": {"$ref": "missing.json"},
            },
        },
        "standards/base.schema.json",
    )
    selected = write(
        tmp_path,
        {
            "title": "Inherited",
            "allOf": [{"$ref": "../standards/base.schema.json"}],
            "examples": ["abc"],
        },
        "schemas/schema.json",
    )
    assert not lint_schemas([selected], root=tmp_path)
    issues = lint_schemas([selected], root=tmp_path, resolution_root=Path("."))
    assert rules(issues) == {"example-validation", "reference-resolution"}
    assert all(issue.file == selected for issue in issues)
    assert standard.exists()


def test_scope_is_generic_and_local_checks_survive_excluded_inheritance(tmp_path):
    """Check local rules run when inherited references are excluded."""
    write(tmp_path, {"type": "object"}, "schemas/other.schema.json")
    selected = write(
        tmp_path,
        {
            "allOf": [
                {"$ref": "../other.schema.json"},
                {"properties": {"id": {"type": "string"}}},
            ],
            "properties": {
                "id": {"examples": [42]},
                "broken": {"$ref": "missing.json"},
            },
        },
        "schemas/subset/schema.json",
    )
    skipped = set()
    issues = lint_schemas(
        [selected],
        root=tmp_path,
        resolution_root=Path("schemas/subset"),
        skipped_references=skipped,
    )
    assert len(skipped) == 1
    assert any(
        issue.rule == "example-validation"
        and issue.path == ("properties", "id", "examples", 0)
        for issue in issues
    )
    assert any(
        issue.rule == "reference-resolution" and "missing.json" in issue.message
        for issue in issues
    )


@pytest.mark.parametrize("keyword", ["not", "oneOf", "if"])
def test_excluded_refs_are_not_treated_as_boolean_schemas(tmp_path, keyword):
    """Check excluded references are not treated as boolean schemas."""
    write(tmp_path, False, "standards/base.schema.json")
    ref = {"$ref": "../standards/base.schema.json"}
    condition = [ref, string()] if keyword == "oneOf" else ref
    schema = ordered({**string(), keyword: condition})
    if keyword == "if":
        schema["then"] = False
        schema = ordered(schema)
    selected = write(tmp_path, schema, "schemas/schema.json")
    skipped = set()
    assert not lint_schemas([selected], root=tmp_path, skipped_references=skipped)
    assert skipped


def test_cli_scope_reporting_and_invalid_boundaries(tmp_path, capsys):
    """Check CLI reports skipped references and invalid boundaries."""
    write(tmp_path, {"type": "string", "minLength": 5}, "standards/base.schema.json")
    selected = write(
        tmp_path,
        {
            "title": "Inherited",
            "allOf": [{"$ref": "../standards/base.schema.json"}],
            "examples": ["abc"],
        },
        "schemas/schema.json",
    )
    assert main(["--root", str(tmp_path), "-v"]) == 0
    output = capsys.readouterr().out
    assert "Incomplete checks" in output and "may be incomplete" in output
    assert "Use -vv" in output and "[skipped-reference]" not in output
    assert main(["--root", str(tmp_path), "-vv"]) == 0
    assert "[skipped-reference]" in capsys.readouterr().out
    assert main(["--root", str(tmp_path), "--resolution-root", "."]) == 1
    assert "Skipped" not in capsys.readouterr().out
    assert (
        main(["--root", str(tmp_path), "--resolution-root", str(tmp_path / "schemas")])
        == 0
    )
    assert main(["--root", str(tmp_path), "--resolution-root", "missing"]) == 2
    assert (
        main([str(selected), "--root", str(tmp_path), "--resolution-root", "standards"])
        == 2
    )


@pytest.mark.parametrize("stale_install", [False, True])
def test_direct_script_uses_checkout_without_pythonpath(tmp_path, stale_install):
    """Check the script imports the current checkout."""
    import os
    import subprocess
    import sys

    schema = write(tmp_path, string(), "schemas/schema.json")
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    if stale_install:
        stale_package = tmp_path / "old-install" / "fega_tools"
        stale_package.mkdir(parents=True)
        (stale_package / "__init__.py").write_text(
            "raise AssertionError('Stale package must not be imported')\n"
        )
        env["PYTHONPATH"] = str(stale_package.parent)
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/py/lint_schemas.py"),
            str(schema),
            "--root",
            str(tmp_path),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Linted 1 schema(s): 0 violation(s)." in result.stdout


def test_help_without_site_packages_and_missing_dependency_message(tmp_path):
    """Check help works without packages and reports missing dependencies clearly."""
    import os
    import subprocess
    import sys

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    command = [sys.executable, "-S", str(ROOT / "scripts/py/lint_schemas.py")]
    help_result = subprocess.run(
        [*command, "--resolution-root", "schemas", "-v", "-h"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "--resolution-root" in help_result.stdout
    run_result = subprocess.run(
        command,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert run_result.returncode == 2
    assert "missing Python module" in run_result.stderr
    assert "pip install -r requirements.txt -e ." in run_result.stderr
    assert "Traceback" not in run_result.stderr


@pytest.mark.parametrize("missing_format", ["uri", "date-time"])
def test_missing_format_support_is_a_setup_error(
    tmp_path, monkeypatch, capsys, missing_format
):
    """Check missing format support is reported as a setup error."""
    from jsonschema import FormatChecker

    monkeypatch.delitem(FormatChecker.checkers, missing_format)
    selected = write(tmp_path, string(), "schemas/schema.json")
    with pytest.raises(
        RuntimeError, match=f"Missing JSON Schema format validators: {missing_format}"
    ):
        lint_schemas([selected], root=tmp_path)
    assert main(["--root", str(tmp_path)]) == 2
    output = capsys.readouterr().out
    assert missing_format in output
    assert "python -m pip install -r requirements.txt" in output
    assert "Linted" not in output
