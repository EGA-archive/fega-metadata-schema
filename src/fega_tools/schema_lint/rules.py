"""Small, independent authoring rules. Their execution list is in __init__.py."""

from __future__ import annotations

import re

from fega_tools.jsonld_utils import JSONLD_KEYWORDS
from .inheritance import conjunction, effective_constraints, has_declared_type
from .model import DEFINITION_ROLES, Node

# Unknown/extension keywords have no imposed position. Do not infer requirements
# from this list.
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
    "allOf",
    "anyOf",
    "oneOf",
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
    "not",
    "if",
    "then",
    "else",
    "readOnly",
    "writeOnly",
    "deprecate",
    "examples",
    "$comment"
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
    """Check that independent schemas contain useful documentation."""
    schema = node.schema
    reuse = annotation_only_reference(schema)
    independent = (
        node.role in DEFINITION_ROLES
        or "type" in schema
        or (("$ref" in schema or "$dynamicRef" in schema) and not reuse)
    )
    if independent and not reuse:
        if "type" not in schema:
            try:
                documented_type = any(
                    has_declared_type(bound) for bound in effective_constraints(node)
                )
            except (ValueError, TypeError, re.error) as exc:
                yield node.issue(
                    "explicit-type", f"Cannot inspect composed type: {exc}"
                )
            else:
                if not documented_type:
                    yield node.issue(
                        "explicit-type",
                        "Independent definitions must declare type locally or in an allOf/anyOf/oneOf branch or in-scope reference.",
                    )
        # Entity roots have standalone examples/ suites; nested definitions still
        # document their own examples, as do roots of other schema files.
        entity_root = not node.path and node.file.match(
            "schemas/entities/*/schema.json"
        )
        required = ("title",) if entity_root else ("title", "examples")
        for key in required:
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
    """Check that references use the repository's preferred form."""
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
    """Check that recognized schema keywords follow the expected order."""
    actual = [key for key in node.schema if key in KEYWORD_RANK]
    expected = sorted(actual, key=KEYWORD_RANK.__getitem__)
    if actual != expected:
        yield node.issue(
            "keyword-order", "Expected keyword order: " + ", ".join(expected)
        )


def property_names(node: Node):
    """Check that property names use the repository's naming style."""
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
    """Require string/array bounds locally or through in-scope inheritance."""
    declared = node.schema.get("type", [])
    types = declared if isinstance(declared, list) else [declared]
    if "string" not in types and "array" not in types:
        return
    schemas = []
    try:
        for bound in effective_constraints(node):
            schemas.extend(
                part.schema
                for part in conjunction(bound)
                if isinstance(part.schema, dict)
            )
    except (ValueError, TypeError, re.error) as exc:
        yield node.issue(
            "nonempty-values", f"Cannot inspect inherited constraints: {exc}"
        )
    for kind, keyword in (("string", "minLength"), ("array", "minItems")):
        if kind in types:
            values = (schema.get(keyword) for schema in schemas)
            if not any(type(value) is int and value >= 1 for value in values):
                yield node.issue(
                    "nonempty-values",
                    f"An explicit {kind} type needs {keyword} >= 1 locally or through in-scope inheritance.",
                    keyword,
                )
    if "array" in types and not any(
        schema.get("uniqueItems") is True for schema in schemas
    ):
        yield node.issue(
            "nonempty-values",
            "An explicit array type needs uniqueItems: true locally or through in-scope inheritance.",
            "uniqueItems",
        )
