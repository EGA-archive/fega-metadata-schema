"""Check examples with local references and unconditional inherited property rules.

Projection follows only $ref/allOf, never conditional or alternative branches:
those are evaluated by jsonschema when validating the containing instance.
Ontology service keywords remain annotations; this linter makes no OLS requests.
"""

from __future__ import annotations

import re

from jsonschema import FormatChecker
from jsonschema.validators import validator_for
from referencing.exceptions import Unresolvable

from .model import BoundSchema, Node, pointer
from .resolution import OutsideResolutionRoot


def check_format_dependencies() -> None:
    """Do not silently accept unchecked URI/date-time examples in partial installs."""
    # These formats are used by the maintained schemas but require jsonschema's
    # optional dependencies. Its default behavior is to ignore missing checkers.
    missing = {"uri", "date-time"} - FormatChecker().checkers.keys()
    if missing:
        raise RuntimeError(
            "Missing JSON Schema format validators: "
            + ", ".join(sorted(missing))
            + ". Install the format extras in this Python environment with "
            "python -m pip install -r requirements.txt."
        )


def conjunction(bound: BoundSchema, seen: frozenset = frozenset()):
    """Expand the unconditional constraints at the same instance location."""
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
            except OutsideResolutionRoot:
                continue
            yield from conjunction(
                BoundSchema(resolved.contents, resolved.resolver), seen
            )
    for branch in bound.schema.get("allOf", []):
        yield from conjunction(bound.child(branch), seen)


def effective_constraints(node: Node):
    """Include constraints inherited by a property, without flattening schemas."""
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


def examples(node: Node):
    values = node.schema.get("examples")
    if not isinstance(values, list):
        return
    try:
        # Keep resolvers attached to each inherited constraint, so relative refs,
        # nested IDs and anchors retain their original scope.
        constraints = {}
        for bound in effective_constraints(node):
            constraints[id(bound.schema)] = bound
    except (Unresolvable, ValueError, TypeError, re.error) as exc:
        yield node.issue(
            "example-validation",
            f"Cannot resolve inherited constraints: {exc}",
            "examples",
        )
        return
    for index, example in enumerate(values):
        messages = set()
        for bound in constraints.values():
            try:
                cls = validator_for(bound.schema)
                validator = cls(
                    bound.schema,
                    _resolver=bound.resolver,
                    format_checker=FormatChecker(),
                )
                for error in validator.iter_errors(example):
                    message = f"Instance #{pointer(tuple(error.absolute_path))}: {error.message}"
                    if message not in messages:
                        messages.add(message)
                        yield node.issue(
                            "example-validation", message, "examples", index
                        )
            except OutsideResolutionRoot:
                # Never substitute true/false for an excluded reference: either
                # can produce incorrect results under not/oneOf/if. Leave this
                # dependent constraint unchecked and continue independent ones.
                continue
            except (
                Unresolvable,
                ValueError,
                TypeError,
                RecursionError,
                re.error,
            ) as exc:
                yield node.issue(
                    "example-validation",
                    f"Cannot validate example: {exc}",
                    "examples",
                    index,
                )
