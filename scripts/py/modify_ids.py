#!/usr/bin/env python3
"""modify_ids.py - bulk-rewrite raw-GitHub URIs inside JSON files

This CLI wraps ``json_pointer.patch_json_tree`` and supports *simultaneous*
updates of owner, repo and/or branch segments in one pass.

Typical usage
-------------
# Swap inplace only the branch (require the owner+repo to stay the same)
python scripts/py/modify_ids.py entities --branch dev v2.3.0 --in-place -v

# Change owner and repo in one go (independently; i.e., don't require both to match at once)
python scripts/py/modify_ids.py entities --owner old-owner new-owner --repo old-repo new-repo \
                                    --independent -o converted/ -vv
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
    from fega_tools.json_pointer import (
        _ALLOWED_SEGMENTS,
        _validate_replacements,
        patch_json_tree,
        rewrite_raw_github_uris,
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
        help="Increase log verbosity by adding more 'v's: '-v' for debug, '-vv' for all messages (trace).",
    )
    return parser

# -------
# Driver
# -------

def main(argv: Sequence[str] | None = None) -> None:
    args = build_arg_parser().parse_args(argv)
    configure_logging(args.verbosity)

    replacements = _parse_replacements(args)
    require_all = not args.independent

    all_json = collect_candidate_json(args.inputs)
    if not all_json:
        logger.error("No JSON files found under the given inputs.")
        sys.exit(1)

    logger.info(f"Scanning {len(all_json)} JSON file(s)")

    summary = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "inputs": [str(p) for p in args.inputs],
        "replacements": replacements,
        "require_all_segments_match": require_all,
        "n_total_files": len(all_json),
        "n_modified": 0,
        "processed_files": [],
        "modified_files": [],
        "uri_mappings": {}
    }

    for fp in all_json:
        logger.debug(f"Processing file: '{fp}'")
        summary["processed_files"].append(str(fp))
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning(f"Skipping invalid JSON file '{fp}': ({exc})")
            continue

        uri_map: Dict[str, str] = {}
        patched = patch_json_tree(
            data,
            replacements=replacements,
            require_all_match=require_all,
            uri_mappings=uri_map,   # Modified in-place
        )
        if uri_map:
            summary["n_modified"] += 1
            summary["modified_files"].append(str(fp))
            summary["uri_mappings"].update(uri_map)
            logger.debug(f"'{len(uri_map)}' URIs updated in '{fp}'")

            if args.in_place or args.output_directory:
                if args.output_directory:
                    out_root = args.output_directory
                    out_path = out_root / fp.relative_to(fp.parent)
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                else:  # in-place modification
                    out_path = fp

                logger.debug(f"Writing modified JSON to '{out_path}'")
                json_content = json.dumps(patched, indent=2, ensure_ascii=False) + "\n"
                out_path.write_text(json_content, encoding="utf-8")

    if args.verbosity >= 1:
        json.dump(summary, sys.stdout, indent=2)
        sys.stdout.write("\n")

    exit_code = 0 if summary["n_modified"] else 1
    sys.exit(exit_code)

if __name__ == "__main__":
    main()
