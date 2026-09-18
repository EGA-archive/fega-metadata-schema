import json
from pathlib import Path
import sys

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/py"))
import validate_examples as suite
from fega_tools import biovalidator
from fega_tools.validation_common import aggregate_category_summaries, summarize_validation_results


@pytest.mark.parametrize("status,paths,passed", [
    ("validation_failed", ["/title"], True),
    ("validation_failed", ["/unrelated"], False),
    ("validation_passed", [], False),
    ("request_error", ["/title"], False),
    ("script_error", ["/title"], False),
    ("unknown_response", ["/title"], False),
])
def test_negative_requires_the_intended_path_at_every_summary_level(tmp_path, monkeypatch, status, paths, passed):
    examples = tmp_path / "dataset/examples"
    (examples / "invalid").mkdir(parents=True)
    (examples / "invalid/missing-title.json").write_text('{}')
    (examples / "expectations.json").write_text(json.dumps({"missing-title.json": ["/title"]}))
    monkeypatch.setattr(suite, "validate_file", lambda path, url: {
        "file": str(path), "status": status, "errors": [{"dataPath": p} for p in paths]
    })
    summary = suite.summarize_category(examples.parent, "invalid", "unused", [])
    assert summary["passed"] is passed
    aggregate = aggregate_category_summaries([{"categories": {"invalid": summary}}], ("invalid",))
    assert aggregate["category_totals"]["invalid"]["passed"] is passed


def test_new_negative_without_expectation_fails_coverage(tmp_path):
    examples = tmp_path / "dataset/examples"
    (examples / "invalid").mkdir(parents=True)
    (examples / "invalid/new.json").write_text('{}')
    (examples / "expectations.json").write_text('{}')
    with pytest.raises(ValueError, match="coverage"):
        suite.summarize_category(examples.parent, "invalid", "unused", [])


@pytest.mark.parametrize("status", [429, 500])
def test_http_failures_never_count_as_expected_rejections(monkeypatch, status):
    response = requests.Response()
    response.status_code = status
    response._content = b'[{"dataPath":"/title","errors":["missing"]}]'
    monkeypatch.setattr(requests, "post", lambda *a, **kw: response)
    result = biovalidator.validate_document({}, "http://unused")
    assert result["status"] == "request_error"
    assert not summarize_validation_results([result], "validation_failed")["passed"]


def test_schema_expectation_inventory_matches_all_negative_examples():
    root = Path(__file__).resolve().parents[1]
    for folder in (root / "schemas").glob("**/examples/invalid"):
        expectations = json.loads((folder.parent / "expectations.json").read_text())
        assert set(expectations) == {p.name for p in folder.glob("*.json")}


def test_ega_schema_rejects_only_removed_in_submission():
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    root = Path(__file__).resolve().parents[1]
    prefix = "https://raw.githubusercontent.com/EGA-archive/fega-metadata-schema/main/"

    def retrieve(uri):
        assert uri.startswith(prefix), uri
        path = root / uri.removeprefix(prefix)
        return Resource.from_contents(json.loads(path.read_text()), default_specification=DRAFT202012)

    path = root / "schemas/entities/dataset/examples/valid/dataset-valid-minimal-non-public.json"
    document = json.loads(path.read_text())
    validator = Draft202012Validator(document["schema"], registry=Registry(retrieve=retrieve))
    assert validator.is_valid(document["data"])
    del document["data"]["inSubmission"]
    errors = list(validator.iter_errors(document["data"]))
    assert len(errors) == 1
    assert errors[0].validator == "required" and "inSubmission" in errors[0].validator_value
