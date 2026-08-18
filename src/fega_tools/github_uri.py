"""Raw ``raw.githubusercontent.com`` URI parsing and rewriting helpers.

This module owns the URI grammar used by the generic JSON rewriter and by
release tooling.  Callers decide what a URI means (for example, whether its
path is first-party); this layer only parses and renders the URI without
losing its encoded path, query string, or fragment.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any


# The path is deliberately broad enough for arbitrary repository assets while
# excluding delimiters that commonly terminate a URL embedded in JSON/prose.
# ``ref_prefix`` includes its trailing slash, so ``ref`` is always the logical
# ref value (``main`` for both ``main`` and ``refs/heads/main``).
RAW_GITHUB_RE = re.compile(
    r"https://raw\.githubusercontent\.com/"
    r"(?P<owner>[^/\s<>\"'{}|\\^`?#]+)/"
    r"(?P<repository>[^/\s<>\"'{}|\\^`?#]+)/"
    r"(?:(?P<ref_prefix>refs/(?:heads|tags|remotes/[^/\s<>\"'{}|\\^`?#]+)/))?"
    r"(?P<ref>[^/\s<>\"'{}|\\^`?#]+)/"
    r"(?P<path>[^\s<>\"'{}|\\^`?#]+)"
    r"(?P<suffix>[?#][^\s<>\"'{}|\\^`]*)?"
)


@dataclass(frozen=True)
class RawGitHubUri:
    """Parsed raw GitHub URI components.

    ``path`` is kept in its original URL-encoded form.  Filesystem callers
    should decode individual path segments only for lookup; rendering uses
    this original value so a rewrite cannot unexpectedly change document
    bytes.
    """

    owner: str
    repository: str
    ref: str
    path: str
    ref_prefix: str | None = None
    suffix: str = ""

    @property
    def raw_ref(self) -> str:
        """Return the source ref including its optional ``refs/...`` prefix."""

        return f"{self.ref_prefix or ''}{self.ref}"

    @property
    def logical_ref(self) -> str:
        """Return a ref value suitable for release-policy comparisons.

        Direct refs, ``refs/heads/...``, and ``refs/tags/...`` are all
        represented by their final ref value.  Remote refs retain their
        remote prefix because ``refs/remotes/origin/main`` is not equivalent
        to the local ``main`` branch.
        """

        return normalize_ref(self.raw_ref)

    @property
    def base(self) -> str:
        """Return the URI through the ref slash, excluding path/suffix."""

        return (
            f"https://raw.githubusercontent.com/{self.owner}/{self.repository}/"
            f"{self.raw_ref}/"
        )

    def render(
        self,
        *,
        owner: str | None = None,
        repository: str | None = None,
        ref: str | None = None,
        path: str | None = None,
        suffix: str | None = None,
        preserve_prefix: bool = True,
    ) -> str:
        """Render this URI with selected components replaced.

        ``preserve_prefix`` defaults to ``True`` for generic rewriting.  A
        release rewrite can set it to ``False`` to emit the canonical direct
        ``owner/repository/ref/path`` form.
        """

        target_owner = self.owner if owner is None else owner
        target_repository = self.repository if repository is None else repository
        target_ref = self.ref if ref is None else ref
        target_prefix = self.ref_prefix if preserve_prefix else None
        target_path = self.path if path is None else path
        target_suffix = self.suffix if suffix is None else suffix
        return (
            f"https://raw.githubusercontent.com/{target_owner}/{target_repository}/"
            f"{target_prefix or ''}{target_ref}/{target_path}{target_suffix}"
        )


def _from_match(match: re.Match[str]) -> RawGitHubUri:
    groups = match.groupdict()
    return RawGitHubUri(
        owner=groups["owner"],
        repository=groups["repository"],
        ref=groups["ref"],
        ref_prefix=groups.get("ref_prefix"),
        path=groups["path"],
        suffix=groups.get("suffix") or "",
    )


def parse_raw_github_uri(value: str) -> RawGitHubUri | None:
    """Parse *value* when it is exactly one raw GitHub URI."""

    if not isinstance(value, str):
        return None
    match = RAW_GITHUB_RE.fullmatch(value)
    return _from_match(match) if match else None


def iter_raw_github_uris(text: str) -> Iterator[RawGitHubUri]:
    """Yield parsed raw GitHub URIs embedded in *text*."""

    for match in RAW_GITHUB_RE.finditer(text):
        yield _from_match(match)


def normalize_ref(value: str) -> str:
    """Normalize direct, heads, and tags refs for semantic comparisons."""

    if value.startswith("refs/heads/") or value.startswith("refs/tags/"):
        return value.split("/", 2)[2]
    return value


def transform_raw_github_uris(
    text: str,
    transform: Callable[[RawGitHubUri], str | None],
) -> tuple[str, int]:
    """Apply *transform* to each URI and count only changed occurrences.

    Returning ``None`` leaves a URI untouched.  Returning the original URI is
    also treated as unchanged, which lets policy callbacks be explicit.
    """

    changed = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal changed
        uri = _from_match(match)
        replacement = transform(uri)
        if replacement is None or replacement == match.group(0):
            return match.group(0)
        changed += 1
        return replacement

    return RAW_GITHUB_RE.sub(replace, text), changed


ALLOWED_SEGMENTS = ("owner", "repo", "branch")


def validate_replacements(replacements: dict[str, tuple[str, str]]) -> None:
    """Ensure replacement segments and values are well formed."""

    if not isinstance(replacements, dict):
        raise TypeError("replacements must be a dict with the format of: {segment: (source, target)}")
    for segment, pair in replacements.items():
        if segment not in ALLOWED_SEGMENTS:
            raise ValueError(f"Invalid segment '{segment}'. Allowed: {sorted(ALLOWED_SEGMENTS)}")
        if not isinstance(pair, tuple) or len(pair) != 2 or not all(isinstance(item, str) for item in pair):
            raise ValueError(
                f"Value for '{segment}' must be a tuple of two strings (source, target). Instead, '{pair}' was given."
            )


def rewrite_raw_github_uris(
    text: str,
    replacements: dict[str, tuple[str, str]],
    *,
    require_all_match: bool = True,
    uri_mappings: dict[str, str] | None = None,
) -> tuple[str, int]:
    """Rewrite raw GitHub URI owner/repository/ref segments in *text*."""

    validate_replacements(replacements)
    if uri_mappings is None:
        uri_mappings = {}
    changed_pairs: list[tuple[str, str]] = []

    def replace(uri: RawGitHubUri) -> str | None:
        values = {"owner": uri.owner, "repo": uri.repository, "branch": uri.ref}
        if require_all_match and any(values[name] != source for name, (source, _target) in replacements.items()):
            return None
        changed = False
        for name, (source, target) in replacements.items():
            if values[name] == source:
                values[name] = target
                changed = True
        if not changed:
            return None
        rewritten = uri.render(
            owner=values["owner"],
            repository=values["repo"],
            ref=values["branch"],
        )
        rewritten_base = (
            f"https://raw.githubusercontent.com/{values['owner']}/{values['repo']}/"
            f"{uri.ref_prefix or ''}{values['branch']}/"
        )
        changed_pairs.append((uri.base, rewritten_base))
        return rewritten

    rewritten_text, count = transform_raw_github_uris(text, replace)
    if count:
        if not any(character.isspace() for character in text):
            uri_mappings.setdefault(text, rewritten_text)
        else:
            for source, target in changed_pairs:
                uri_mappings.setdefault(source, target)
    return rewritten_text, count


def patch_json_tree(
    obj: Any,
    *,
    replacements: dict[str, tuple[str, str]],
    require_all_match: bool = True,
    uri_mappings: dict[str, str] | None = None,
) -> Any:
    """Recursively apply URI replacements to a JSON-compatible tree."""

    validate_replacements(replacements)
    if uri_mappings is None:
        uri_mappings = {}
    if isinstance(obj, dict):
        return {
            key: patch_json_tree(
                value,
                replacements=replacements,
                require_all_match=require_all_match,
                uri_mappings=uri_mappings,
            )
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [
            patch_json_tree(
                item,
                replacements=replacements,
                require_all_match=require_all_match,
                uri_mappings=uri_mappings,
            )
            for item in obj
        ]
    if isinstance(obj, str):
        rewritten, count = rewrite_raw_github_uris(
            obj,
            replacements,
            require_all_match=require_all_match,
            uri_mappings=uri_mappings,
        )
        return rewritten if count else obj
    return obj


__all__ = [
    "RAW_GITHUB_RE",
    "RawGitHubUri",
    "ALLOWED_SEGMENTS",
    "validate_replacements",
    "iter_raw_github_uris",
    "normalize_ref",
    "parse_raw_github_uri",
    "patch_json_tree",
    "rewrite_raw_github_uris",
    "transform_raw_github_uris",
]
