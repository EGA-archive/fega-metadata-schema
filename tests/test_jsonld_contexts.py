from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_ROOT = REPO_ROOT / "scripts" / "py"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

from validate_jsonld_contexts import _context_url_is_acceptable, validate_file_jsonld
from fega_tools.jsonld_utils import build_id_to_path_map


def test_profile_context_resolves_to_the_base_graph_context() -> None:
    """Profiles may declare the base graph context instead of a sibling file."""
    id_map = build_id_to_path_map(REPO_ROOT)
    profile_ref = (
        "https://raw.githubusercontent.com/EGA-archive/fega-metadata-schema/dev/"
        "schemas/graph/profiles/dataset-and-datafile.schema.json"
    )
    graph_context = (
        "https://raw.githubusercontent.com/EGA-archive/fega-metadata-schema/dev/"
        "schemas/graph/context.jsonld"
    )
    example = REPO_ROOT / "schemas/graph/examples/valid/graph-valid-dataset-datafile.json"

    assert _context_url_is_acceptable(graph_context, profile_ref, example, id_map)


def test_flat_profiles_and_graph_requirement_defs_are_addressable() -> None:
    """Flat profile files resolve, while requirements remain graph definitions."""
    id_map = build_id_to_path_map(REPO_ROOT)
    profile_ref = (
        "https://raw.githubusercontent.com/EGA-archive/fega-metadata-schema/dev/"
        "schemas/graph/profiles/organism-lab-data.schema.json"
    )
    graph_ref = (
        "https://raw.githubusercontent.com/EGA-archive/fega-metadata-schema/dev/"
        "schemas/graph/schema.json"
    )

    assert id_map[profile_ref] == REPO_ROOT / "schemas/graph/profiles/organism-lab-data.schema.json"
    assert id_map[graph_ref] == REPO_ROOT / "schemas/graph/schema.json"

    graph_schema = json.loads(
        (REPO_ROOT / "schemas/graph/schema.json").read_text(encoding="utf-8")
    )
    for requirement in (
        "hasBiomaterial",
        "hasProcess",
        "hasDataset",
        "hasDatafile",
        "hasOrganismBiomaterial",
        "hasLabProtocol",
    ):
        assert requirement in graph_schema["$defs"]


def test_named_graph_context_check_counts_named_graph_triples() -> None:
    """The graph smoke test must see the entity triples inside @graph."""
    id_map = build_id_to_path_map(REPO_ROOT)
    example = REPO_ROOT / "schemas/graph/examples/valid/graph-valid-minimal.json"

    result = validate_file_jsonld(example, id_map)

    assert result["status"] == "validation_passed"
    assert result["n_triples"] > 1
    assert result["n_type_triples"] > 1
