"""Offline JSON Schema resources, reusing the repository's local URI catalogue."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urldefrag, urljoin, urlparse

from referencing import Registry
from referencing._core import Resolved
from referencing.exceptions import NoSuchResource, Unretrievable

from fega_tools.io import load_json
from fega_tools.github_uri import parse_raw_github_uri
from fega_tools.jsonld_utils import build_id_to_path_map
from .model import BoundSchema, Node, resource


class OutsideResolutionRoot(Exception):
    """An intentionally excluded reference, not a broken schema dependency."""


class LocalResolver:
    """Canonicalize retrieval aliases before resolving fragments.

    referencing deliberately uses the retrieval URI for pointer lookups. A file
    URI and a declared $id can differ, so enter the canonical resource first to
    preserve relative nested IDs and references. All actual resolution, anchors
    and dynamic scope remain delegated to referencing. Private resolver access
    is isolated here (jsonschema's _resolver integration uses the same object).
    """

    def __init__(self, resolver, store):
        """Create a resolver that uses the local schema store."""
        self.resolver = resolver
        self.store = store

    def lookup(self, ref):
        """Resolve a reference through the local schema store."""
        uri, fragment = urldefrag(urljoin(self.resolver._base_uri, ref))
        self.store.check_uri(uri)
        try:
            retrieved = self.resolver._registry.get_or_retrieve(uri)
        except (NoSuchResource, Unretrievable):
            # Delegate failure formatting to referencing.
            return self.resolver.lookup(ref)
        identifier = retrieved.value.id()
        canonical = identifier if identifier and urlparse(identifier).scheme else uri
        resolved = self.resolver.lookup(
            canonical + ("#" + fragment if fragment else "")
        )
        return Resolved(resolved.contents, LocalResolver(resolved.resolver, self.store))

    def in_subresource(self, subresource):
        """Create a resolver for a nested schema resource."""
        return LocalResolver(self.resolver.in_subresource(subresource), self.store)

    def dynamic_scope(self):
        """Return the resolver's dynamic scope."""
        return self.resolver.dynamic_scope()


class SchemaStore:
    """Resolve canonical IDs, file URIs, pointers and anchors without network I/O."""

    def __init__(
        self,
        root: Path,
        files: list[Path],
        resolution_root: Path,
        skipped_references: set[str],
    ):
        """Build a local store for the selected schema files."""
        self.root = root
        self.resolution_root = resolution_root
        self.skipped_references = skipped_references
        search_roots = [resolution_root]
        if resolution_root == root:
            # Preserve the repository catalogue without scanning virtualenvs or
            # unrelated tooling. Other allowed files remain reachable by URI.
            search_roots = [
                path for path in (root / "schemas", root / "standards") if path.is_dir()
            ] or search_roots
        self.paths = build_id_to_path_map(root, search_roots=search_roots)
        self.documents = {}
        self.physical_paths: dict[Path, Path] = {}
        self.registry = Registry(retrieve=self.retrieve)
        # Register selected files first, including explicitly named nonstandard files.
        for path in files:
            try:
                document = self.load(path)
                self.registry = self.registry.with_resource(
                    path.as_uri(), resource(document)
                )
                if isinstance(document, dict) and isinstance(document.get("$id"), str):
                    self.paths[document["$id"]] = path
            except (ValueError, OSError):
                # The runner reports load errors per file and continues.
                continue
        self.registry = self.registry.crawl()

    def physical_path(self, path: Path) -> Path:
        """Return the cached physical path for a file."""
        # A run is read-only; resolving symlinks once avoids filesystem I/O on
        # every repeated lookup during example validation.
        if path not in self.physical_paths:
            self.physical_paths[path] = path.resolve()
        return self.physical_paths[path]

    def load(self, path: Path):
        """Load and cache a schema file."""
        path = self.physical_path(path)
        self.check_path(path, path.as_uri())
        if path not in self.documents:
            self.documents[path] = load_json(path, reject_duplicate_keys=True)
        return self.documents[path]

    def path_for_uri(self, uri: str) -> Path | None:
        """Map a URI to a local path without opening its target."""
        path = self.paths.get(uri)
        parsed = urlparse(uri)
        raw = parse_raw_github_uri(uri)
        if (
            path is None
            and raw
            and (raw.owner, raw.repository) == ("EGA-archive", "fega-metadata-schema")
        ):
            # Bundled standards also have arbitrary .json filenames. Published
            # first-party IDs resolve to this checkout, including release IDs.
            candidate = self.physical_path(self.root / unquote(raw.path))
            if candidate.is_relative_to(self.root):
                path = candidate
        if (
            path is None
            and parsed.scheme == "file"
            and parsed.netloc in {"", "localhost"}
        ):
            path = Path(unquote(parsed.path))
        return self.physical_path(path) if path is not None else None

    def check_path(self, path: Path, uri: str) -> None:
        """Reject files outside the configured resolution root."""
        if not path.is_relative_to(self.resolution_root):
            self.skipped_references.add(uri)
            raise OutsideResolutionRoot(uri)

    def check_uri(self, uri: str) -> None:
        """Check the local path for a reference URI."""
        path = self.path_for_uri(uri)
        if path is not None:
            self.check_path(path, uri)

    def retrieve(self, uri: str):
        """Load the local resource for a reference URI."""
        path = self.path_for_uri(uri)
        if path is None:
            raise NoSuchResource(
                ref=f"{uri} (no local file; network access is disabled)"
            )
        self.check_path(path, uri)
        document = self.load(path)
        if isinstance(document, dict) and isinstance(document.get("$id"), str):
            # A dependency can have an arbitrary filename, absent from the
            # catalogue. Retain its canonical alias once discovered locally.
            self.paths[urljoin(uri, document["$id"])] = path
        return resource(document)

    def node(self, path: Path) -> Node:
        """Create a schema node with a resolver bound to the file."""
        schema = self.load(path)
        resolver = self.registry.resolver(path.as_uri()).in_subresource(
            resource(schema)
        )
        return Node(path, (), BoundSchema(schema, LocalResolver(resolver, self)))
