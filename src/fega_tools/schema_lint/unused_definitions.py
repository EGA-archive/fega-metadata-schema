"""Collection-wide advisory check for $defs with no incoming references.

This is reference accounting, not reachability analysis: mutually referencing
definitions count as used, but a definition's own recursive references do not.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from urllib.parse import quote, unquote, urldefrag

from referencing.exceptions import Unresolvable

from .model import Issue, walk
from .resolution import OutsideResolutionRoot, SchemaStore


def unused_definitions(
    store: SchemaStore, selected: Sequence[Path], sources: Sequence[Path]
) -> Iterator[Issue]:
    """Report selected definitions, checking all collection sources.

    Bound resolvers handle IDs, aliases, anchors and nested resource scopes.
    Only schema nodes are scanned, never examples or annotations.
    """
    nodes = []
    for file in sources:
        try:
            nodes.extend(list(walk(store.node(file))))
        except (OSError, ValueError, TypeError, Unresolvable):
            # Parsing/structure errors belong to normal selected-file checks.
            continue
    locations = {
        id(node.schema): node for node in nodes if isinstance(node.schema, dict)
    }
    # True/False are singletons: index their containers, not their identities.
    booleans = {}
    for node in nodes:
        if isinstance(node.schema, bool) and node.path:
            parent = store.load(node.file)
            for part in node.path[:-1]:
                parent = parent[part]
            booleans[id(parent), str(node.path[-1])] = node

    definitions = {
        (node.file, node.path): node
        for node in nodes
        if node.role == "$defs" and node.file in selected
    }
    used = set()
    for source in nodes:
        if not isinstance(source.schema, dict):
            continue
        for keyword in ("$ref", "$dynamicRef"):
            ref = source.schema.get(keyword)
            if not isinstance(ref, str):
                continue
            try:
                resolved = source.bound.resolver.lookup(ref)
                target = locations.get(id(resolved.contents))
                if isinstance(resolved.contents, bool):
                    uri, fragment = urldefrag(ref)
                    fragment = unquote(fragment)
                    if not fragment.startswith("/"):
                        continue  # Boolean resource roots cannot contain $defs.
                    parent_pointer, _, token = fragment.rpartition("/")
                    parent = source.bound.resolver.lookup(
                        uri + "#" + quote(parent_pointer, safe="/~")
                    ).contents
                    token = token.replace("~1", "/").replace("~0", "~")
                    target = booleans.get((id(parent), token))
                if target is None:
                    continue
            except (
                OutsideResolutionRoot,
                Unresolvable,
                OSError,
                ValueError,
                TypeError,
                AttributeError,  # A referenced, unselected document may be malformed.
            ):
                continue  # Broken/excluded refs belong to the normal checks.

            # Referencing part of a definition uses it; referencing a parent
            # schema does not automatically use entries in that schema's $defs.
            for length in range(1, len(target.path) + 1):
                path = target.path[:length]
                key = (target.file, path)
                if key in definitions and not (
                    source.file == target.file and source.path[:length] == path
                ):
                    used.add(key)

    for key in sorted(definitions, key=lambda key: (str(key[0]), str(key[1]))):
        if key not in used:
            yield definitions[key].issue(
                "unused-definition",
                "No reference to this $defs entry was found elsewhere in the schema collection.",
            )
