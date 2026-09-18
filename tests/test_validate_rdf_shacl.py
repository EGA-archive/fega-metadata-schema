from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = REPO_ROOT / "scripts" / "py"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from validate_rdf_shacl import root_has_required_type


CONTEXT = {
    "dcat": "http://www.w3.org/ns/dcat#",
    "sameAs": {"@id": "http://www.w3.org/2002/07/owl#sameAs", "@type": "@id"},
    "related": {"@id": "http://example.org/related", "@type": "@id"},
}

SHAPE_PATHS = [
    REPO_ROOT / "standards/rdf/dcatap/release-3.0.0/shapes.ttl",
    REPO_ROOT / "standards/rdf/healthdcat-ap/release-6.0.0/shacl/non-public-shapes-v6.ttl",
]
REQUIRED_ROOT_TYPE = "http://www.w3.org/ns/dcat#Dataset"


def test_root_type_survives_self_reference() -> None:
    """Check that a root type remains valid when the document references itself."""
    document = {
        "@context": CONTEXT,
        "@id": "http://example.org/dataset/1",
        "@type": "dcat:Dataset",
        "sameAs": {"@id": "http://example.org/dataset/1", "@type": "ega:Dataset"},
    }

    assert root_has_required_type(document, "http://www.w3.org/ns/dcat#Dataset")


def test_nested_type_does_not_satisfy_root_requirement() -> None:
    """Check that a nested type cannot satisfy the root requirement."""
    document = {
        "@context": CONTEXT,
        "@id": "http://example.org/dataset/1",
        "@type": "http://example.org/OtherRootType",
        "related": {
            "@id": "http://example.org/dataset/2",
            "@type": "dcat:Dataset",
        },
    }

    assert not root_has_required_type(document, "http://www.w3.org/ns/dcat#Dataset")


def test_invalid_jsonld_context_is_reported() -> None:
    """Check that an invalid JSON-LD context raises a clear error."""
    with pytest.raises(ValueError, match="Failed to expand JSON-LD"):
        root_has_required_type({"@context": "not a valid context reference"}, "dcat:Dataset")


def test_dataset_examples_validate_against_composed_shapes():
    from validate_rdf_shacl import validate_rdf_shacl

    summary = validate_rdf_shacl(
        REPO_ROOT / "schemas/entities",
        "dataset",
        SHAPE_PATHS,
        required_root_type=REQUIRED_ROOT_TYPE,
    )
    assert summary["passed"], summary
    assert len(summary["shape_files"]) == 2
    assert summary["total_valid_files"] == 2
    assert summary["total_invalid_files"] == 6
    assert summary["category_totals"]["valid"]["validation_passed"] == 2
    assert summary["category_totals"]["invalid"]["validation_failed"] == 6


def test_missing_shapes_and_malformed_shapes_fail_configuration(tmp_path):
    from validate_rdf_shacl import load_shapes

    good = tmp_path / "good.ttl"
    good.write_text("@prefix sh: <http://www.w3.org/ns/shacl#> . <urn:shape> a sh:NodeShape .")
    with pytest.raises(FileNotFoundError):
        load_shapes([good, tmp_path / "missing.ttl"])
    bad = tmp_path / "bad.ttl"
    bad.write_text("not valid Turtle")
    with pytest.raises(RuntimeError):
        load_shapes([bad])


def test_meta_shacl_rejects_invalid_constraint_definition(tmp_path):
    from validate_rdf_shacl import load_shapes

    path = tmp_path / "invalid-shape.ttl"
    path.write_text('''@prefix sh: <http://www.w3.org/ns/shacl#> .
      <urn:shape> a sh:NodeShape; sh:property [sh:path <urn:title>; sh:minCount "one"] .''')
    with pytest.raises(RuntimeError, match="MetaSHACL"):
        load_shapes([path])


def test_absent_root_target_cannot_count_as_a_shacl_negative():
    from validate_rdf_shacl import load_shapes, validate_file_shacl
    from fega_tools.jsonld_utils import build_id_to_path_map

    _, _, shapes, _ = load_shapes(SHAPE_PATHS)
    path = REPO_ROOT / "schemas/entities/dataset/examples/valid/dataset-valid-minimal-non-public.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["data"]["@type"] = "ega:dataset"
    result = validate_file_shacl(
        path,
        shapes,
        build_id_to_path_map(REPO_ROOT / "schemas"),
        REQUIRED_ROOT_TYPE,
        document,
    )
    assert result["status"] == "validation_failed"
    assert not result.get("violations")
    assert "Required root RDF type not found" in result["errors"][0]


@pytest.mark.parametrize("severity", ["Warning", "Info", "Violation"])
def test_shacl_severity_is_preserved_and_blocks_conformance(severity):
    from rdflib import Graph
    from fega_tools.rdf_utils import validate_against_shacl

    shapes = Graph().parse(data=f'''@prefix sh: <http://www.w3.org/ns/shacl#> .
      <urn:shape> a sh:NodeShape; sh:targetNode <urn:dataset>;
      sh:property [sh:path <urn:title>; sh:minCount 1; sh:severity sh:{severity}] .''', format="turtle")
    conforms, _, report = validate_against_shacl(Graph(), shapes)
    assert not conforms
    assert report["violations"][0]["severity"] == "http://www.w3.org/ns/shacl#" + severity
