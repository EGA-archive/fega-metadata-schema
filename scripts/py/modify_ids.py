#!/usr/bin/env python3
"""Bulk-rewrite raw-GitHub URIs inside JSON and JSON-LD documents.

Typical usage
-------------
# Swap in place only the branch (require the owner+repo to stay the same)
python scripts/py/modify_ids.py schemas standards --branch dev v2.3.0 --in-place -v

# Change owner and repo independently in a converted copy
python scripts/py/modify_ids.py schemas --owner old-owner new-owner \
    --repo old-repo new-repo --independent -o converted/ -vv
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

try:
    from fega_tools.io import collect_candidate_files
    from fega_tools.json_pointer import (
        _ALLOWED_SEGMENTS,
        _validate_replacements,
        patch_json_tree,
    )
    from fega_tools.logging_utils import configure_logging
except ModuleNotFoundError as exc:
    msg = (
        "ERROR: The helper package 'fega_tools' is not importable.\n"
        "Install the repository in editable mode first:\n"
        "    pip install -e ."
    )
    raise ModuleNotFoundError(msg) from exc


logger = logging.getLogger(Path(__file__).stem)

# -------
# CLI argument helpers
# -------

def _add_segment_arg(parser: argparse.ArgumentParser, segment: str) -> None:
    parser.add_argument(
        f"--{segment}",
        metavar=("SOURCE", "TARGET"),
        nargs=2,
        help=f"Replace '{segment}' segment: SOURCE -> TARGET",
    )


def _parse_replacements(args: argparse.Namespace) -> Dict[str, Tuple[str, str]]:
    replacements: Dict[str, Tuple[str, str]] = {}
    for segment in _ALLOWED_SEGMENTS: # repo, owner, branch
        value = getattr(args, segment)
        if value is not None:
            replacements[segment] = tuple(value)
    if not replacements:
        raise SystemExit("ERROR: No replacements requested. Use --owner/--repo/--branch.")
    _validate_replacements(replacements)
    return replacements

# -------
# CLI parser
# -------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="modify_ids",
        description=(
            "Rewrite owner/repo/branch segments of raw GitHub URIs in JSON "
            "and JSON-LD documents. The default is a non-writing dry run."
        ),
        epilog=(
            "Examples:\n"
            "  modify_ids schemas --branch dev v2.3.0 --in-place -v         # Swap 'dev' with 'v2.3.0' in-place\n"
            "  modify_ids schemas --owner old-owner new-owner --repo old-repo new-repo "
            "--independent -o converted/ -vv\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "inputs",
        type=Path,
        nargs="+",
        help="JSON/JSON-LD file(s) or directory(ies) to rewrite",
    )

    for segment in _ALLOWED_SEGMENTS: # repo, owner, branch
        _add_segment_arg(parser, segment)

    parser.add_argument(
        "--independent",
        action="store_true",
        help=(
            "Apply replacements independently (default requires all specified "
            "segments to match before any replacement occurs)."
        ),
    )
    output_group = parser.add_mutually_exclusive_group() # allow either output directory or in-place modification
    output_group.add_argument(
        "-o",
        "--output",
        dest="output_directory",
        type=Path,
        help="Directory for modified copies while preserving originals",
    )
    output_group.add_argument(
        "-w",
        "--in-place",
        action="store_true",
        help="Rewrite files in place instead of copying",
    )
    parser.add_argument(
        "--verbosity",
        "-v",
        action="count",
        default=0,
        help="Increase logging verbosity by adding more 'v's: '-v' for debug, '-vv' for all messages (trace).",
    )
    return parser


def _collect_candidates(inputs: Sequence[Path]) -> List[Tuple[Path, Path]]:
    """Return ``(source, output-relative-path)`` pairs for JSON documents."""
    candidates: Dict[Path, Path] = {}
    relative_paths_by_source: Dict[Path, Path] = {}
    suffixes = {".json", ".jsonld"}

    def add_candidate(source: Path, relative: Path) -> None:
        """Add one source/output pair while rejecting ambiguous overlaps."""
        existing_relative = relative_paths_by_source.get(source)
        if existing_relative is not None:
            if existing_relative != relative:
                raise ValueError(
                    f"Input source '{source}' maps to multiple output paths: "
                    f"'{existing_relative}' and '{relative}'"
                )
            return

        previous = candidates.get(relative)
        if previous is not None and previous != source:
            raise ValueError(
                f"Multiple inputs map to output path '{relative}': "
                f"'{previous}' and '{source}'"
            )

        candidates[relative] = source
        relative_paths_by_source[source] = relative

    for input_path in inputs:
        resolved = input_path.resolve()
        if not resolved.exists():
            raise FileNotFoundError(f"Path not found: {input_path}")

        if resolved.is_dir():
            files = collect_candidate_files(
                [resolved], suffixes, missing="error"
            )
            for source in files:
                relative = Path(resolved.name) / source.relative_to(resolved)
                add_candidate(source, relative)
        elif resolved.is_file():
            files = collect_candidate_files(
                [resolved], suffixes, missing="error"
            )
            for source in files:
                relative = Path(resolved.name)
                add_candidate(source, relative)
        else:
            logger.debug("Ignoring non-JSON path: '%s'", input_path)

    return sorted(
        ((source, relative) for relative, source in candidates.items()),
        key=lambda candidate: (
            candidate[1].suffix.casefold() == ".jsonld",
            candidate[1].as_posix().casefold(),
        ),
    )


def _read_text(path: Path) -> str:
    """Read without normalizing CRLF/LF line endings."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_text(path: Path, content: str) -> None:
    """Write without normalizing CRLF/LF line endings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def main(argv: Sequence[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    configure_logging(args.verbosity)

    replacements = _parse_replacements(args)
    require_all = not args.independent

    try:
        candidates = _collect_candidates(args.inputs)
    except (FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        sys.exit(1)

    if not candidates:
        logger.error("No JSON or JSON-LD files found under the given inputs.")
        sys.exit(1)

    logger.info("Scanning %d JSON/JSON-LD file(s)", len(candidates))
    summary = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "mode": (
            "in-place"
            if args.in_place
            else "output"
            if args.output_directory
            else "dry-run"
        ),
        "inputs": [str(path) for path in args.inputs],
        "replacements": replacements,
        "require_all_segments_match": require_all,
        "n_total_files": len(candidates),
        "n_modified": 0,
        "processed_files": [],
        "modified_files": [],
        "uri_mappings": {},
        "errors": [],
    }
    pending_writes: List[Tuple[Path, str]] = []

    for source, relative in candidates:
        logger.debug("Processing file: '%s'", source)
        summary["processed_files"].append(str(source))
        try:
            original_text = _read_text(source)
            data = json.loads(original_text)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            summary["errors"].append(f"'{source}': {exc}")
            continue

        uri_map: Dict[str, str] = {}
        patched = patch_json_tree(
            data,
            replacements=replacements,
            require_all_match=require_all,
            uri_mappings=uri_map,
        )
        if not uri_map:
            continue

        patched_text = json.dumps(patched, indent=2, ensure_ascii=False) + "\n"
        if "\r\n" in original_text:
            patched_text = patched_text.replace("\n", "\r\n")

        summary["n_modified"] += 1
        summary["modified_files"].append(str(source))
        summary["uri_mappings"].update(uri_map)
        logger.debug("'%d' URI mapping(s) updated in '%s'", len(uri_map), source)

        if args.in_place:
            pending_writes.append((source, patched_text))
        elif args.output_directory:
            pending_writes.append((args.output_directory / relative, patched_text))

    if summary["errors"]:
        json.dump(summary, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)

    try:
        for output_path, content in pending_writes:
            _write_text(output_path, content)
    except OSError as exc:
        summary["errors"].append(str(exc))
        json.dump(summary, sys.stdout, indent=2)
        sys.stdout.write("\n")
        sys.exit(1)

    if args.verbosity >= 1:
        json.dump(summary, sys.stdout, indent=2)
        sys.stdout.write("\n")

    sys.exit(0 if summary["n_modified"] else 1)


if __name__ == "__main__":
    main()
