from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.py import (
    modify_ids,
    release,
    schema_diff,
    validate_examples,
    validate_jsonld_contexts,
    validate_jsonld_coverage,
    validate_jsonld_frames,
    validate_metadata,
    validate_rdf_shacl,
    verify_remote_release,
)
from fega_tools.release_workflow import build_parser as build_workflow_parser
from fega_tools.release_notes import build_parser as build_notes_parser


ROOT = Path(__file__).parents[1]
PYTHON = ROOT / ".venv/bin/python"


def _assert_documented(parser) -> None:
    assert parser.description, parser.prog
    for action in parser._actions:
        if action.dest == "help":
            continue
        if action.__class__.__name__ == "_SubParsersAction":
            for child in action.choices.values():
                _assert_documented(child)
            continue
        assert action.help and "Example:" in action.help, action


def test_every_parser_and_argument_has_plain_help_and_an_example() -> None:
    parsers = (
        modify_ids.build_arg_parser(),
        release.build_parser(),
        build_notes_parser(),
        build_workflow_parser(),
        schema_diff.build_arg_parser(),
        validate_examples.make_arg_parser(),
        validate_jsonld_contexts.make_arg_parser(),
        validate_jsonld_coverage.make_arg_parser(),
        validate_jsonld_frames.make_arg_parser(),
        validate_metadata.make_arg_parser(),
        validate_rdf_shacl.make_arg_parser(),
        verify_remote_release.build_parser(),
    )
    for parser in parsers:
        _assert_documented(parser)


def test_every_script_accepts_top_level_help() -> None:
    for script in sorted((ROOT / "scripts/py").glob("*.py")):
        result = subprocess.run([str(PYTHON), str(script), "-h"], cwd=ROOT, env={"PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True)
        assert result.returncode == 0, f"{script}: {result.stderr}"
