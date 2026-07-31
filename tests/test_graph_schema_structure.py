from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_graph_schema() -> dict:
    return json.loads(
        (REPO_ROOT / "schemas/graph/schema.json").read_text(encoding="utf-8")
    )


def test_graph_entity_dispatch_uses_type_declarations() -> None:
    schema = load_graph_schema()
    defs = schema["$defs"]

    assert "oneOf" not in defs["supportedEntityType"]
    supported_type_refs = defs["supportedEgaTypeValue"]["anyOf"]
    assert len(supported_type_refs) == 11
    assert all(
        branch["$ref"].startswith(
            "../common/schema.json#/$defs/relationshipItemRestriction"
        )
        and branch["$ref"].endswith("/properties/@type/anyOf/0")
        for branch in supported_type_refs
    )
    assert defs["routableEntityType"]["properties"]["@type"]["anyOf"][1][
        "uniqueItems"
    ] is True

    entity_names = {
        "Biomaterial",
        "Cohort",
        "DAC",
        "Datafile",
        "Dataset",
        "Policy",
        "Process",
        "Protocol",
        "ProtocolCollection",
        "Study",
        "Submission",
    }
    for entity_name in entity_names:
        route_refs = {
            branch["$ref"]
            for branch in defs[f"entitySchema{entity_name}"]["if"]["allOf"]
        }
        assert f"#/$defs/declares{entity_name}" in route_refs
        assert "#/$defs/routableEntityType" in route_refs


def test_graph_profile_requirements_use_declaration_predicates() -> None:
    defs = load_graph_schema()["$defs"]

    for requirement, entity_name in (
        ("hasBiomaterial", "Biomaterial"),
        ("hasProcess", "Process"),
        ("hasDataset", "Dataset"),
        ("hasDatafile", "Datafile"),
    ):
        contains_ref = defs[requirement]["properties"]["@graph"]["contains"]["$ref"]
        assert contains_ref == f"#/$defs/declares{entity_name}"

    for requirement, entity_name in (
        ("hasOrganismBiomaterial", "Biomaterial"),
        ("hasLabProtocol", "Protocol"),
    ):
        contains = defs[requirement]["properties"]["@graph"]["contains"]
        assert contains["allOf"][0]["$ref"] == f"#/$defs/declares{entity_name}"
