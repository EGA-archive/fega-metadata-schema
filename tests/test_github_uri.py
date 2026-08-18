from __future__ import annotations

import pytest

from fega_tools.github_uri import (
    validate_replacements,
    parse_raw_github_uri,
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
        validate_replacements({"tag": ("main", "dev")})


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


def test_parse_raw_github_uri_separates_prefixed_ref_and_suffix() -> None:
    uri = parse_raw_github_uri(
        "https://raw.githubusercontent.com/owner/repo/refs/heads/main/"
        "schemas/x.json?download=1#anchor"
    )
    assert uri is not None
    assert uri.ref_prefix == "refs/heads/"
    assert uri.ref == "main"
    assert uri.path == "schemas/x.json"
    assert uri.suffix == "?download=1#anchor"
    assert uri.render(ref="v1.0.0", preserve_prefix=False) == (
        "https://raw.githubusercontent.com/owner/repo/v1.0.0/"
        "schemas/x.json?download=1#anchor"
    )
