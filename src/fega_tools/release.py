"""Repository-discovered release analysis and verification for FEGA schemas.

The release inventory is derived from the working tree.  No release
configuration file or change-fragment directory is consulted by this module.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote, urlsplit

from jsonschema import Draft202012Validator, FormatChecker

from .github_uri import (
    RawGitHubUri,
    iter_raw_github_uris,
    normalize_ref,
    parse_raw_github_uri,
    transform_raw_github_uris,
)
from .schema_diff import Severity, compare_schemas


SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
TEXT_EXTENSIONS = {".json", ".jsonld", ".ttl", ".jsonldc", ".yaml", ".yml", ".md"}
TOP_LEVEL_CITATION_VERSION_RE = re.compile(r"(?m)^version\s*:\s*([^\r\n#]+?)\s*(?:#.*)?$")
HEX64_RE = re.compile(r"^[a-f0-9]{64}$")
_REPOSITORY_RE = re.compile(r"^[^/\\\s]+/[^/\\\s]+$")
_GITHUB_SCP_RE = re.compile(r"^git@github\.com:(?P<path>[^?#\s]+)$", re.IGNORECASE)
_GITHUB_URL_SCHEMES = {"http", "https", "ssh", "git", "git+ssh"}


@functools.total_ordering
@dataclass(frozen=True)
class Version:
    """Strict SemVer 2.0.0 value with correct prerelease precedence."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str | int, ...] = ()
    build: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> "Version":
        if not isinstance(value, str):
            raise ValueError(f"Invalid semantic version: {value!r}")
        match = SEMVER_RE.fullmatch(value)
        if not match:
            raise ValueError(f"Invalid semantic version: {value}")
        pre_raw, build_raw = match.group(4), match.group(5)
        pre: list[str | int] = []
        if pre_raw:
            for identifier in pre_raw.split("."):
                if identifier.isdigit():
                    if len(identifier) > 1 and identifier.startswith("0"):
                        raise ValueError(f"Invalid numeric prerelease identifier: {value}")
                    pre.append(int(identifier))
                else:
                    pre.append(identifier)
        return cls(int(match.group(1)), int(match.group(2)), int(match.group(3)), tuple(pre), tuple((build_raw or "").split(".")) if build_raw else ())

    def _precedence(self) -> tuple[Any, ...]:
        # A stable version has higher precedence than every prerelease.
        pre = (1,) if not self.prerelease else (0, tuple((0, item) if isinstance(item, int) else (1, item) for item in self.prerelease))
        return (self.major, self.minor, self.patch, *pre)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        if (self.major, self.minor, self.patch) != (other.major, other.minor, other.patch):
            return (self.major, self.minor, self.patch) < (other.major, other.minor, other.patch)
        if not self.prerelease or not other.prerelease:
            return bool(self.prerelease) and not bool(other.prerelease)
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            if isinstance(left, int) and isinstance(right, str):
                return True
            if isinstance(left, str) and isinstance(right, int):
                return False
            return left < right
        return len(self.prerelease) < len(other.prerelease)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and (self.major, self.minor, self.patch, self.prerelease) == (other.major, other.minor, other.patch, other.prerelease)

    def bump(self, severity: Severity) -> "Version":
        if severity == Severity.MAJOR:
            return Version(self.major + 1, 0, 0)
        if severity == Severity.MINOR:
            return Version(self.major, self.minor + 1, 0)
        if severity == Severity.PATCH:
            return Version(self.major, self.minor, self.patch + 1)
        return self

    def __str__(self) -> str:
        value = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            value += "-" + ".".join(str(part) for part in self.prerelease)
        if self.build:
            value += "+" + ".".join(self.build)
        return value


@dataclass(frozen=True)
class Component:
    name: str
    schema: Path
    version: Version
    identifier: str
    context: Path | None = None
    frame: Path | None = None

    @property
    def id(self) -> str:
        return self.identifier

    @property
    def assets(self) -> tuple[Path, ...]:
        return tuple(path for path in (self.context, self.frame) if path is not None)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")


def _repository_path(path: str) -> str | None:
    """Return a validated ``owner/repository`` path, if present."""
    value = path
    if value.startswith("/"):
        value = value[1:]
    if value.endswith("/"):
        value = value[:-1]
    if value.startswith("/") or value.endswith("/"):
        return None
    if value.endswith(".git"):
        value = value[:-4]
    return value if _REPOSITORY_RE.fullmatch(value) else None


def _repository_candidate(value: str) -> str | None:
    """Parse a repository name or GitHub remote without substring matching."""
    scp = _GITHUB_SCP_RE.fullmatch(value)
    if scp:
        return _repository_path(scp.group("path"))

    direct = _repository_path(value)
    if direct is not None:
        return direct

    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    if scheme not in _GITHUB_URL_SCHEMES:
        return None
    if hostname is None or hostname.casefold() != "github.com":
        return None
    if scheme in {"ssh", "git+ssh"}:
        if parsed.username not in {None, "git"} or parsed.password is not None:
            return None
    elif parsed.username is not None or parsed.password is not None:
        return None
    if port is not None or parsed.query or parsed.fragment:
        return None
    return _repository_path(parsed.path)


def repository_identity(root: Path | None = None, repository: str | None = None) -> str:
    """Resolve ``owner/repository`` from CLI, environment, then Git origin."""
    candidate = repository or os.environ.get("GITHUB_REPOSITORY")
    if candidate is None and root is not None:
        try:
            candidate = subprocess.check_output(["git", "-C", str(root), "config", "--get", "remote.origin.url"], text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            candidate = None
    if candidate:
        value = _repository_candidate(candidate.strip())
        if value is not None:
            return value
    raise ValueError("Cannot resolve repository identity; pass --repository owner/repo or set GITHUB_REPOSITORY")


def _component_name(path: Path) -> str:
    if path.name == "schema.json":
        return path.parent.name
    return path.name[: -len(".schema.json")]


def discover_components(root: Path) -> list[Component]:
    """Discover deterministic first-party components from ``schemas/**``."""
    root = root.resolve()
    candidates = sorted((path for path in (root / "schemas").rglob("*") if path.is_file() and (path.name == "schema.json" or path.name.endswith(".schema.json"))), key=lambda path: path.relative_to(root).as_posix())
    components: list[Component] = []
    names: dict[str, Path] = {}
    ids: dict[str, Path] = {}
    for path in candidates:
        relative = path.relative_to(root)
        try:
            document = load_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Cannot parse component schema {relative}: {exc}") from exc
        if not isinstance(document, dict):
            raise ValueError(f"Component schema must be an object: {relative}")
        identifier = document.get("$id")
        version_raw = document.get("meta:version")
        if not isinstance(identifier, str) or not identifier:
            raise ValueError(f"Component schema requires a string $id: {relative}")
        if not isinstance(version_raw, str):
            # Discovery intentionally ignores JSON schemas that are not
            # release components (for example local test fixtures).
            continue
        version = Version.parse(version_raw)
        name = _component_name(path)
        if name in names:
            raise ValueError(f"Duplicate derived component name '{name}' ({names[name]} and {relative})")
        if identifier in ids:
            raise ValueError(f"Duplicate component $id '{identifier}' ({ids[identifier]} and {relative})")
        names[name] = relative
        ids[identifier] = relative
        context = path.parent / "context.jsonld"
        frame = path.parent / "frame.jsonld"
        components.append(Component(name, relative, version, identifier, context.relative_to(root) if context.is_file() else None, frame.relative_to(root) if frame.is_file() else None))
    return components


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _group_digest(directory: Path) -> tuple[str, int]:
    """Hash sorted files as ``u64(path length), path, u64(data length), data``."""
    digest = hashlib.sha256()
    files = sorted(path for path in directory.rglob("*") if path.is_file())
    for path in files:
        relative = path.relative_to(directory).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest(), len(files)


def discover_standard_groups(root: Path) -> list[dict[str, Any]]:
    """Discover and hash immediate groups below JSON Schema and RDF standards."""
    root = root.resolve()
    groups: list[dict[str, Any]] = []
    for family in ("json-schema", "rdf"):
        base = root / "standards" / family
        if not base.is_dir():
            continue
        for directory in sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name):
            digest, count = _group_digest(directory)
            groups.append({"name": directory.name, "path": directory.relative_to(root).as_posix(), "sha256": digest, "file_count": count})
    return sorted(groups, key=lambda item: item["path"])


def standard_groups(root: Path) -> list[dict[str, Any]]:
    """Alias retained for callers that use the shorter API name."""
    return discover_standard_groups(root)


def _normalise_uri(value: str) -> str:
    normalised, _ = transform_raw_github_uris(
        value,
        lambda uri: uri.render(
            owner="{owner}",
            repository="{repo}",
            ref="{ref}",
            preserve_prefix=False,
        ),
    )
    return normalised


def _local_asset_path(uri: RawGitHubUri, root: Path, *, include_manifest: bool = False) -> Path | None:
    """Resolve an existing first-party URI asset to a root-relative path."""

    decoded_path = unquote(uri.path)
    parts = decoded_path.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    candidate = Path(*parts)
    root_resolved = root.resolve()
    try:
        resolved = (root_resolved / candidate).resolve()
        relative = resolved.relative_to(root_resolved)
    except (OSError, ValueError):
        return None
    allowed = bool(relative.parts and relative.parts[0] in {"schemas", "standards"})
    if include_manifest:
        allowed = allowed or relative == Path("build/release_manifest.schema.json")
    if not allowed or not resolved.is_file():
        return None
    return relative


def _scan_dependencies(root: Path, components: list[Component], *, transitive_standards: bool = True) -> dict[str, list[str]]:
    by_path = {component.schema: component.name for component in components}
    groups = discover_standard_groups(root)
    group_by_path = {Path(item["path"]): item["name"] for item in groups}
    result: dict[str, set[str]] = {component.name: set() for component in components}
    document_cache: dict[Path, Any] = {}
    refs_cache: dict[Path, set[str]] = {}
    standard_closure_cache: dict[Path, set[str]] = {}

    def refs(path: Path) -> set[str]:
        if path in refs_cache:
            return refs_cache[path]
        try:
            document = document_cache.setdefault(path, load_json(root / path))
        except (OSError, json.JSONDecodeError):
            document_cache[path] = None
            refs_cache[path] = set()
            return set()
        found: set[str] = set()
        def visit(value: Any) -> None:
            if isinstance(value, dict):
                if isinstance(value.get("$ref"), str):
                    found.add(value["$ref"])
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(document)
        refs_cache[path] = found
        return found

    def standard_closure(path: Path) -> set[str]:
        if path in standard_closure_cache:
            return standard_closure_cache[path]
        found: set[str] = set()
        pending = [path]
        seen: set[Path] = set()
        while pending:
            current = pending.pop(0)
            if current in seen:
                continue
            seen.add(current)
            for reference in sorted(refs(current)):
                base = reference.split("#", 1)[0]
                if not base:
                    continue
                parsed_uri = parse_raw_github_uri(base)
                target = _local_asset_path(parsed_uri, root) if parsed_uri else None
                if target is None and not urlsplit(base).scheme:
                    candidate = (root / current).parent / unquote(base)
                    try:
                        target = candidate.resolve().relative_to(root.resolve())
                    except ValueError:
                        target = None
                if target is None:
                    continue
                for group_path, group_name in group_by_path.items():
                    if group_path == target or group_path in target.parents:
                        found.add("standard:" + group_path.as_posix())
                if target.parts and target.parts[0] == "standards" and target not in seen:
                    pending.append(target)
        standard_closure_cache[path] = found
        return found

    direct: dict[str, set[str]] = {component.name: set() for component in components}
    standard_edges: dict[str, set[str]] = {component.name: set() for component in components}
    for component in components:
        pending = [component.schema]
        visited: set[Path] = set()
        while pending:
            current = pending.pop(0)
            if current in visited:
                continue
            visited.add(current)
            for reference in sorted(refs(current)):
                base = reference.split("#", 1)[0]
                if not base:
                    continue
                parsed_uri = parse_raw_github_uri(base)
                target = _local_asset_path(parsed_uri, root) if parsed_uri else None
                if target is None and not urlsplit(base).scheme:
                    candidate = (root / current).parent / unquote(base)
                    try:
                        target = candidate.resolve().relative_to(root.resolve())
                    except ValueError:
                        target = None
                    if target is not None and not (root / target).is_file():
                        target = None
                if target is None:
                    parsed = urlsplit(base)
                    if not parsed.scheme:
                        raise ValueError(f"Component '{component.name}' has missing local schema dependency: {base}")
                    parsed_uri = parse_raw_github_uri(base)
                    if parsed_uri and parsed_uri.path.strip("/").split("/", 1)[0] in {"schemas", "standards"}:
                        raise ValueError(f"Component '{component.name}' has missing first-party schema dependency: {base}")
                    continue
                if target in by_path and by_path[target] != component.name:
                    direct[component.name].add(by_path[target])
                    # Component dependency closure is computed below from a
                    # graph of direct edges; walking the target here would
                    # repeat the same schema traversal for every component.
                    continue
                for group_path, group_name in group_by_path.items():
                    if group_path == target or group_path in target.parents:
                        standard_edges[component.name].add("standard:" + group_path.as_posix())
                if target.parts and target.parts[0] == "standards":
                    if transitive_standards:
                        standard_edges[component.name].update(standard_closure(target))
                    continue
                if target.suffix in {".json", ".jsonld"} and target not in visited:
                    pending.append(target)
    # Compute component transitive closure over a finite graph, so cycles are
    # deterministic and cannot make release analysis recurse indefinitely.
    for name in direct:
        pending = list(sorted(direct[name]))
        seen: set[str] = set()
        while pending:
            dependency = pending.pop(0)
            if dependency in seen:
                continue
            seen.add(dependency)
            pending.extend(sorted(direct.get(dependency, set()) - seen))
        result[name] = seen | standard_edges[name]
    return {name: sorted(values) for name, values in result.items()}


def associate_pr_rationales(
    root: Path,
    pull_requests: Sequence[Any],
    *,
    components: list[Component] | None = None,
    dependencies: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, str]:
    """Associate concrete PR compatibility reviews with affected components.

    A review is accepted only where the PR's changed files intersect the
    component, a referenced component, or a typed standard group reached by
    that component.  This deliberately avoids turning one broad review into a
    waiver for unrelated unknown changes.
    """
    root = root.resolve()
    components = components or discover_components(root)
    dependencies = dependencies or _scan_dependencies(root, components, transitive_standards=True)
    by_name = {item.name: item for item in components}

    def changed(record: Any) -> set[str]:
        values = getattr(record, "changed_files", None)
        if values is None and isinstance(record, Mapping):
            values = record.get("changed_files", record.get("files", ()))
        return {str(item).strip().lstrip("./") for item in (values or ()) if isinstance(item, str) and str(item).strip()}

    def rationale(record: Any) -> str | None:
        notes = getattr(record, "release_notes", None)
        if notes is None and isinstance(record, Mapping):
            notes = record.get("compatibility_rationale", record.get("compatibility_review"))
            if notes == "Not applicable":
                notes = None
        value = getattr(notes, "compatibility_rationale", notes)
        if not isinstance(value, str) or not value.strip() or value.strip().casefold() == "not applicable":
            return None
        return " ".join(value.split())

    candidates: dict[str, list[tuple[int, str]]] = {item.name: [] for item in components}
    for record in pull_requests:
        value = rationale(record)
        files = changed(record)
        if value is None or not files:
            continue
        number = int(getattr(record, "number", record.get("number", 0) if isinstance(record, Mapping) else 0))
        for component in components:
            relevant: set[str] = {component.schema.as_posix(), component.schema.parent.as_posix() + "/"}
            # Context/frame and other sibling assets are covered by the
            # component directory.  Include dependent component directories.
            for dependency in dependencies.get(component.name, ()):
                if dependency.startswith("standard:"):
                    relevant.add(dependency.removeprefix("standard:").rstrip("/") + "/")
                elif dependency in by_name:
                    relevant.add(by_name[dependency].schema.parent.as_posix() + "/")
            if any(path == candidate.rstrip("/") or path.startswith(candidate) for path in files for candidate in relevant):
                candidates[component.name].append((number, value))
    result: dict[str, str] = {}
    for name, values in candidates.items():
        unique = sorted(set(values), key=lambda item: (item[0], item[1]))
        if unique:
            result[name] = "; ".join(f"PR #{number}: {value}" for number, value in unique)
    return result


# Descriptive aliases retained for callers that prefer a verb phrase.
approved_rationales_from_prs = associate_pr_rationales
collect_approved_rationales = associate_pr_rationales


def _asset_hashes(root: Path, component: Component) -> dict[str, str]:
    values = {"schema": _sha256(root / component.schema)}
    if component.context:
        values["context"] = _sha256(root / component.context)
    if component.frame:
        values["frame"] = _sha256(root / component.frame)
    return values


def _component_by_identity(components: list[Component], identifier: str) -> Component | None:
    normalised = _normalise_uri(identifier)
    for component in components:
        if _normalise_uri(component.identifier) == normalised:
            return component
    return None


def _changed_file(old: Path | None, new: Path | None) -> bool:
    if old is None or new is None:
        return old != new
    return old.read_bytes() != new.read_bytes()


def analyse_release(root: Path, previous_root: Path | None = None, *, bootstrap: bool = False, approved_rationales: Mapping[str, str] | None = None, repository: str | None = None, requested_version: str | None = None) -> dict[str, Any]:
    """Analyse current components against an optional previous checkout."""
    root = root.resolve()
    repo = repository_identity(root, repository)
    current = discover_components(root)
    dependencies = _scan_dependencies(root, current, transitive_standards=True)
    previous: list[Component] = []
    if previous_root is not None:
        try:
            previous = discover_components(previous_root.resolve())
        except (OSError, ValueError) as exc:
            if not bootstrap:
                raise
            previous = []
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    rationales = approved_rationales or {}
    current_groups = {item["name"]: item for item in discover_standard_groups(root)}
    previous_groups = {item["name"]: item for item in discover_standard_groups(previous_root.resolve())} if previous_root and previous else {}
    previous_has_release_manifest = bool(
        previous_root and (previous_root.resolve() / "build/release_manifest.json").is_file()
    )
    required_by_name: dict[str, Severity] = {}
    detail_by_name: dict[str, list[dict[str, Any]]] = {}
    old_by_name: dict[str, Component | None] = {}
    for component in current:
        old = _component_by_identity(previous, component.identifier)
        if old is None:
            old = next((item for item in previous if item.name == component.name), None)
        required = Severity.SAME
        details: list[dict[str, Any]] = []
        previous_version: Version | None = old.version if old else None
        old_by_name[component.name] = old
        if old is None:
            required = Severity.SAME
        else:
            old_schema = load_json(previous_root / old.schema) if previous_root and (previous_root / old.schema).is_file() else {}
            new_schema = load_json(root / component.schema)
            diff = compare_schemas(old_schema, new_schema)
            required = diff.severity
            details.extend(change.as_dict() for change in diff.changes)
            if old.schema != component.schema:
                required = max(required, Severity.MAJOR)
                details.insert(0, {"path": old.schema.as_posix(), "severity": "major", "message": "component schema relocated"})
            if _changed_file(previous_root / old.context if previous_root and old.context else None, root / component.context if component.context else None):
                required = max(required, Severity.PATCH)
                details.append({"path": (component.context or old.context or Path("context.jsonld")).as_posix(), "severity": "patch", "message": "context changed"})
            if _changed_file(previous_root / old.frame if previous_root and old.frame else None, root / component.frame if component.frame else None):
                required = max(required, Severity.PATCH)
                details.append({"path": (component.frame or old.frame or Path("frame.jsonld")).as_posix(), "severity": "patch", "message": "frame changed"})
            for dependency in dependencies.get(component.name, []):
                if dependency.startswith("standard:"):
                    group_path = dependency.removeprefix("standard:")
                    current_group = next((item for item in current_groups.values() if item["path"] == group_path), None)
                    previous_group = next((item for item in previous_groups.values() if item["path"] == group_path), None)
                    if current_group and previous_group and current_group["sha256"] != previous_group["sha256"]:
                        required = max(required, Severity.UNKNOWN)
                        details.append({"path": group_path, "severity": "unknown", "message": "reachable standard group changed"})
        required_by_name[component.name] = required
        detail_by_name[component.name] = details
    # A changed dependency affects every component that reaches it.  Keep the
    # graph finite and deterministic even when component references cycle.
    for component in current:
        inherited = [required_by_name.get(dep, Severity.SAME) for dep in dependencies.get(component.name, []) if dep in required_by_name]
        if inherited:
            required_by_name[component.name] = max([required_by_name[component.name], *inherited])
    for component in current:
        old = old_by_name[component.name]
        required = required_by_name[component.name]
        details = detail_by_name[component.name]
        original_required = required
        exception = None
        if required == Severity.UNKNOWN and isinstance(rationales.get(component.name), str) and rationales[component.name].strip():
            # The caller has supplied an explicit review rationale; the core
            # still requires a version change, but does not invent a major.
            exception = {"used": True, "provenance": rationales[component.name].strip()}
            required = Severity.PATCH
            required_by_name[component.name] = required
        if old is not None:
            if component.version == old.version and details:
                errors.append(f"Component '{component.name}' changed but meta:version is unchanged")
            minimum = old.version.bump(required if required != Severity.UNKNOWN else Severity.MAJOR)
            allow_initial_prerelease_reset = (
                not previous_has_release_manifest
                and not details
                and required == Severity.SAME
                and bool(old.version.prerelease)
                and bool(component.version.prerelease)
                and component.version < old.version
            )
            if component.version < minimum and not allow_initial_prerelease_reset:
                errors.append(f"Component '{component.name}' declares {component.version}, below automatic minimum {minimum}")
        records.append({"name": component.name, "schema": component.schema.as_posix(), "id": component.identifier, "version": str(component.version), "previous_version": str(old.version) if old else None, "required_change": required.label(), "dependencies": dependencies.get(component.name, []), "checksums": _asset_hashes(root, component), "details": details, "compatibility_exception": exception or {"used": False}, "automatic_required_change": original_required.label()})
    current_ids = {_normalise_uri(item.identifier) for item in current}
    removed = [{"name": item.name, "schema": item.schema.as_posix(), "previous_version": str(item.version), "change": "major"} for item in previous if _normalise_uri(item.identifier) not in current_ids]
    bundle_change = Severity.MAJOR if removed else max((required_by_name.get(item.name, Severity.SAME) for item in current), default=Severity.SAME)
    if any(old_by_name.get(item.name) is None for item in current):
        bundle_change = max(bundle_change, Severity.MINOR)
    bundle = Version.parse(requested_version) if requested_version else None
    if bundle and previous_root:
        previous_manifest_path = previous_root.resolve() / "build/release_manifest.json"
        if previous_manifest_path.is_file():
            try:
                previous_bundle = Version.parse(load_json(previous_manifest_path).get("bundle_version"))
                if bundle <= previous_bundle:
                    errors.append(f"Bundle version {bundle} must be greater than previous release {previous_bundle}")
                elif not previous_bundle.prerelease and bundle_change != Severity.SAME:
                    minimum_bundle = previous_bundle.bump(bundle_change if bundle_change != Severity.UNKNOWN else Severity.MAJOR)
                    if bundle < minimum_bundle:
                        errors.append(f"Bundle version {bundle} is below automatic minimum {minimum_bundle}")
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                if not bootstrap:
                    errors.append("Previous release manifest bundle_version is malformed")
    return {"repository": repo, "components": records, "removed_components": removed, "errors": errors, "bundle_version": str(bundle) if bundle else None, "bundle_change": bundle_change.label()}


def create_manifest(root: Path, *, bundle_version: str, repository: str | None = None, source_commit: str, release_date: str | None = None, tag: str | None = None) -> dict[str, Any]:
    root = root.resolve()
    repo = repository_identity(root, repository)
    version = Version.parse(bundle_version)
    if release_date is None:
        release_date = date.today().isoformat()
    date.fromisoformat(release_date)
    components = discover_components(root)
    dependencies = _scan_dependencies(root, components)
    entries = []
    for component in components:
        entries.append({"name": component.name, "schema": component.schema.as_posix(), "id": component.identifier, "version": str(component.version), "dependencies": dependencies.get(component.name, []), "checksums": _asset_hashes(root, component)})
    entries.sort(key=lambda item: (item["schema"], item["name"]))
    return {"$schema": "release_manifest.schema.json", "manifest_version": 2, "bundle_version": str(version), "tag": tag or f"v{version}", "release_date": release_date, "source_repository": repo, "source_commit": source_commit, "components": entries, "standards": discover_standard_groups(root)}


def _manifest_path(root: Path) -> Path:
    return root / "build" / "release_manifest.json"


def _rewrite_text(text: str, root: Path, repository: str, from_ref: str, target_ref: str) -> tuple[str, int]:
    source_ref = normalize_ref(from_ref)
    release_ref = normalize_ref(target_ref)
    repository_owner, repository_name = repository.split("/", 1)

    def replace(uri: RawGitHubUri) -> str | None:
        if _local_asset_path(uri, root, include_manifest=True) is None or uri.logical_ref != source_ref:
            return None
        return uri.render(owner=repository_owner, repository=repository_name, ref=release_ref, preserve_prefix=False)

    return transform_raw_github_uris(text, replace)


def rewrite_repository_uris(root: Path, from_ref: str, to_ref: str, *, repository: str | None = None) -> int:
    root = root.resolve()
    repo = repository_identity(root, repository)
    changed = 0
    for directory in (root / "schemas", root / "standards", root / "build"):
        if not directory.is_dir():
            continue
        for path in sorted(item for item in directory.rglob("*") if item.is_file() and item.suffix in TEXT_EXTENSIONS):
            raw = path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            rewritten, count = _rewrite_text(text, root, repo, from_ref, to_ref)
            if count and rewritten != text:
                path.write_bytes(rewritten.encode("utf-8"))
                changed += count
    return changed


def _citation_version(root: Path) -> str | None:
    path = root / "CITATION.cff"
    if not path.is_file():
        return None
    matches = TOP_LEVEL_CITATION_VERSION_RE.findall(path.read_text(encoding="utf-8"))
    return matches[0].strip() if len(matches) == 1 else None


def _validate_manifest(root: Path, manifest: dict[str, Any]) -> list[str]:
    schema_path = root / "build" / "release_manifest.schema.json"
    if not schema_path.is_file():
        return []
    schema = load_json(schema_path)
    return [error.message for error in Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(manifest)]


def verify_release(root: Path, *, mode: str = "development", expected_ref: str | None = None, tag: str | None = None, repository: str | None = None) -> None:
    """Verify development inventory or an exact candidate/tag snapshot."""
    if mode == "release":
        mode = "tag"
    if mode not in {"development", "candidate", "tag"}:
        raise ValueError(f"Unsupported verification mode: {mode}")
    root = root.resolve()
    manifest_path = _manifest_path(root)
    if not manifest_path.is_file() and mode == "development":
        # Bootstrap repositories have no immutable inventory yet.  Discovery
        # itself remains a useful syntax check and a missing manifest is not a
        # release verification failure before the first tag exists.
        components = discover_components(root)
        discover_standard_groups(root)
        _scan_dependencies(root, components)
        return
    manifest = load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("Release manifest must be an object")
    errors = _validate_manifest(root, manifest)
    strict = mode in {"candidate", "tag"}
    check_ref = strict or (mode == "development" and expected_ref is not None)
    expected = tag or expected_ref
    if strict:
        if not expected:
            raise ValueError("candidate/tag verification requires --tag or expected ref")
        if manifest.get("tag") != expected:
            errors.append("manifest tag does not match expected tag")
        try:
            repo = repository_identity(root, repository)
            if manifest.get("source_repository") != repo:
                errors.append("manifest repository does not match expected repository")
        except ValueError as exc:
            errors.append(str(exc))
        if _citation_version(root) != manifest.get("bundle_version"):
            errors.append("CITATION.cff version does not match manifest bundle_version")
        if manifest.get("tag") != f"v{manifest.get('bundle_version')}":
            errors.append("manifest tag does not match bundle_version")
    try:
        repo = repository_identity(root, repository)
    except ValueError:
        repo = manifest.get("source_repository", "")
        if not repo:
            try:
                first_id = discover_components(root)[0].identifier
                parsed = parse_raw_github_uri(first_id)
                repo = f"{parsed.owner}/{parsed.repository}" if parsed else ""
            except (IndexError, OSError, ValueError):
                repo = ""
    if repo:
        try:
            _scan_dependencies(root, discover_components(root))
        except ValueError as exc:
            errors.append(str(exc))
    for directory in (root / "schemas", root / "standards", root / "build"):
        if not directory.is_dir():
            continue
        for path in directory.rglob("*"):
            if not path.is_file() or path.suffix not in TEXT_EXTENSIONS:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                errors.append(f"{path.relative_to(root)} is not UTF-8")
                continue
            for uri in iter_raw_github_uris(text):
                if _local_asset_path(uri, root, include_manifest=True) is not None and check_ref:
                    identity = uri.owner + "/" + uri.repository
                    if identity != repo:
                        errors.append(f"{path.relative_to(root)} contains URI repository {identity!r}, expected {repo!r}")
                    elif uri.logical_ref != normalize_ref(expected or ""):
                        errors.append(f"{path.relative_to(root)} contains URI ref {uri.raw_ref!r}, expected {expected!r}")
    if strict:
        components = discover_components(root)
        expected_entries = create_manifest(root, bundle_version=str(manifest.get("bundle_version")), repository=manifest.get("source_repository"), source_commit=str(manifest.get("source_commit", "")), release_date=str(manifest.get("release_date", "")), tag=str(manifest.get("tag", "")))["components"]
        if manifest.get("components") != expected_entries:
            errors.append("manifest component inventory or checksums do not match working tree")
        if manifest.get("standards") != discover_standard_groups(root):
            errors.append("manifest standards inventory or checksums do not match working tree")
    if errors:
        raise ValueError("Release verification failed:\n- " + "\n- ".join(errors))


def prepare_release(root: Path, previous_root: Path | None = None, *, release_date: str, source_commit: str, bootstrap: bool = False, requested_version: str | None = None, repository: str | None = None, write: bool = True, changelog_text: str | None = None, approved_rationales: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Prepare a release report and optionally materialise its tag snapshot."""
    if requested_version is None:
        raise ValueError("A bundle version must be supplied explicitly")
    analysis = analyse_release(root, previous_root, bootstrap=bootstrap, approved_rationales=approved_rationales, repository=repository, requested_version=requested_version)
    if analysis["errors"]:
        raise ValueError("Release analysis failed:\n- " + "\n- ".join(analysis["errors"]))
    if not write:
        return analysis
    repo = repository_identity(root, repository)
    tag = f"v{requested_version}"
    rewrite_repository_uris(root, "main", tag, repository=repo)
    citation = root / "CITATION.cff"
    if citation.is_file():
        text = citation.read_text(encoding="utf-8")
        updated, count = TOP_LEVEL_CITATION_VERSION_RE.subn(lambda match: f"version: {requested_version}", text, count=1)
        if count != 1:
            raise ValueError("CITATION.cff must contain exactly one top-level version")
        citation.write_text(updated, encoding="utf-8")
    manifest = create_manifest(root, bundle_version=requested_version, repository=repo, source_commit=source_commit, release_date=release_date, tag=tag)
    write_json(_manifest_path(root), manifest)
    if changelog_text is not None:
        (root / "CHANGELOG.md").write_text(changelog_text, encoding="utf-8")
    verify_release(root, mode="tag", expected_ref=tag, repository=repo)
    analysis["manifest"] = manifest
    return analysis


def build_manifest(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return create_manifest(*args, **kwargs)
