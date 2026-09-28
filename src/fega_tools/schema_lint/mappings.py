"""FEGA's implicit-subject SSSOM profile, with lightweight offline value checks.

Slot types: https://mapping-commons.github.io/sssom/1.0/Mapping/
Confidence: https://mapping-commons.github.io/sssom/1.0/confidence/
No downloaded/vendored specification or closed list of ontology predicates.
"""

from __future__ import annotations

import datetime
import re

from .model import Node

# reviewer_id appears once: repeated JSON keys are rejected by the loader.
MAPPING_ORDER = (
    "predicate_id",
    "object_id",
    "mapping_justification",
    "subject_match_field",
    "object_match_field",
    "match_string",
    "author_id",
    "creator_id",
    "reviewer_id",
    "mapping_date",
)
REQUIRED_FIELDS = (
    "predicate_id",
    "object_id",
    "mapping_justification",
    "author_id",
    "creator_id",
)
IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*:[^\s:][^\s]*\Z")


def is_identifier(value) -> bool:
    """Return whether a value is a non-empty identifier."""
    return isinstance(value, str) and bool(IDENTIFIER.fullmatch(value))


def mappings(node: Node):
    """Check schema mapping entries for valid fields and values."""
    if "meta:sssomMapping" in node.schema:
        yield node.issue(
            "sssom",
            "Use the repository keyword meta:sssomMappings (plural).",
            "meta:sssomMapping",
        )
    if "meta:sssomMappings" not in node.schema:
        return
    entries = node.schema["meta:sssomMappings"]
    if not isinstance(entries, list) or not entries:
        yield node.issue(
            "sssom",
            "meta:sssomMappings must be a non-empty array.",
            "meta:sssomMappings",
        )
        return
    for index, mapping in enumerate(entries):
        path = ("meta:sssomMappings", index)
        if not isinstance(mapping, dict):
            yield node.issue("sssom", "Each mapping must be an object.", *path)
            continue
        for key in REQUIRED_FIELDS:
            if key not in mapping:
                yield node.issue(
                    "sssom", f"Missing mandatory mapping field {key}.", *path, key
                )
        actual = [key for key in mapping if key in MAPPING_ORDER]
        expected = sorted(actual, key=MAPPING_ORDER.index)
        if actual != expected:
            yield node.issue(
                "sssom-order",
                "Expected mapping field order: " + ", ".join(expected),
                *path,
            )
        for key, value in mapping.items():
            problem = None
            if key in {
                "predicate_id",
                "object_id",
                "mapping_justification",
                "subject_id",
            }:
                if not is_identifier(value):
                    problem = "must be a non-empty CURIE or absolute URI"
            elif key in {"author_id", "creator_id", "reviewer_id"}:
                if (
                    not isinstance(value, list)
                    or not value
                    or not all(is_identifier(item) for item in value)
                ):
                    problem = "must be a non-empty array of CURIEs or absolute URIs; omit unused optional fields"
            elif key in {"subject_match_field", "object_match_field", "match_string"}:
                # FEGA already uses scalar values as well as SSSOM multivalued slots.
                values = value if isinstance(value, list) else [value]
                if not values or not all(
                    isinstance(item, str) and item.strip() for item in values
                ):
                    problem = "must be a non-empty string or array of non-empty strings"
            elif key == "confidence":
                if type(value) not in (int, float) or not 0 <= value <= 1:
                    problem = "must be a number between 0 and 1"
            elif key in {"mapping_date", "publication_date", "review_date"}:
                try:
                    if not isinstance(value, str) or not re.fullmatch(
                        r"\d{4}-\d{2}-\d{2}", value
                    ):
                        raise ValueError
                    datetime.date.fromisoformat(value)
                except ValueError:
                    problem = "must be a real calendar date formatted YYYY-MM-DD"
            if problem:
                yield node.issue("sssom", f"{key} {problem}.", *path, key)
