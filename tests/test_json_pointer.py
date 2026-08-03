from __future__ import annotations

import pytest

from fega_tools.json_pointer import (
    _validate_replacements,
    patch_json_tree,
    rewrite_raw_github_uris,
)


OLD_RAW_PREFIX = (
    "https://raw.githubusercontent.com/"
    + "M-" + "casado/"
    + "fega-" + "metadata-" + "schema/main/"
)


def test_patch_json_tree_updates_raw_github_uri_segments() -> None:
    """Check that matching raw GitHub URLs are rewritten in JSON-LD keys."""
    uri = OLD_RAW_PREFIX + "schemas/entities/cohort/schema.json"
    document = {
        "$id": uri,
        "nested": {
            "$ref": uri,
            "description": f"See {uri} and {uri}.",
            "@context": "./context.jsonld",
        },
    }
    mappings: dict[str, str] = {}

    patched = patch_json_tree(
        document,
        replacements={"branch": ("main", "dev")},
        uri_mappings=mappings,
    )

    expected = uri.replace("/main/", "/dev/")
    assert patched["$id"] == expected
    assert patched["nested"]["$ref"] == expected
    assert patched["nested"]["description"] == f"See {expected} and {expected}."
    assert patched["nested"]["@context"] == "./context.jsonld"
    assert mappings[uri] == expected


def test_patch_json_tree_requires_all_segments_to_match_by_default() -> None:
    """Check that partial matches are ignored when all segments must match."""
    uri = OLD_RAW_PREFIX + "schema.json"

    patched = patch_json_tree(
        {"$ref": uri},
        replacements={
            "owner": ("other-owner", "new-owner"),
            "branch": ("main", "dev"),
        },
    )

    assert patched == {"$ref": uri}


def test_validate_replacements_rejects_unknown_segment() -> None:
    """Check that unsupported replacement segment names are rejected."""
    with pytest.raises(ValueError, match="Invalid segment"):
        _validate_replacements({"tag": ("main", "dev")})


def test_rewrite_raw_github_uris_preserves_refs_prefix_and_fragments() -> None:
    """Check that URI rewriting preserves prefixes and fragments."""
    uri = (
        "https://raw.githubusercontent.com/"
        "M-" + "casado/fega-" + "metadata-" + "schema/refs/heads/main/"
        "schemas/entities/cohort/schema.json#/properties/id"
    )
    mappings: dict[str, str] = {}

    rewritten, count = rewrite_raw_github_uris(
        f"prefix {uri} suffix",
        {
            "owner": ("M-casado", "EGA-archive"),
            "repo": ("fega-metadata-schema", "fega-metadata-schema"),
            "branch": ("main", "dev"),
        },
        uri_mappings=mappings,
    )

    expected = uri.replace("M-casado", "EGA-archive").replace("/main/", "/dev/")
    assert rewritten == f"prefix {expected} suffix"
    assert count == 1
    assert mappings[uri.split("/schemas/")[0] + "/"] == expected.split("/schemas/")[0] + "/"
