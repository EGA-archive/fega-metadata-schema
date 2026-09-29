"""In-scope schema traversal shared by authoring and example checks."""

from __future__ import annotations

import re

from referencing.exceptions import Unresolvable

from .model import BoundSchema, Node
from .resolution import OutsideResolutionRoot


def conjunction(bound: BoundSchema, seen: frozenset = frozenset()):
    """Expand $ref/allOf constraints at the same instance location, cycle-safely."""
    if id(bound.schema) in seen:
        return
    seen = seen | {id(bound.schema)}
    yield bound
    if not isinstance(bound.schema, dict):
        return
    for key in ("$ref", "$dynamicRef"):
        if key in bound.schema:
            try:
                resolved = bound.resolver.lookup(bound.schema[key])
            except (OutsideResolutionRoot, Unresolvable):
                # Excluded references cannot supply constraints. Broken ones are
                # diagnosed by the reference rule; still inspect other branches.
                continue
            yield from conjunction(
                BoundSchema(resolved.contents, resolved.resolver), seen
            )
    for branch in bound.schema.get("allOf", []):
        yield from conjunction(bound.child(branch), seen)


def has_declared_type(bound: BoundSchema, seen: frozenset = frozenset()) -> bool:
    """Find a type at this instance location, including composition alternatives.

    This is an authoring presence check: one typed branch suffices. It does not
    imply that its type or other constraints apply to every valid instance.
    """
    for part in conjunction(bound, seen):
        schema = part.schema
        if not isinstance(schema, dict):
            continue
        if "type" in schema:
            return True
        branch_seen = seen | {id(bound.schema), id(schema)}
        for keyword in ("anyOf", "oneOf"):
            if any(
                has_declared_type(part.child(branch), branch_seen)
                for branch in schema.get(keyword, [])
            ):
                return True
    return False


def effective_constraints(node: Node):
    """Include inherited property constraints, preserving each resolver's scope.

    Alternatives and conditionals are not unconditional inheritance. Do not
    project their constraints onto the containing definition.
    """
    yield node.bound
    if node.parent is None:
        return
    if node.role == "allOf":
        yield from effective_constraints(node.parent)
    elif node.role == "properties":
        seen = set()
        for parent in effective_constraints(node.parent):
            for bound in conjunction(parent):
                schema = bound.schema
                if not isinstance(schema, dict) or id(schema) in seen:
                    continue
                seen.add(id(schema))
                properties = schema.get("properties", {})
                if node.name in properties and properties[node.name] is not node.schema:
                    yield bound.child(properties[node.name])
                matched = False
                for pattern, child in schema.get("patternProperties", {}).items():
                    if re.search(pattern, str(node.name)):
                        matched = True
                        yield bound.child(child)
                if (
                    node.name not in properties
                    and not matched
                    and "additionalProperties" in schema
                ):
                    yield bound.child(schema["additionalProperties"])
