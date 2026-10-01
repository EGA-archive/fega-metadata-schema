"""Reports preserve diagnostics in terminals, plain logs and compact summaries."""

from __future__ import annotations

import io
import os
import re
import shutil

import pytest

from fega_tools.schema_lint.model import Issue
from fega_tools.schema_lint import reporting
from scripts.py.lint_schemas import main


class Terminal(io.StringIO):
    def isatty(self):
        return True


def render(root, issues=(), *, stream=None, **options):
    stream = stream if stream is not None else io.StringIO()
    reporting.print_report(issues, root=root, file_count=2, stream=stream, **options)
    return stream.getvalue()


def test_verbose_report_groups_files_and_separates_every_diagnostic(tmp_path):
    common = tmp_path / "schemas/common/schema.json"
    entity = tmp_path / "schemas/entities/DAC/schema.json"
    issues = [
        Issue(
            common,
            ("properties", "a/b~c", "title"),
            "documentation",
            "Title is missing.",
        ),
        Issue(entity, (), "schema-structure", "Invalid root."),
        Issue(
            common,
            ("examples", 0),
            "example-validation",
            "First reason.\nSecond reason.",
        ),
    ]
    output = render(tmp_path, issues, verbosity=1)
    assert str(tmp_path) not in output
    assert output.count("schemas/common/schema.json") == 1
    assert output.index("Second reason.") < output.index(
        "schemas/entities/DAC/schema.json"
    )
    assert (
        "  [documentation]\n    Path: #/properties/a~1b~0c/title\n    Title is missing."
        in output
    )
    assert "    Path: #\n    Invalid root." in output
    assert "    First reason.\n    Second reason." in output
    assert "Linted 2 schema(s): 3 violation(s); 0 warning(s)." in output
    assert "\x1b[" not in output


def test_external_paths_and_long_pointers_are_never_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(
        shutil, "get_terminal_size", lambda fallback: os.terminal_size((40, 24))
    )
    path = tmp_path / "external.schema.json"
    name = "aLongProperty" * 12
    message = (
        "A useful explanation that should wrap without losing any words or details."
    )
    output = render(
        tmp_path / "repository",
        [Issue(path, ("properties", name), "documentation", message)],
        verbosity=1,
    )
    assert str(path) in output
    assert f"Path: #/properties/{name}\n" in output
    assert message in " ".join(output.split())


@pytest.mark.parametrize(
    "mode,terminal,no_color,term,expected",
    [
        ("auto", True, "", "xterm", True),
        ("auto", False, "", "xterm", False),
        ("auto", True, "1", "xterm", False),
        ("auto", True, "", "dumb", False),
        ("never", True, "", "xterm", False),
        ("always", False, "1", "dumb", True),
    ],
)
def test_color_policy_preserves_plain_report(
    tmp_path, monkeypatch, mode, terminal, no_color, term, expected
):
    monkeypatch.setenv("NO_COLOR", no_color)
    monkeypatch.setenv("TERM", term)
    issues = [Issue(tmp_path / "schema.json", (), "documentation", "Missing title.")]
    output = render(
        tmp_path,
        issues,
        stream=Terminal() if terminal else io.StringIO(),
        verbosity=1,
        color=mode,
    )
    assert ("\x1b[" in output) == expected
    assert re.sub(r"\x1b\[[0-9;]*m", "", output) == render(
        tmp_path, issues, verbosity=1, color="never"
    )


def test_colorama_remains_optional(tmp_path, monkeypatch):
    monkeypatch.setattr(reporting, "Fore", None)
    output = render(tmp_path, color="always", stream=Terminal())
    assert output == "Linted 2 schema(s): 0 violation(s); 0 warning(s).\n"


def test_skipped_checks_are_distinct_and_details_require_double_verbose(tmp_path):
    options = dict(
        skipped_references={"https://example.org/external.schema.json"},
        skipped_examples={
            f"{tmp_path}/schemas/schema.json#/examples: custom keywords require --check-api"
        },
    )
    output = render(tmp_path, verbosity=1, **options)
    assert "Incomplete checks (not lint violations)" in output
    text = " ".join(output.split())
    assert "Use --check-api" in text and "Use --resolution-root ." in text
    assert "Use -vv" in output
    assert "https://example.org/" not in output
    assert "Linted 2 schema(s): 0 violation(s); 0 warning(s)." in output
    detailed = render(tmp_path, verbosity=2, **options)
    assert (
        "[skipped-reference]\n    https://example.org/external.schema.json" in detailed
    )
    assert "[skipped-example]\n    schemas/schema.json#/examples:" in detailed
    assert "Use -vv" not in detailed
    api_report = render(
        tmp_path, validator_url="http://localhost:3020/validate", **options
    )
    assert "Some dependencies are outside --resolution-root." in " ".join(
        api_report.split()
    )
    assert "Use --check-api" not in api_report


def test_compact_summary_counts_rules_without_printing_details(tmp_path):
    output = render(
        tmp_path,
        [
            Issue(tmp_path / "schema.json", (), "documentation", "Missing title."),
            Issue(tmp_path / "schema.json", (), "documentation", "Missing examples."),
            Issue(tmp_path / "schema.json", (), "nonempty-values", "Missing minItems."),
        ],
    )
    assert "Missing" not in output
    assert re.search(r"documentation\s+2\n", output)
    assert re.search(r"nonempty-values\s+1\n", output)
    assert "Use -v to display every violation." in output


def test_cli_plain_report_is_read_only_and_keeps_failure_exit_code(tmp_path, capsys):
    schema = tmp_path / "schemas/schema.json"
    schema.parent.mkdir()
    original = '{"type": "string"}'
    schema.write_text(original)
    assert main(["--root", str(tmp_path), "--color", "never", "-v"]) == 1
    output = capsys.readouterr().out
    assert "\x1b[" not in output
    assert "schemas/schema.json\n" in output
    assert "  [documentation]\n" in output
    assert "Path: #/title" in output and "Path: #/minLength" in output
    assert schema.read_text() == original
