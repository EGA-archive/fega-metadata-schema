"""
io.py - file-system helpers for FEGA tools
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Collection, List, Literal, Sequence, Set

LOGGER = logging.getLogger(__name__)

MissingPathPolicy = Literal["warn", "error"]


def collect_candidate_files(
    paths: Sequence[Path],
    suffixes: Collection[str],
    *,
    missing: MissingPathPolicy = "warn",
) -> List[Path]:
    """Return matching files found under files or directories in *paths*.

    Suffix matching is case-insensitive. Returned paths are resolved,
    deduplicated, and sorted. Missing paths are warnings by default, or raise
    ``FileNotFoundError`` when ``missing="error"`` is requested.
    """
    if missing not in {"warn", "error"}:
        raise ValueError("missing must be either 'warn' or 'error'")

    raw_suffixes = (suffixes,) if isinstance(suffixes, str) else suffixes
    normalized_suffixes = {
        suffix.lower() if suffix.startswith(".") else f".{suffix.lower()}"
        for suffix in raw_suffixes
    }
    if not normalized_suffixes:
        raise ValueError("At least one file suffix is required")

    files: Set[Path] = set()

    for p in paths:
        if not p.exists():
            message = f"Path not found: {p}"
            if missing == "error":
                raise FileNotFoundError(message)
            LOGGER.warning("%s", message)
            continue

        if p.is_dir():
            for fp in p.rglob("*"):
                if fp.is_file() and fp.suffix.lower() in normalized_suffixes:
                    files.add(fp.resolve())
        elif p.is_file() and p.suffix.lower() in normalized_suffixes:
            files.add(p.resolve())
        else:
            LOGGER.debug("Ignoring non-matching path: %s", p)

    return sorted(files)


def collect_candidate_json(
    paths: Sequence[Path],
    *,
    include_jsonld: bool = False,
    missing: MissingPathPolicy = "warn",
) -> List[Path]:
    """Return candidate JSON files, optionally including JSON-LD files."""
    suffixes = {".json", ".jsonld"} if include_jsonld else {".json"}
    return collect_candidate_files(paths, suffixes, missing=missing)


def load_json(path: Path) -> Any:
    """Load and return the JSON value stored at *path*."""
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_json_object(path: Path) -> dict[str, Any]:
    """Load JSON from *path* and require a top-level object."""
    value = load_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def clone_json(value: Any) -> Any:
    """Return a JSON-safe deep copy of *value*."""
    return json.loads(json.dumps(value))
