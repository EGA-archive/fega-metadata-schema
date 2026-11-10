"""Read-only EGA schema linting; edit RULES to add or remove a node-level check."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.validators import validator_for
from referencing.exceptions import Unresolvable

from fega_tools.io import collect_candidate_json, load_json
from .api_examples import ApiExampleValidator
from .examples import check_format_dependencies, examples
from .mappings import mappings
from .model import BoundSchema, Issue, Node, children, pointer, walk
from .resolution import OutsideResolutionRoot, SchemaStore
from .unused_definitions import unused_definitions
from .rules import (
    documentation,
    keyword_order,
    nonempty_values,
    property_names,
    reference_style,
)

# Infrastructure checks (JSON parsing, metaschema, references) run before these
# independent node rules. A rule yields Issues and never changes the document.
RULES = (
    documentation,
    reference_style,
    keyword_order,
    property_names,
    nonempty_values,
    mappings,
    examples,
)


def discover_schemas(paths: Sequence[Path]) -> list[Path]:
    """Select conventional schema filenames or JSON documents declaring $schema.

    Explicit JSON files may have any name.

    Passing the repository directory selects its schemas/ subtree. Examples,
    expectation manifests, JSON-LD contexts and standards aren't lint targets.
    """
    selected = set()
    for path in paths:
        if path.is_file():
            selected.update(collect_candidate_json([path], missing="error"))
        else:
            directory = path / "schemas" if (path / "schemas").is_dir() else path
            for file in collect_candidate_json([directory], missing="error"):
                if file.name == "schema.json" or file.name.endswith(".schema.json"):
                    selected.add(file)
                    continue
                try:
                    value = load_json(file)
                except (OSError, ValueError):
                    continue  # Not identifiable as a schema; explicit files still fail.
                if isinstance(value, dict) and "$schema" in value:
                    selected.add(file)
    return sorted(selected)


def references(node: Node, seen: set):
    """Check every reference, even in unused definitions and external dependencies."""
    if not isinstance(node.schema, dict) or id(node.schema) in seen:
        return
    seen.add(id(node.schema))
    for keyword in ("$ref", "$dynamicRef"):
        if keyword not in node.schema:
            continue
        ref = node.schema[keyword]
        try:
            resolved = node.bound.resolver.lookup(ref)
            if not isinstance(resolved.contents, (dict, bool)):
                yield node.issue(
                    "reference-resolution",
                    f"{ref!r} points to a value that is not a schema.",
                    keyword,
                )
                continue
            # Keep the originating file/pointer in the report; mention the target
            # for transitive failures so out-of-scope dependencies are actionable.
            target = Node(
                node.file, (), BoundSchema(resolved.contents, resolved.resolver)
            )
            for issue in references(target, seen):
                yield node.issue(
                    issue.rule,
                    f"Via {ref!r}, target #{pointer(issue.path)}: {issue.message}",
                    keyword,
                )
        except OutsideResolutionRoot:
            continue
        except (Unresolvable, ValueError, TypeError, OSError) as exc:
            yield node.issue(
                "reference-resolution",
                f"Cannot resolve {ref!r} to a local schema: {exc}",
                keyword,
            )
    for path, role, child in children(node.schema):
        try:
            target = Node(node.file, node.path + path, node.bound.child(child), role)
            yield from references(target, seen)
        except (ValueError, TypeError) as exc:
            yield node.issue(
                "reference-resolution", f"Invalid referenced schema: {exc}", *path
            )


def schema_structure(file: Path) -> list[Issue]:
    """Check parsing and metaschema validity before entering bound resolvers."""
    try:
        schema = load_json(file, reject_duplicate_keys=True)
    except (OSError, ValueError) as exc:
        return [Issue(file, (), "json", str(exc))]
    if not isinstance(schema, (dict, bool)):
        return [
            Issue(
                file, (), "schema-structure", "A schema must be an object or boolean."
            )
        ]
    dialect = schema.get("$schema") if isinstance(schema, dict) else None
    cls = (
        validator_for(schema, default=None)
        if isinstance(dialect, str)
        else Draft202012Validator
    )
    if cls is None:
        return [
            Issue(
                file,
                ("$schema",),
                "schema-structure",
                "Unsupported JSON Schema dialect.",
            )
        ]
    # The repository uses draft 2020-12; absent $schema also means 2020-12.
    return [
        Issue(file, tuple(error.absolute_path), "schema-structure", error.message)
        for error in validator_for(cls.META_SCHEMA)(cls.META_SCHEMA).iter_errors(schema)
    ]


def lint_schemas(
    files: Sequence[Path],
    *,
    root: Path,
    resolution_root: Path | None = None,
    skipped_references: set[str] | None = None,
    validator_url: str | None = None,
    skipped_examples: set[str] | None = None,
    warnings: list[Issue] | None = None,
) -> list[Issue]:
    """Lint files, resolving only inside schemas/ (or a supplied boundary).

    Standalone collections without schemas/ default to root itself. Relative
    resolution roots are interpreted against root. The optional output set
    records excluded references; exclusions are not lint violations. Providing
    validator_url enables API checks for examples involving custom keywords.
    When supplied, warnings receives advisory findings separately from returned
    violations. Usage is checked across schemas/, within the resolution boundary,
    even when only some files are selected. Only selected definitions are reported.
    """
    root = root.resolve()
    if resolution_root is None:
        resolution_root = root / "schemas" if (root / "schemas").is_dir() else root
    elif not resolution_root.is_absolute():
        resolution_root = root / resolution_root
    resolution_root = resolution_root.resolve()
    if not resolution_root.is_dir():
        raise ValueError(f"Resolution root is not a directory: {resolution_root}")
    files = sorted({path.resolve() for path in files})
    for file in files:
        if not file.is_relative_to(resolution_root):
            raise ValueError(
                f"Selected schema is outside resolution root: {file}; widen --resolution-root"
            )
    check_format_dependencies()
    api = ApiExampleValidator(validator_url) if validator_url is not None else None
    issues = []
    valid_files = []
    for file in files:
        structural = schema_structure(file)
        issues.extend(structural)
        if structural:
            continue
        valid_files.append(file)
    store = SchemaStore(
        root,
        valid_files,
        resolution_root,
        skipped_references if skipped_references is not None else set(),
    )
    checked_references = set()
    for file in valid_files:
        node = store.node(file)
        issues.extend(references(node, checked_references))
        for location in walk(node):
            if isinstance(location.schema, dict):
                for rule in RULES:
                    if rule is examples:
                        issues.extend(
                            examples(location, api=api, skipped=skipped_examples)
                        )
                    else:
                        issues.extend(rule(location))
    if warnings is not None:
        collection_root = (
            root / "schemas" if (root / "schemas").is_dir() else resolution_root
        )
        sources = sorted(
            {
                path.resolve()
                for path in discover_schemas([collection_root]) + valid_files
                if path.resolve().is_relative_to(resolution_root)
                and path.resolve().is_relative_to(collection_root)
            }
        )
        sources = [
            path
            for path in sources
            if path in valid_files or not schema_structure(path)
        ]
        # Ancillary usage scanning must not add unrelated diagnostics or skipped
        # references to the selected-file report. Third-party sources are excluded.
        usage_store = SchemaStore(root, sources, resolution_root, set())
        warnings.extend(unused_definitions(usage_store, valid_files, sources))
    return sorted(
        set(issues),
        key=lambda issue: (str(issue.file), str(issue.path), issue.rule, issue.message),
    )
