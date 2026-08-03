"""Rewrite raw-GitHub URIs inside JSON-compatible structures and text."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

# Match a raw-GitHub URL through its branch/tag segment.  Keeping the optional
# ``refs/...`` prefix means release tooling can rewrite both direct and fully
# qualified raw URLs without changing their shape.
RAW_GITHUB_RE = re.compile(
    r"https://raw\.githubusercontent\.com/"
    r"(?P<owner>[^/]+)/"
    r"(?P<repo>[^/]+)/"
    r"(?P<ref_prefix>refs/(?:heads|tags|remotes/[^/]+)/)?"
    r"(?P<branch>[^/]+)/"
)

# Keys that are typically URIs inside our JSON Schemas / metadata instances.
#   e.g., "$id": "https://raw.githubusercontent.com/EGA-archive/fega-metadata-schema/dev/schemas/entities/biomaterial/schema.json"
ID_KEYS = {"$id", "$ref", "@context"}

_ALLOWED_SEGMENTS = {"owner", "repo", "branch"}

# -------
# Validation of input helper
# -------

def _validate_replacements(replacements: Dict[str, Tuple[str, str]]) -> None:
    """Ensure *replacements* is well-formed.

    * keys must be a subset of {owner, repo, branch}
    * each value must be a 2-tuple of strings (source, target)
    """
    if not isinstance(replacements, dict):
        raise TypeError("replacements must be a dict with the format of: {segment: (source, target)}")

    for seg, tpl in replacements.items():
        if seg not in _ALLOWED_SEGMENTS:
            raise ValueError(f"Invalid segment '{seg}'. Allowed: {sorted(_ALLOWED_SEGMENTS)}")
        if (
            not isinstance(tpl, tuple)
            or len(tpl) != 2
            or not all(isinstance(x, str) for x in tpl)
        ):
            raise ValueError(
                f"Value for '{seg}' must be a tuple of two strings (source, target). Instead, '{tpl}' was given."
            )

# -------
# Low-level URI helpers
# -------

def _swap_multiple_segments(
    uri: str,
    replacements: Dict[str, Tuple[str, str]],
    require_all_match: bool = True,
) -> Optional[str]:
    """Return a modified URI or ``None`` when no replacement applies.

    Parameters
    ----------
    uri : str
        Raw GitHub URI to inspect.
    replacements : dict
        Mapping like ``{"owner": ("old", "new"), "repo": ("oldrepo", "newrepo"), "branch": ("oldbranch", "newbranch")}``.
        Not all groups are required to be given.
    require_all_match : bool
        If True, *all* specified segments must match their *source* before any
        replacement occurs. If False, each segment is replaced independently
        when its *source* matches.
    """
    rewritten, count = rewrite_raw_github_uris(
        uri,
        replacements,
        require_all_match=require_all_match,
    )
    return rewritten if count else None


def rewrite_raw_github_uris(
    text: str,
    replacements: Dict[str, Tuple[str, str]],
    *,
    require_all_match: bool = True,
    uri_mappings: Optional[Dict[str, str]] = None,
) -> Tuple[str, int]:
    """Rewrite every matching raw-GitHub URL occurring in *text*.

    The search is not anchored to the beginning of the string, so URLs inside
    descriptions, comments, or other JSON string values are handled as well.
    The returned count is the number of URL occurrences replaced.
    """
    _validate_replacements(replacements)
    if uri_mappings is None:
        uri_mappings = {}

    replacements_count = 0
    changed_pairs = []

    def replace(match: re.Match[str]) -> str:
        nonlocal replacements_count
        groups = match.groupdict()

        if require_all_match and any(
            groups[group] != source
            for group, (source, _target) in replacements.items()
        ):
            return match.group(0)

        values = {
            "owner": groups["owner"],
            "repo": groups["repo"],
            "branch": groups["branch"],
        }
        changed = False
        for group, (source, target) in replacements.items():
            if values[group] == source:
                values[group] = target
                changed = True

        if not changed:
            return match.group(0)

        rewritten = (
            f"https://raw.githubusercontent.com/{values['owner']}/"
            f"{values['repo']}/{groups['ref_prefix'] or ''}{values['branch']}/"
        )
        replacements_count += 1
        changed_pairs.append((match.group(0), rewritten))
        return rewritten

    rewritten_text = RAW_GITHUB_RE.sub(replace, text)
    if replacements_count:
        # A complete JSON string is the most useful mapping for callers of
        # ``patch_json_tree``. For embedded prose, retain each matched URL
        # prefix instead because there may be multiple URLs in one value.
        if not any(character.isspace() for character in text):
            uri_mappings.setdefault(text, rewritten_text)
        else:
            for source, target in changed_pairs:
                uri_mappings.setdefault(source, target)

    return rewritten_text, replacements_count


# -------
# JSON tree traversal
# -------

def patch_json_tree(
    obj: Any,
    *,
    replacements: Dict[str, Tuple[str, str]],
    require_all_match: bool = True,
    uri_mappings: Optional[Dict[str, str]] = None,
) -> Any:
    """Traverse *obj* once, applying all requested URI replacements recursively to a given JSON."""
    _validate_replacements(replacements)

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
        return _maybe_swap(obj, replacements, require_all_match, uri_mappings)

    # Numbers, booleans, and null are returned unchanged.
    return obj

# -------
# Internal helper
# -------

def _maybe_swap(
    value: Any,
    replacements: Dict[str, Tuple[str, str]],
    require_all_match: bool,
    uri_mappings: Dict[str, str],
):
    """
    Attempts to replace segments of a URI string based on provided replacements.
    Returns:
        Any: The swapped URI string if a replacement was made, otherwise the original value.
    """
    if not isinstance(value, str):
        return value

    new_uri, count = rewrite_raw_github_uris(
        value,
        replacements,
        require_all_match=require_all_match,
        uri_mappings=uri_mappings,
    )
    if not count:
        return value
    return new_uri
