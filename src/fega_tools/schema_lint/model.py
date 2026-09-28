"""Schema locations and keyword-aware traversal (never walk instance annotations)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from referencing import Resource
from referencing.jsonschema import DRAFT202012

MAP_KEYWORDS = {
    "$defs",
    "definitions",
    "properties",
    "patternProperties",
    "dependentSchemas",
}
LIST_KEYWORDS = {"allOf", "anyOf", "oneOf", "prefixItems"}
SINGLE_KEYWORDS = {
    "items",
    "contains",
    "additionalProperties",
    "unevaluatedProperties",
    "propertyNames",
    "additionalItems",
    "unevaluatedItems",
    "not",
    "if",
    "then",
    "else",
    "contentSchema",
}
DEFINITION_ROLES = {
    "root",
    "$defs",
    "definitions",
    "properties",
    "patternProperties",
    "items",
    "prefixItems",
    "additionalProperties",
    "unevaluatedProperties",
    "propertyNames",
    "additionalItems",
    "unevaluatedItems",
    "contentSchema",
}


def pointer(parts: tuple) -> str:
    return "".join(
        "/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts
    )


def resource(schema: Any) -> Resource:
    return Resource.from_contents(schema, default_specification=DRAFT202012)


@dataclass(frozen=True)
class Issue:
    file: Path
    path: tuple
    rule: str
    message: str

    def __str__(self) -> str:
        return f"{self.file}#{pointer(self.path)} [{self.rule}] {self.message}"


@dataclass
class BoundSchema:
    schema: Any
    resolver: Any

    def child(self, schema: Any) -> BoundSchema:
        return BoundSchema(schema, self.resolver.in_subresource(resource(schema)))


@dataclass
class Node:
    file: Path
    path: tuple
    bound: BoundSchema
    role: str = "root"
    parent: Node | None = None
    name: str | int | None = None

    @property
    def schema(self) -> Any:
        return self.bound.schema

    def issue(self, rule: str, message: str, *suffix: Any) -> Issue:
        return Issue(self.file, self.path + suffix, rule, message)


def children(schema: Any) -> Iterator[tuple[tuple, str, Any]]:
    """Yield schema-valued keywords only; examples, contexts and mappings are data."""
    if not isinstance(schema, dict):
        return
    for key, value in schema.items():
        if key in MAP_KEYWORDS and isinstance(value, dict):
            for name, child in value.items():
                if isinstance(child, (dict, bool)):
                    yield (key, name), key, child
        elif key in LIST_KEYWORDS and isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, (dict, bool)):
                    yield (key, index), key, child
        elif key in SINGLE_KEYWORDS and isinstance(value, (dict, bool)):
            yield (key,), key, value
        # Older drafts can occur in referenced standards.
        elif key == "items" and isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, (dict, bool)):
                    yield (key, index), "prefixItems", child
        elif key == "dependencies" and isinstance(value, dict):
            for name, child in value.items():
                if isinstance(child, (dict, bool)):
                    yield (key, name), key, child


def walk(node: Node) -> Iterator[Node]:
    yield node
    for path, role, child in children(node.schema):
        yield from walk(
            Node(
                node.file,
                node.path + path,
                node.bound.child(child),
                role,
                node,
                path[-1],
            )
        )
