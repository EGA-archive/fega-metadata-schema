from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = REPO_ROOT / "scripts" / "py"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from validate_jsonld_frames import (
    _select_graph_document,
    _select_primary_entity,
    resolve_file_entity,
)
from fega_tools.jsonld_utils import build_id_to_path_map

import pytest
from pyld import jsonld
import validate_jsonld_frames as frames


def _payload_state(route, data, *, actual_context=None, graph=False):
    context = {"link": "https://example.org/link", "other": "https://example.org/other",
               "items": {"@id": "https://example.org/items", "@container": "@list"}}
    canonical = jsonld.normalize({"@context": context, **data}, {"algorithm": "URDNA2015", "format": "application/n-quads"})

    def loader(url, options=None):
        return {"contentType": "application/ld+json", "contextUrl": None,
                "documentUrl": url, "document": {"@context": actual_context or context}}

    return {"ready": True, "path": Path("case.json"), "schema_ref": "https://example.org/schema",
            "inline_context": context, "document_loader": loader,
            "is_graph_document": graph, "original_canonical": canonical,
            "routes": {route: {"result": None, "primary": data, "framed": data}}}


@pytest.mark.parametrize("route", frames.ROUTES)
@pytest.mark.parametrize("properties", [
    {"link": {"@id": "_:anonymous"}},
    {"link": {"@id": "_:shared"}, "other": {"@id": "_:shared"}},
    {"items": []},
    {"link": {"@value": "text", "@language": "en"}},
])
def test_final_payload_preserves_anonymous_links_lists_and_literals(monkeypatch, route, properties):
    state = _payload_state(route, {"@id": "https://example.org/root", **properties})
    sent = []
    monkeypatch.setattr(frames, "post_to_validator", lambda doc, url: sent.append(doc) or [])
    frames._validate_route_output(state, route, "unused", False)
    assert state["routes"][route]["result"]["status"] == "validation_passed"
    assert len(sent) == 1
    normalized = jsonld.normalize(sent[0]["data"], {
        "algorithm": "URDNA2015", "format": "application/n-quads", "documentLoader": state["document_loader"]})
    assert normalized == state["original_canonical"]


@pytest.mark.parametrize("route", frames.ROUTES)
def test_final_payload_uses_its_actual_context(monkeypatch, route):
    state = _payload_state(route, {"@id": "https://example.org/root", "link": "value"},
                           actual_context={"link": "https://example.org/wrong"})
    monkeypatch.setattr(frames, "post_to_validator", lambda *args: pytest.fail("Corrupt RDF reached schema validation"))
    frames._validate_route_output(state, route, "unused", False)
    assert state["routes"][route]["result"]["failed_stage"] == "payload_rdf_equivalence"


@pytest.mark.parametrize("route", frames.ROUTES)
def test_corrupt_graph_projection_fails_even_when_raw_frame_matches(monkeypatch, route):
    data = {"@id": "https://example.org/graph", "@graph": [{"@id": "https://example.org/node", "link": "kept"}]}
    state = _payload_state(route, data, graph=True)
    state["routes"][route]["primary"] = {"@id": data["@id"], "@graph": []}
    monkeypatch.setattr(frames, "post_to_validator", lambda *args: pytest.fail("Corrupt projection reached schema validation"))
    frames._validate_route_output(state, route, "unused", False)
    assert state["routes"][route]["result"]["failed_stage"] == "payload_rdf_equivalence"


def test_primary_selection_matches_equivalent_compact_and_absolute_ids() -> None:
    """Framing must compare node identifiers by JSON-LD value, not spelling."""
    # The test gives the frame output the short CURIE form, but tells the selector
    # the original used the full URL. It passes only if the selector expands both 
    # forms and identifies them as the same Dataset.
    context = {"ega": "https://identifiers.org/ega:"}
    framed = {
        "@context": context,
        "@id": "ega:EGAD00000000001",
        "@type": "ega:dataset",
    }

    primary, error = _select_primary_entity(
        framed,
        "https://identifiers.org/ega:EGAD00000000001",
        ["ega:dataset"],
        context,
    )

    assert error is None
    assert primary == {
        "@id": "ega:EGAD00000000001",
        "@type": "ega:dataset",
    }


def test_graph_selection_does_not_restore_missing_source_fields() -> None:
    """Graph projection must contain only fields present in the framed output."""
    framed = {
        "@graph": [
            {
                "@id": "ega:EGAF00000000001",
                "@type": "ega:datafile",
                "fileName": "reads.fastq.gz",
            }
        ]
    }

    selected, error = _select_graph_document(framed, {"ega:EGAF00000000001"})

    assert error is None
    assert selected is not None
    assert selected["@graph"] == framed["@graph"]
    assert "checksums" not in selected["@graph"][0]


def test_graph_selection_derives_inverse_links_from_framed_nodes() -> None:
    """Inverse schema properties are rebuilt from framed RDF nodes, not input."""
    framed = {
        "@graph": [
            {
                "@id": "ega:EGAC00000000001",
                "@type": "ega:DAC",
                "governs": [{"@id": "ega:EGAP00000000001"}],
            },
            {
                "@type": "org:Membership",
                "org:organization": {"@id": "ega:EGAC00000000001"},
                "org:member": {
                    "@id": "ega:EGAW00000000001",
                    "foaf:name": "Jane Doe",
                },
                "org:role": [{"@id": "https://example.org/base/main_contact"}],
            },
            {
                "@id": "ega:EGAP00000000001",
                "@type": "ega:policy",
            },
        ]
    }

    selected, error = _select_graph_document(
        framed, {"ega:EGAC00000000001", "ega:EGAP00000000001"}
    )

    assert error is None
    assert selected is not None
    by_id = {item["@id"]: item for item in selected["@graph"]}
    assert by_id["ega:EGAC00000000001"]["components"] == [
        {
            "@type": "org:Membership",
            "agent": {"@id": "ega:EGAW00000000001", "name": "Jane Doe"},
            "roles": ["main_contact"],
        }
    ]
    assert by_id["ega:EGAP00000000001"]["governedBy"] == [
        {"@id": "ega:EGAC00000000001", "@type": "ega:DAC"}
    ]


def test_profile_example_resolves_base_graph_frame() -> None:
    """A profile example must reuse the graph frame for single-file debugging."""
    id_map = build_id_to_path_map(REPO_ROOT)
    example = REPO_ROOT / "schemas/graph/examples/valid/graph-valid-dataset-datafile.json"

    entity_dir, frame_path = resolve_file_entity(example, REPO_ROOT / "schemas", id_map)

    assert entity_dir == REPO_ROOT / "schemas/graph/profiles"
    assert frame_path == REPO_ROOT / "schemas/graph/frame.jsonld"
