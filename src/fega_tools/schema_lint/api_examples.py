"""Optional Biovalidator checks using self-contained, in-scope schema snapshots."""

from __future__ import annotations

from copy import deepcopy
import json

from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from fega_tools import biovalidator
from .model import BoundSchema, children
from .resolution import OutsideResolutionRoot

DIALECT = "https://json-schema.org/draft/2020-12/schema"
CUSTOM_KEYWORDS = frozenset(
    {
        "graphRestriction",
        "isChildTermOf",
        "isValidTerm",
        "isValidTaxonomy",
        "isValidIdentifier",
    }
)
# Definitions are reached through references, not merely by being declared.
# contentSchema is an annotation in the supported draft, not an assertion.
INACTIVE_CHILDREN = {"$defs", "definitions", "contentSchema"}


def contains_custom_keywords(bound: BoundSchema, seen: frozenset = frozenset()) -> bool:
    """Inspect validation branches and references, never example/annotation data."""
    schema = bound.schema
    if not isinstance(schema, dict) or id(schema) in seen:
        return False
    seen = seen | {id(schema)}
    if CUSTOM_KEYWORDS.intersection(schema):
        return True
    for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
        if keyword in schema:
            try:
                target = bound.resolver.lookup(schema[keyword])
            except (OutsideResolutionRoot, Unresolvable):
                continue  # Reference checks report broken/excluded dependencies.
            if contains_custom_keywords(
                BoundSchema(target.contents, target.resolver), seen
            ):
                return True
    return any(
        contains_custom_keywords(bound.child(child), seen)
        for _, role, child in children(schema)
        if role not in INACTIVE_CHILDREN
    )


def bundle_constraints(constraints: list[BoundSchema]) -> dict:
    """Copy applicable constraints and rewrite static refs to local JSON Pointers.

    Resolve against the checkout before sending anything. This avoids published
    schema versions, server registrations and remote reference retrieval. Keep
    composition/conditional branches intact; never extract a custom keyword from
    its surrounding validation logic. Unsupported dynamic scope fails explicitly.
    """
    definitions = {}
    targets = {}

    def reference(bound):
        identity = id(bound.schema)
        if identity not in targets:
            key = f"s{len(targets)}"
            targets[identity] = key  # Register before descending through cycles.
            definitions[key] = copy_schema(bound)
        return {"$ref": f"#/$defs/{targets[identity]}"}

    def copy_schema(bound):
        schema = bound.schema
        if not isinstance(schema, dict):
            return schema
        if bound.resolver.specification() != DRAFT202012:
            raise ValueError(
                "Biovalidator example snapshots require draft 2020-12 schemas."
            )
        if {
            "$dynamicRef",
            "$recursiveRef",
            "$dynamicAnchor",
            "$recursiveAnchor",
        }.intersection(schema):
            raise ValueError(
                "Biovalidator example snapshots do not support dynamic/recursive reference scope."
            )
        if schema.get("$schema", DIALECT) != DIALECT:
            raise ValueError(
                "Biovalidator example snapshots require draft 2020-12 schemas."
            )
        copied = {
            key: deepcopy(value)
            for key, value in schema.items()
            if key not in INACTIVE_CHILDREN | {"$id", "$schema", "$anchor", "examples"}
        }
        if "$ref" in schema:
            target = bound.resolver.lookup(schema["$ref"])
            copied["$ref"] = reference(BoundSchema(target.contents, target.resolver))[
                "$ref"
            ]
        for path, role, child in children(schema):
            if role in INACTIVE_CHILDREN:
                continue
            container = copied
            for key in path[:-1]:
                container = container[key]
            container[path[-1]] = copy_schema(bound.child(child))
        return copied

    branches = [reference(bound) for bound in constraints]
    return {"$schema": DIALECT, "$async": True, "$defs": definitions, "allOf": branches}


class ApiExampleValidator:
    """Reuse the project's HTTP client and cache identical schema/data requests."""

    def __init__(self, url: str):
        biovalidator.assert_validator_reachable(url)
        self.url = url
        self.cache = {}

    def validate(self, schema: dict, example):
        document = {"schema": schema, "data": example}
        key = json.dumps(document, sort_keys=True)
        if key not in self.cache:
            self.cache[key] = biovalidator.validate_document(document, self.url)
        return self.cache[key]
