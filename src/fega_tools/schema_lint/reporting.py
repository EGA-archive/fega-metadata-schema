"""Human-readable lint reports; formatting never changes lint results."""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
from collections import Counter, defaultdict
from pathlib import Path
from typing import Sequence, TextIO

from .model import Issue, pointer

try:
    from colorama import Fore, Style, just_fix_windows_console
except ModuleNotFoundError:
    Fore = Style = None


def _use_color(mode: str, stream: TextIO) -> bool:
    """Explicit CLI choices override the automatic terminal/environment policy."""
    if Fore is None or mode == "never":
        return False
    return mode == "always" or (
        stream.isatty()
        and not os.environ.get("NO_COLOR")
        and os.environ.get("TERM") != "dumb"
    )


def _display_path(path: Path, root: Path) -> str:
    """Shorten repository paths while keeping external files unambiguous."""
    path = path.resolve()
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def print_report(
    issues: Sequence[Issue],
    *,
    file_count: int,
    root: Path,
    stream: TextIO,
    verbosity: int = 0,
    color: str = "auto",
    warnings: Sequence[Issue] = (),
    checks: Sequence[str] = (),
    skipped_references: set[str] | None = None,
    skipped_examples: set[str] | None = None,
    validator_url: str | None = None,
) -> None:
    """Group violations by file; reserve detailed skipped-check lists for -vv.

    Colour is supplementary: labels and spacing carry the same information in
    plain logs. Pointers and URLs remain intact for copying, even on narrow TTYs.
    """
    root = root.resolve()
    use_color = _use_color(color, stream)
    if use_color and stream is sys.stdout:
        just_fix_windows_console()
        stream = sys.stdout  # Older Windows consoles may now use a wrapper.
    styles = (
        {
            "heading": Style.BRIGHT,
            "error": Fore.RED,
            "path": Fore.CYAN,
            "warning": Fore.YELLOW,
            "success": Fore.GREEN,
        }
        if use_color
        else {}
    )
    width = max(40, min(100, shutil.get_terminal_size((100, 24)).columns))

    def line(value: str = "", style: str = "") -> None:
        if style in styles:
            value = styles[style] + value + Style.RESET_ALL
        print(value, file=stream)

    def paragraph(value: str, indent: str = "", style: str = "") -> None:
        # Preserve explicit line breaks (e.g. nested validation explanations).
        for part in value.splitlines():
            for wrapped in textwrap.wrap(
                part,
                width=width,
                initial_indent=indent,
                subsequent_indent=indent,
                break_long_words=False,
                break_on_hyphens=False,
            ):
                line(wrapped, style)

    if verbosity and checks:
        paragraph("Checks: " + ", ".join(checks))
        line()

    def findings_report(findings: Sequence[Issue], style: str) -> None:
        grouped = defaultdict(list)
        for issue in findings:
            grouped[issue.file].append(issue)
        for file, findings in sorted(grouped.items()):
            line(_display_path(file, root), "heading")
            for issue in findings:
                line(f"  [{issue.rule}]", style)
                line(f"    Path: #{pointer(issue.path)}", "path")
                paragraph(issue.message, "    ")
                line()

    if verbosity and issues:
        findings_report(issues, "error")
    if verbosity > 1 and warnings:
        line("Warnings (not lint violations)", "warning")
        findings_report(warnings, "warning")

    if validator_url:
        paragraph(f"Custom-keyword example checks: Biovalidator at {validator_url}.")
        line()

    if skipped_references or skipped_examples:
        line("Incomplete checks (not lint violations)", "warning")
        if skipped_examples:
            paragraph(
                f"Custom-keyword validation incomplete for {len(skipped_examples)} example array(s). "
                + (
                    "Some dependencies are outside --resolution-root."
                    if validator_url
                    else "Use --check-api to enable Biovalidator checks."
                ),
                "  ",
            )
        if skipped_references:
            paragraph(
                f"Skipped {len(skipped_references)} out-of-scope reference(s); "
                "dependent example checks may be incomplete. "
                "Use --resolution-root . to include repository dependencies.",
                "  ",
            )
        if verbosity > 1:
            for ref in sorted(skipped_references or ()):
                line("  [skipped-reference]", "warning")
                line(f"    {ref}")
            for example in sorted(skipped_examples or ()):
                file, separator, detail = example.partition("#")
                location = _display_path(Path(file), root) + separator + detail
                line("  [skipped-example]", "warning")
                line(f"    {location}")
        else:
            line("  Use -vv to list skipped references and example locations.")
        line()

    line(
        f"Linted {file_count} schema(s): {len(issues)} violation(s); {len(warnings)} warning(s).",
        (
            "error"
            if issues
            else (
                "warning"
                if warnings or skipped_references or skipped_examples
                else "success"
            )
        ),
    )
    if issues:
        counts = Counter(issue.rule for issue in issues)
        rule_width = max(len("Rule"), *(len(rule) for rule in counts))
        line(f"  {'Rule':<{rule_width}}  Count", "heading")
        for rule, count in sorted(counts.items()):
            line(f"  {rule:<{rule_width}}  {count:>5}")
        if not verbosity:
            line("Use -v to display every violation.")
    if warnings and verbosity < 2:
        line("Use -vv to display every warning.")
