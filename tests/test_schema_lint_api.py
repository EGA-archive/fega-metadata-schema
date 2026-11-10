"""Optional API example checks: scope, payload semantics and failure reporting."""

from copy import deepcopy
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from fega_tools import biovalidator
from fega_tools.schema_lint import lint_schemas
from fega_tools.schema_lint.api_examples import CUSTOM_KEYWORDS, bundle_constraints
from fega_tools.schema_lint.resolution import SchemaStore
from scripts.py.lint_schemas import main

URL = "http://validator.test/validate"


def write(root, schema, name="schema.json"):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(schema))
    return path


@pytest.fixture
def api(monkeypatch):
    """Record real client payloads while substituting only the HTTP transport."""
    calls = []
    monkeypatch.setattr(biovalidator, "assert_validator_reachable", lambda url: None)

    def post(document, url):
        assert url == URL
        calls.append(deepcopy(document))
        return []

    monkeypatch.setattr(biovalidator, "post_to_validator", post)
    return calls


@pytest.mark.parametrize("keyword", sorted(CUSTOM_KEYWORDS))
def test_api_checks_each_custom_keyword_and_caches_repeats(tmp_path, api, keyword):
    selected = write(
        tmp_path, {"type": "string", keyword: True, "examples": ["x", "x"]}
    )
    issues = lint_schemas([selected], root=tmp_path, validator_url=URL)
    assert not any(issue.rule == "example-validation" for issue in issues)
    assert len(api) == 1
    assert api[0]["data"] == "x"
    assert any(keyword in schema for schema in api[0]["schema"]["$defs"].values())


def test_api_ignores_standard_examples_and_annotation_data(tmp_path, api):
    selected = write(
        tmp_path,
        {
            "type": "object",
            "properties": {"graphRestriction": {"type": "string"}},
            "$defs": {"unused": {"isValidTerm": True}},
            "examples": [{"graphRestriction": "annotation"}],
        },
    )
    lint_schemas([selected], root=tmp_path, validator_url=URL)
    assert not api


def test_offline_custom_logic_is_incomplete_without_false_oneof_failure(
    tmp_path, monkeypatch
):
    def forbid(*args, **kwargs):
        pytest.fail("Offline mode made a network request")

    monkeypatch.setattr(biovalidator, "post_to_validator", forbid)
    monkeypatch.setattr(biovalidator, "assert_validator_reachable", forbid)
    selected = write(
        tmp_path,
        {"oneOf": [{"isValidTerm": True}, {"type": "string"}], "examples": ["x"]},
    )
    skipped = set()
    issues = lint_schemas([selected], root=tmp_path, skipped_examples=skipped)
    assert not any(issue.rule == "example-validation" for issue in issues)
    assert len(skipped) == 1
    assert "--check-api" in next(iter(skipped))


def test_api_preserves_full_composition_and_nested_custom_constraints(tmp_path, api):
    selected = write(
        tmp_path,
        {
            "type": "object",
            "properties": {
                "term": {"oneOf": [{"isValidTerm": True}, {"const": "unknown"}]}
            },
            "examples": [{"term": "unknown"}],
        },
    )
    lint_schemas([selected], root=tmp_path, validator_url=URL)
    assert len(api) == 1
    submitted = api[0]
    assert submitted["data"] == {"term": "unknown"}
    assert submitted["schema"]["$defs"]["s0"]["properties"]["term"]["oneOf"] == [
        {"isValidTerm": True},
        {"const": "unknown"},
    ]


def test_api_projects_inherited_id_and_label_and_preserves_references(tmp_path, api):
    write(
        tmp_path,
        {
            "$id": "https://example.test/base",
            "type": "object",
            "$defs": {
                "id": {"$anchor": "identifier", "type": "string", "pattern": "^TERM:"}
            },
            "properties": {
                "id": {"$ref": "#identifier"},
                "label": {"type": "string", "minLength": 1},
            },
        },
        "base.schema.json",
    )
    selected = write(
        tmp_path,
        {
            "allOf": [{"$ref": "base.schema.json"}],
            "properties": {
                "id": {
                    "graphRestriction": {"ontologies": ["test"]},
                    "examples": ["TERM:1"],
                },
                "label": {
                    "graphRestriction": {"queryFields": ["label"]},
                    "examples": ["Label"],
                },
            },
            "examples": [{"id": "TERM:1", "label": "Label"}],
        },
    )
    before = selected.read_bytes()
    lint_schemas([selected], root=tmp_path, validator_url=URL)
    assert len(api) == 3
    by_value = {json.dumps(call["data"]): call["schema"] for call in api}
    id_schema = by_value[json.dumps("TERM:1")]
    assert Draft202012Validator(id_schema).is_valid("TERM:1")
    assert not Draft202012Validator(id_schema).is_valid("wrong")
    label_schema = by_value[json.dumps("Label")]
    assert not Draft202012Validator(label_schema).is_valid("")
    assert selected.read_bytes() == before
    assert all("https://example.test" not in json.dumps(call) for call in api)


@pytest.mark.parametrize("wide", [False, True])
def test_api_never_resolves_outside_requested_boundary(tmp_path, api, wide):
    write(tmp_path, {"type": "string"}, "standards/base.schema.json")
    selected = write(
        tmp_path,
        {
            "isValidTerm": True,
            "$ref": "../standards/base.schema.json",
            "examples": ["x"],
        },
        "schemas/schema.json",
    )
    skipped = set()
    lint_schemas(
        [selected],
        root=tmp_path,
        validator_url=URL,
        resolution_root=Path(".") if wide else Path("schemas"),
        skipped_examples=skipped,
    )
    assert len(api) == int(wide)
    assert bool(skipped) is not wide


@pytest.mark.parametrize(
    "response", [[{"message": "unknown ontology term"}], {"unexpected": True}]
)
def test_api_reports_invalid_and_unexpected_responses(
    tmp_path, api, monkeypatch, response
):
    monkeypatch.setattr(
        biovalidator, "post_to_validator", lambda document, url: response
    )
    selected = write(tmp_path, {"isValidTerm": True, "examples": ["a", "b"]})
    issues = lint_schemas([selected], root=tmp_path, validator_url=URL)
    errors = [issue for issue in issues if issue.rule == "example-validation"]
    assert {issue.path for issue in errors} == {("examples", 0), ("examples", 1)}
    assert all("Biovalidator" in issue.message for issue in errors)


def test_api_request_failure_is_reported_and_other_examples_continue(
    tmp_path, api, monkeypatch
):
    import requests

    calls = []

    def post(document, url):
        calls.append(document["data"])
        if len(calls) == 1:
            raise requests.Timeout("timed out")
        return []

    monkeypatch.setattr(biovalidator, "post_to_validator", post)
    selected = write(tmp_path, {"isValidTerm": True, "examples": ["a", "b"]})
    issues = lint_schemas([selected], root=tmp_path, validator_url=URL)
    errors = [issue for issue in issues if issue.rule == "example-validation"]
    assert calls == ["a", "b"]
    assert len(errors) == 1
    assert "timed out" in errors[0].message


def test_bundle_handles_recursive_static_refs_and_nested_ids(tmp_path):
    selected = write(
        tmp_path,
        {
            "$id": "https://example.test/root",
            "$defs": {
                "node": {
                    "$id": "child",
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "isValidTerm": False},
                        "child": {"$ref": "child"},
                    },
                }
            },
            "$ref": "child",
        },
    )
    store = SchemaStore(tmp_path, [selected], tmp_path, set())
    bundle = bundle_constraints([store.node(selected).bound])
    validator = Draft202012Validator(bundle)
    assert validator.is_valid({"id": "a", "child": {"id": "b"}})
    assert not validator.is_valid({"child": {"id": 3}})


def test_dynamic_reference_scope_fails_explicitly(tmp_path, api):
    selected = write(
        tmp_path, {"$dynamicAnchor": "node", "isValidTerm": True, "examples": ["x"]}
    )
    issues = lint_schemas([selected], root=tmp_path, validator_url=URL)
    assert not api
    assert any("dynamic/recursive" in issue.message for issue in issues)


def test_old_dialect_reference_is_not_silently_reinterpreted(tmp_path, api):
    write(
        tmp_path,
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "definitions": {"term": {"type": "string", "isValidTerm": True}},
        },
        "old.schema.json",
    )
    selected = write(
        tmp_path, {"$ref": "old.schema.json#/definitions/term", "examples": ["x"]}
    )
    issues = lint_schemas([selected], root=tmp_path, validator_url=URL)
    assert not api
    assert any("draft 2020-12" in issue.message for issue in issues)


def test_cli_api_mode_and_endpoint_requirement(tmp_path, api, monkeypatch, capsys):
    selected = write(
        tmp_path, {"isValidTerm": True, "examples": ["x"]}, "schemas/schema.json"
    )
    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "--check-api",
                "--validator-url",
                URL,
                str(selected),
            ]
        )
        == 1
    )
    assert len(api) == 1
    assert URL in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        main(["--validator-url", URL])
    assert exc.value.code == 2

    def unreachable(url):
        raise RuntimeError("Cannot reach Biovalidator")

    monkeypatch.setattr(biovalidator, "assert_validator_reachable", unreachable)
    assert main(["--root", str(tmp_path), "--check-api", str(selected)]) == 2
    assert "Cannot reach Biovalidator" in capsys.readouterr().out
