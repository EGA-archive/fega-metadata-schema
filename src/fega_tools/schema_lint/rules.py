"""Small, independent authoring rules. Their execution list is in __init__.py."""

from __future__ import annotations

import re

from fega_tools.jsonld_utils import JSONLD_KEYWORDS
from .model import DEFINITION_ROLES, Node

# Unknown/extension keywords have no imposed position. Do not infer requirements
# from this list. In particular, $defs and $comment were not assigned positions.
KEYWORD_ORDER = (
    "$schema",
    "$id",
    "$anchor",
    "$dynamicAnchor",
    "meta:version",
    "$async",
    "type",
    "title",
    "description",
    "@context",
    "$ref",
    "$dynamicRef",
    "meta:sssomMappings",
    "format",
    "minLength",
    "maxLength",
    "pattern",
    "contentEncoding",
    "contentMediaType",
    "contentSchema",
    "const",
    "default",
    "minimum",
    "exclusiveMinimum",
    "maximum",
    "exclusiveMaximum",
    "multipleOf",
    "minProperties",
    "maxProperties",
    "required",
    "dependentRequired",
    "unevaluatedProperties",
    "additionalProperties",
    "patternProperties",
    "propertyNames",
    "dependentSchemas",
    "properties",
    "enum",
    "meta:enum",
    "uniqueItems",
    "minItems",
    "maxItems",
    "prefixItems",
    "unevaluatedItems",
    "items",
    "minContains",
    "maxContains",
    "contains",
    "graphRestriction",
    "isChildTermOf",
    "isValidTerm",
    "isValidTaxonomy",
    "isValidIdentifier",
    "allOf",
    "anyOf",
    "oneOf",
    "not",
    "if",
    "then",
    "else",
    "readOnly",
    "writeOnly",
    "deprecate",
    "examples",
)
KEYWORD_RANK = {key: index for index, key in enumerate(KEYWORD_ORDER)}
ANNOTATIONS = {
    "$schema",
    "$id",
    "$anchor",
    "$dynamicAnchor",
    "$comment",
    "title",
    "description",
    "default",
    "examples",
    "readOnly",
    "writeOnly",
    "deprecated",
    "deprecate",
    "@context",
    "meta:version",
    "meta:enum",
    "meta:sssomMappings",
    "$async",
}


def annotation_only_reference(schema: dict) -> bool:
    """Documentation can come from a reference or a pure allOf reuse wrapper."""
    if "$ref" in schema or "$dynamicRef" in schema:
        return not (schema.keys() - ANNOTATIONS - {"$ref", "$dynamicRef"})
    branches = schema.get("allOf")
    return (
        isinstance(branches, list)
        and bool(branches)
        and not (schema.keys() - ANNOTATIONS - {"allOf"})
        and all(
            isinstance(branch, dict) and annotation_only_reference(branch)
            for branch in branches
        )
    )


def documentation(node: Node):
    schema = node.schema
    reuse = annotation_only_reference(schema)
    independent = (
        node.role in DEFINITION_ROLES
        or "type" in schema
        or (("$ref" in schema or "$dynamicRef" in schema) and not reuse)
    )
    if independent and not reuse:
        if "type" not in schema:
            yield node.issue(
                "explicit-type", "Independent definitions must declare type."
            )
        for key in ("title", "description", "examples"):
            if key not in schema:
                yield node.issue(
                    "documentation",
                    f"Independent definitions need non-empty {key}.",
                    key,
                )
    # Annotations that are supplied must be useful, including on reuse nodes.
    for key in ("title", "description"):
        if key in schema and (
            not isinstance(schema[key], str) or not schema[key].strip()
        ):
            yield node.issue("documentation", f"{key} must be a non-empty string.", key)
    if "examples" in schema and (
        not isinstance(schema["examples"], list) or not schema["examples"]
    ):
        yield node.issue(
            "documentation", "examples must be a non-empty array.", "examples"
        )


def reference_style(node: Node):
    schema = node.schema
    if "$ref" in schema and len(schema) > 1:
        # A documented allOf edge is the explicit inheritance representation.
        documented_edge = node.role == "allOf" and annotation_only_reference(schema)
        if not documented_edge:
            yield node.issue(
                "reference-style",
                "Direct $ref must stand alone; put documented inheritance in allOf.",
            )
    branches = schema.get("allOf")
    if isinstance(branches, list) and len(branches) == 1 and set(schema) == {"allOf"}:
        branch = branches[0]
        if isinstance(branch, dict) and set(branch) == {"$ref"}:
            yield node.issue(
                "reference-style",
                "Undocumented single-reference allOf must use a direct $ref.",
            )


def keyword_order(node: Node):
    actual = [key for key in node.schema if key in KEYWORD_RANK]
    expected = sorted(actual, key=KEYWORD_RANK.__getitem__)
    if actual != expected:
        yield node.issue(
            "keyword-order", "Expected keyword order: " + ", ".join(expected)
        )


def property_names(node: Node):
    properties = node.schema.get("properties", {})
    if not isinstance(properties, dict):
        return
    for name in properties:
        if name not in JSONLD_KEYWORDS and not re.fullmatch(r"[a-z][a-zA-Z0-9]*", name):
            yield node.issue(
                "property-name",
                f"Property {name!r} must use camelCase (JSON-LD keywords are exempt).",
                "properties",
                name,
            )


def nonempty_values(node: Node):
    declared = node.schema.get("type", [])
    types = declared if isinstance(declared, list) else [declared]
    for kind, keyword in (("string", "minLength"), ("array", "minItems")):
        if kind in types:
            value = node.schema.get(keyword)
            if type(value) is not int or value < 1:
                yield node.issue(
                    "nonempty-values",
                    f"An explicit {kind} type needs explicit {keyword} >= 1.",
                    keyword,
                )
    if "array" in types and node.schema.get("uniqueItems") is not True:
        yield node.issue(
            "nonempty-values",
            "An explicit array type needs uniqueItems: true.",
            "uniqueItems",
        )
