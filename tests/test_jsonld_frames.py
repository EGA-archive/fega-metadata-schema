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
