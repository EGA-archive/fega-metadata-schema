"""Parse pull-request release notes and build deterministic changelog entries.

The module deliberately keeps Markdown parsing small and strict.  GitHub transport is
isolated behind an injectable client so collection can be tested without network access.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import quote, urlsplit, urlunsplit

from .cli_utils import help_with_example

ALLOWED_CATEGORIES = ("Added", "Changed", "Fixed", "Removed", "Security", "None")
RENDER_CATEGORIES = ("Added", "Changed", "Fixed", "Removed", "Security")
_H2_RE = re.compile(r"^##\s+(.+?)\s*$")
_CATEGORY_RE = re.compile(r"^Category:\s*(\S.*?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s*(.*?)\s*$")
_PLACEHOLDER_RE = re.compile(
    r"\b(?:tbd|todo|your\s+(?:summary|description)|placeholder|"
    r"describe\s+the\s+user-visible\s+change|"
    r"this\s+pr\s+(?:adds?|changes?|fixes?|removes?)\s+x\b)"
    r"|\[\s*(?:describe|summary|add)\b",
    re.I,
)


@dataclass(frozen=True)
class Diagnostic:
    """A stable, machine-readable validation finding."""

    code: str
    message: str
    line: int | None = None
    section: str | None = None

    def __str__(self) -> str:
        where = f"line {self.line}: " if self.line else ""
        return f"{where}{self.message} ({self.code})"


@dataclass(frozen=True)
class ParsedReleaseNotes:
    category: str | None = None
    bullets: tuple[str, ...] = ()
    compatibility_rationale: str | None = None
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def summary(self) -> tuple[str, ...]:
        return self.bullets

    @property
    def summary_text(self) -> str:
        return " ".join(self.bullets)

    @property
    def compatibility_review(self) -> str | None:
        return self.compatibility_rationale

    @property
    def rationale(self) -> str | None:
        return self.compatibility_rationale

    @property
    def valid(self) -> bool:
        return not self.diagnostics


@dataclass(frozen=True)
class PullRequestRecord:
    number: int
    title: str
    html_url: str
    merge_commit_sha: str
    merged_at: str
    body: str = ""
    parsed: ParsedReleaseNotes | None = None
    commits: tuple[str, ...] = ()
    # Repository-relative files changed by this PR.  This is populated from
    # the GitHub ``pulls/{number}/files`` endpoint, never from PR prose.
    changed_files: tuple[str, ...] = ()

    @property
    def files(self) -> tuple[str, ...]:
        """Compatibility alias used by consumers of older snapshots."""
        return self.changed_files

    @property
    def release_notes(self) -> ParsedReleaseNotes:
        return self.parsed or parse_pr_body(self.body)

    @property
    def category(self) -> str | None:
        return self.release_notes.category

    @property
    def bullets(self) -> tuple[str, ...]:
        return self.release_notes.bullets


@dataclass(frozen=True)
class CommitRecord:
    sha: str
    subject: str


@dataclass(frozen=True)
class CollectionResult:
    pull_requests: tuple[PullRequestRecord, ...]
    commits: tuple[CommitRecord, ...] = ()
    accounted_commits: tuple[str, ...] = ()

    @property
    def records(self) -> tuple[PullRequestRecord, ...]:
        return self.pull_requests

    def to_dict(self) -> dict[str, Any]:
        return {
            "pull_requests": [record_to_dict(pr) for pr in self.pull_requests],
            "commits": [asdict(commit) for commit in self.commits],
            "accounted_commits": list(self.accounted_commits),
        }


class CollectionError(ValueError):
    """Raised when a release range cannot be accounted for safely."""


class GitHubClient(Protocol):
    """Minimal read-only interface used by :func:`collect_pull_requests`.

    Implementations may expose either ``list_commits`` and ``associated_pull_requests``
    or the equivalent ``compare_commits`` and ``get_commit_pulls`` methods.
    """


class _HtmlCommentParser(HTMLParser):
    """Collect HTML comment spans without interpreting note content as HTML.

    ``HTMLParser`` follows the browser-compatible comment tokenisation rules,
    including malformed endings such as ``--!>``.  The source is only masked
    after parsing so all non-comment text remains byte-for-byte unchanged and
    line numbers remain stable for diagnostics.
    """

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self._source = source
        self._line_offsets = [0]
        for line in source.splitlines(keepends=True):
            self._line_offsets.append(self._line_offsets[-1] + len(line))
        self.spans: list[tuple[int, int]] = []
        self._comment_start: int | None = None

    def _offset(self) -> int:
        line, column = self.getpos()
        if line <= 0 or line > len(self._line_offsets):
            return len(self._source)
        return min(self._line_offsets[line - 1] + column, len(self._source))

    def _event(self) -> None:
        if self._comment_start is not None:
            self.spans.append((self._comment_start, self._offset()))
            self._comment_start = None

    def finish(self) -> None:
        """Close a final comment at end-of-input, including an unterminated one."""
        if self._comment_start is not None:
            self.spans.append((self._comment_start, len(self._source)))
            self._comment_start = None

    def handle_comment(self, data: str) -> None:
        self._event()
        self._comment_start = self._offset()

    # Every other token callback marks the end of a preceding comment.  The
    # callbacks retain no parsed HTML; this helper only uses the parser to
    # identify comment spans.
    def handle_data(self, data: str) -> None:
        self._event()

    def handle_entityref(self, name: str) -> None:
        self._event()

    def handle_charref(self, name: str) -> None:
        self._event()

    def handle_decl(self, decl: str) -> None:
        self._event()

    def handle_pi(self, data: str) -> None:
        self._event()

    def unknown_decl(self, data: str) -> None:
        self._event()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._event()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._event()

    def handle_endtag(self, tag: str) -> None:
        self._event()


def _strip_html_comments(value: str) -> str:
    """Mask HTML comments while preserving all non-comment text and newlines."""
    parser = _HtmlCommentParser(value)
    parser.feed(value)
    parser.close()
    parser.finish()
    if not parser.spans:
        return value
    chars = list(value)
    for start, end in parser.spans:
        for index in range(start, end):
            if chars[index] not in "\r\n":
                chars[index] = " "
    return "".join(chars)


def _section_ranges(body: str) -> tuple[list[tuple[str, int, int]], list[Diagnostic]]:
    lines = body.splitlines()
    headings: list[tuple[str, int]] = []
    for index, line in enumerate(lines):
        match = _H2_RE.match(line)
        if match:
            headings.append((match.group(1).strip(), index))
    diagnostics: list[Diagnostic] = []
    release = [(name, start, headings[pos + 1][1] if pos + 1 < len(headings) else len(lines))
               for pos, (name, start) in enumerate(headings) if name == "Release notes"]
    compatibility = [(name, start, headings[pos + 1][1] if pos + 1 < len(headings) else len(lines))
                     for pos, (name, start) in enumerate(headings) if name == "Compatibility review"]
    if len(release) > 1:
        diagnostics.append(Diagnostic("duplicate-release-notes", "PR body contains repeated Release notes sections", release[1][1] + 1, "Release notes"))
    if len(compatibility) > 1:
        diagnostics.append(Diagnostic("duplicate-compatibility-review", "PR body contains repeated Compatibility review sections", compatibility[1][1] + 1, "Compatibility review"))
    return release[:1] + compatibility[:1], diagnostics


def _placeholder(value: str) -> bool:
    return not value.strip() or bool(_PLACEHOLDER_RE.search(value))


def parse_pr_body(body: str) -> ParsedReleaseNotes:
    """Parse the strict ``## Release notes`` contract from a pull-request body."""
    if not isinstance(body, str):
        return ParsedReleaseNotes(diagnostics=(Diagnostic("body-not-text", "PR body must be text"),))
    lines = body.splitlines()
    ranges, diagnostics = _section_ranges(body)
    release = next((item for item in ranges if item[0] == "Release notes"), None)
    compatibility = next((item for item in ranges if item[0] == "Compatibility review"), None)
    category: str | None = None
    bullets: list[str] = []
    if release is None:
        diagnostics.append(Diagnostic("missing-release-notes", "PR body must contain exactly one ## Release notes section", section="Release notes"))
    else:
        _, start, end = release
        # Permanent HTML guidance comments in the repository template are not
        # contributor content. Remove complete (including multi-line) comment
        # blocks before applying the strict release-note grammar.
        content = _strip_html_comments("\n".join(lines[start + 1:end])).splitlines()
        category_lines: list[tuple[int, str]] = []
        for offset, line in enumerate(content, start=start + 2):
            match = _CATEGORY_RE.match(line)
            if match:
                category_lines.append((offset, match.group(1).strip()))
                continue
            if not line.strip():
                continue
            bullet = _BULLET_RE.match(line)
            if bullet:
                value = bullet.group(1).strip()
                if not value:
                    diagnostics.append(Diagnostic("empty-bullet", "Release-note bullets cannot be empty", offset, "Release notes"))
                elif _placeholder(value):
                    diagnostics.append(Diagnostic("placeholder", "Release-note placeholders must be replaced", offset, "Release notes"))
                else:
                    bullets.append(value)
                continue
            if line.lstrip().startswith("#"):
                diagnostics.append(Diagnostic("unknown-heading", "Only Category and bullet lines are allowed in Release notes", offset, "Release notes"))
            else:
                diagnostics.append(Diagnostic("unexpected-content", "Only Category and bullet lines are allowed in Release notes", offset, "Release notes"))
        if not category_lines:
            diagnostics.append(Diagnostic("missing-category", "Release notes must contain one Category: line", start + 1, "Release notes"))
        elif len(category_lines) > 1:
            diagnostics.append(Diagnostic("duplicate-category", "Release notes must contain one Category: line", category_lines[1][0], "Release notes"))
        else:
            category = category_lines[0][1]
            if category not in ALLOWED_CATEGORIES:
                diagnostics.append(Diagnostic("unknown-category", f"Unknown release-note category: {category!r}", category_lines[0][0], "Release notes"))
            elif category == "None":
                if bullets:
                    diagnostics.append(Diagnostic("none-with-bullets", "Category None must not contain release-note bullets", category_lines[0][0], "Release notes"))
            elif not bullets:
                diagnostics.append(Diagnostic("missing-summary", "Non-None categories require at least one non-placeholder bullet", category_lines[0][0], "Release notes"))
    rationale: str | None = None
    if compatibility is not None:
        _, start, end = compatibility
        values: list[str] = []
        content = _strip_html_comments("\n".join(lines[start + 1:end])).splitlines()
        for offset, line in enumerate(content, start=start + 2):
            if line.lstrip().startswith("#"):
                diagnostics.append(Diagnostic("unknown-heading", "Compatibility review must contain prose or Not applicable", offset, "Compatibility review"))
                continue
            if line.strip():
                values.append(line.strip())
        rationale_text = " ".join(values).strip()
        if _placeholder(rationale_text):
            if rationale_text:
                diagnostics.append(Diagnostic("placeholder", "Compatibility review placeholders must be replaced", start + 2, "Compatibility review"))
            rationale = None
        elif rationale_text.casefold() == "not applicable":
            rationale = None
        else:
            rationale = rationale_text
    return ParsedReleaseNotes(category, tuple(bullets), rationale, tuple(diagnostics))


def validate_pr_body(body: str) -> list[Diagnostic]:
    return list(parse_pr_body(body).diagnostics)


def _normalise_title(title: str) -> str:
    value = " ".join(str(title).split())
    value = value.replace("\u2028", " ").replace("\u2029", " ")
    value = value.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("#", "\\#")
    return value or "Untitled pull request"


def _normalise_url(url: str) -> str:
    """Keep untrusted API metadata inside a valid Markdown link destination."""
    value = "".join(str(url).split())
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "#"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "#"
    # Parentheses and control characters can terminate or inject Markdown links.
    netloc = quote(parsed.netloc, safe="@[]:.-_~")
    return urlunsplit((parsed.scheme, netloc, quote(parsed.path, safe="/%:@!$&'*+,;=-._~"), quote(parsed.query, safe="/?&=:@!$'*,;+-._~%"), quote(parsed.fragment, safe="/?&=:@!$'*,;+-._~%")))


def _record_order(record: PullRequestRecord) -> tuple[datetime, int]:
    text = record.merged_at or "9999-12-31T23:59:59Z"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        parsed = datetime.max.replace(tzinfo=timezone.utc)
    return parsed, record.number


def render_changelog_entry(bundle_version: str, release_date: date, pull_requests: Sequence[PullRequestRecord]) -> str:
    """Render one Keep a Changelog bundle section, sorted by merge time and PR number."""
    grouped: dict[str, list[PullRequestRecord]] = {category: [] for category in RENDER_CATEGORIES}
    for record in sorted(pull_requests, key=_record_order):
        notes = record.release_notes
        if notes.diagnostics:
            raise ValueError(f"PR #{record.number} has invalid release notes: {notes.diagnostics[0].message}")
        if notes.category in grouped:
            grouped[notes.category].append(record)
        elif notes.category == "None":
            continue
        else:
            raise ValueError(f"PR #{record.number} has no valid release-note category")
    output = [f"## [{bundle_version}] - {release_date.isoformat()}", ""]
    for category in RENDER_CATEGORIES:
        records = grouped[category]
        if not records:
            continue
        output.extend([f"### {category}", ""])
        for record in records:
            title = _normalise_title(record.title)
            output.extend([f"#### [#{record.number}]({_normalise_url(record.html_url)}) — {title}", ""])
            output.extend(f"- {bullet}" for bullet in record.release_notes.bullets)
            output.append("")
    return "\n".join(output).rstrip() + "\n"


def insert_changelog_entry(existing: str, entry: str) -> str:
    """Insert an entry after Unreleased, preserving the preamble and existing content."""
    match = re.search(r"(?m)^##\s+\[Unreleased\]\s*$", existing)
    if not match:
        raise ValueError("CHANGELOG.md has no recognisable ## [Unreleased] section")
    version_match = re.search(r"(?m)^##\s+\[([^]]+)\]\s+-\s+", entry)
    if not version_match:
        raise ValueError("Changelog entry must start with a version heading")
    version = version_match.group(1)
    if re.search(rf"(?m)^##\s+\[{re.escape(version)}\](?:\s|$)", existing):
        raise ValueError(f"Changelog already contains bundle version {version}")
    next_heading = re.search(r"(?m)^##\s+", existing[match.end():])
    insertion = match.end() + (next_heading.start() if next_heading else len(existing[match.end():]))
    before, after = existing[:insertion], existing[insertion:]
    before = before.rstrip() + "\n\n"
    after = after.lstrip("\n")
    return before + entry.rstrip() + "\n\n" + after


def _commit_value(raw: Any) -> CommitRecord:
    if isinstance(raw, str):
        return CommitRecord(raw, "")
    sha = str(raw.get("sha") or raw.get("id") or "")
    commit = raw.get("commit") if isinstance(raw, Mapping) else None
    subject = raw.get("subject") if isinstance(raw, Mapping) else ""
    if not subject and isinstance(commit, Mapping):
        subject = str(commit.get("message", "").splitlines()[0] if commit.get("message") else "")
    if not sha:
        raise CollectionError("GitHub commit response omitted a SHA")
    return CommitRecord(sha, str(subject or ""))


def _client_call(client: Any, names: Sequence[str], *args: Any, **kwargs: Any) -> Any:
    for name in names:
        method = getattr(client, name, None)
        if method is not None:
            try:
                return method(*args, **kwargs)
            except TypeError:
                return method(*args)
    raise CollectionError(f"GitHub client does not implement any of: {', '.join(names)}")


def _client_variants(client: Any, names: Sequence[str], variants: Sequence[tuple[Any, ...]]) -> Any:
    """Call a test/client adapter while allowing the documented bootstrap arities."""
    last_error: TypeError | None = None
    for args in variants:
        for name in names:
            method = getattr(client, name, None)
            if method is None:
                continue
            try:
                return method(*args)
            except TypeError as exc:
                last_error = exc
                continue
    if last_error:
        raise CollectionError(f"GitHub client method has an unsupported signature: {last_error}") from last_error
    raise CollectionError(f"GitHub client does not implement any of: {', '.join(names)}")


def _pr_from_api(raw: Mapping[str, Any], body: str | None = None) -> PullRequestRecord:
    base_repo = ((raw.get("base") or {}).get("repo") or {}).get("full_name") if isinstance(raw.get("base"), Mapping) else None
    if raw.get("repository") and isinstance(raw["repository"], Mapping):
        base_repo = raw["repository"].get("full_name") or base_repo
    number = raw.get("number")
    if not isinstance(number, int):
        raise CollectionError("Associated pull request omitted its number")
    merged_at = raw.get("merged_at") or raw.get("mergedAt")
    if not merged_at:
        raise CollectionError(f"PR #{number} is not merged")
    changed = raw.get("changed_files", raw.get("files", ()))
    if not isinstance(changed, (list, tuple)):
        changed = ()
    return PullRequestRecord(number, str(raw.get("title") or ""), str(raw.get("html_url") or raw.get("url") or ""), str(raw.get("merge_commit_sha") or raw.get("mergeCommitSha") or ""), str(merged_at), str(body if body is not None else raw.get("body") or ""), changed_files=tuple(str(item) for item in changed if isinstance(item, str)))


def collect_pull_requests(repository: str, previous_ref: str | None, source_sha: str, client: GitHubClient) -> CollectionResult:
    """Collect merged PR notes and ensure every release-range commit is accounted for."""
    if previous_ref:
        commits_raw = _client_call(client, ("list_range_commits", "compare_commits", "list_commits"), repository, previous_ref, source_sha)
    else:
        commits_raw = _client_variants(
            client,
            ("list_commits", "list_range_commits"),
            ((repository, None, source_sha), (repository, source_sha)),
        )
    commits = tuple(_commit_value(raw) for raw in commits_raw)
    by_number: dict[int, PullRequestRecord] = {}
    accounted: set[str] = set()
    failures: list[str] = []
    for commit in commits:
        associated = _client_call(client, ("associated_pull_requests", "get_commit_pulls", "commit_pull_requests"), repository, commit.sha)
        candidates = associated if isinstance(associated, Sequence) and not isinstance(associated, (str, bytes, Mapping)) else []
        valid: list[Mapping[str, Any]] = []
        for raw in candidates:
            if not isinstance(raw, Mapping):
                continue
            try:
                pr = _pr_from_api(raw)
            except CollectionError:
                continue
            foreign = ((raw.get("base") or {}).get("repo") or {}).get("full_name") if isinstance(raw.get("base"), Mapping) else None
            if foreign and foreign != repository:
                continue
            valid.append(raw)
        if not valid:
            failures.append(f"{commit.sha} {commit.subject}".rstrip())
            continue
        for raw in valid:
            pr = _pr_from_api(raw)
            if not pr.body and hasattr(client, "get_pull"):
                detail = client.get_pull(repository, pr.number)
                if isinstance(detail, Mapping):
                    pr = _pr_from_api(detail, str(detail.get("body") or ""))
            notes = parse_pr_body(pr.body)
            if notes.diagnostics:
                raise CollectionError(f"PR #{pr.number} has invalid release notes: {notes.diagnostics[0].message}")
            previous = by_number.get(pr.number)
            merged_commits = tuple(sorted(set((*((previous.commits if previous else ())), commit.sha))))
            by_number[pr.number] = PullRequestRecord(pr.number, pr.title, pr.html_url, pr.merge_commit_sha, pr.merged_at, pr.body, notes, merged_commits, previous.changed_files if previous else ())
            accounted.add(commit.sha)
    if failures:
        raise CollectionError("Unaccounted release-range commits:\n- " + "\n- ".join(failures))
    # Fetch files only once per deduplicated PR.  A missing adapter method is
    # tolerated for lightweight callers, while the real GhApiClient always
    # implements it and therefore provides provenance for rationale review.
    records_by_number: dict[int, PullRequestRecord] = {}
    for number, record in sorted(by_number.items()):
        files: tuple[str, ...] = ()
        method = next((getattr(client, name, None) for name in ("get_pull_files", "pull_files", "list_pull_files") if getattr(client, name, None) is not None), None)
        if method is not None:
            try:
                raw_files = method(repository, number)
            except TypeError:
                raw_files = method(number)
            values: set[str] = set()
            if isinstance(raw_files, Sequence) and not isinstance(raw_files, (str, bytes, Mapping)):
                for item in raw_files:
                    if isinstance(item, Mapping) and isinstance(item.get("filename"), str) and item["filename"].strip():
                        values.add(item["filename"].strip().lstrip("./"))
            files = tuple(sorted(values))
        records_by_number[number] = PullRequestRecord(record.number, record.title, record.html_url, record.merge_commit_sha, record.merged_at, record.body, record.parsed, record.commits, files)
    records = tuple(sorted(records_by_number.values(), key=_record_order))
    return CollectionResult(records, commits, tuple(commit.sha for commit in commits if commit.sha in accounted))


class GhApiClient:
    """Small ``gh api`` read-only adapter used by the optional CLI."""

    def _api(self, endpoint: str, **params: str) -> Any:
        # ``--paginate --slurp`` makes truncation visible as one complete JSON value;
        # callers flatten endpoint-specific page envelopes below.
        command = ["gh", "api", "--paginate", "--slurp", endpoint]
        for key, value in params.items():
            command.extend(["-f", f"{key}={value}"])
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise CollectionError(f"GitHub CLI request failed: {exc}") from exc
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CollectionError("GitHub CLI returned invalid JSON") from exc

    def list_commits(self, repository: str, *refs: str | None) -> list[Mapping[str, Any]]:
        if len(refs) == 2 and refs[0]:
            data = self._api(f"repos/{repository}/compare/{refs[0]}...{refs[1]}")
            pages = data if isinstance(data, list) else [data]
            commits: list[Mapping[str, Any]] = []
            for page in pages:
                if isinstance(page, Mapping):
                    commits.extend(item for item in page.get("commits", []) if isinstance(item, Mapping))
            # GitHub compare responses are chronological (oldest first).
            return commits
        data = self._api(f"repos/{repository}/commits", sha=refs[-1] or "")
        pages = data if isinstance(data, list) else [data]
        return [item for page in pages for item in (page if isinstance(page, list) else []) if isinstance(item, Mapping)]

    def associated_pull_requests(self, repository: str, sha: str) -> list[Mapping[str, Any]]:
        data = self._api(f"repos/{repository}/commits/{sha}/pulls")
        pages = data if isinstance(data, list) else [data]
        return [item for page in pages for item in (page if isinstance(page, list) else []) if isinstance(item, Mapping)]

    def get_pull(self, repository: str, number: int) -> Mapping[str, Any]:
        data = self._api(f"repos/{repository}/pulls/{number}")
        if isinstance(data, Mapping):
            return data
        if isinstance(data, list):
            for page in data:
                if isinstance(page, Mapping):
                    return page
                if isinstance(page, list):
                    for item in page:
                        if isinstance(item, Mapping):
                            return item
        raise CollectionError(f"GitHub pull request #{number} response was empty")

    def get_pull_files(self, repository: str, number: int) -> list[Mapping[str, Any]]:
        """Return every changed-file record for a pull request."""
        data = self._api(f"repos/{repository}/pulls/{number}/files")
        pages = data if isinstance(data, list) else [data]
        result: list[Mapping[str, Any]] = []
        for page in pages:
            values = page if isinstance(page, list) else page.get("files", []) if isinstance(page, Mapping) else []
            if isinstance(values, list):
                result.extend(item for item in values if isinstance(item, Mapping) and isinstance(item.get("filename"), str))
        return result


def record_to_dict(record: PullRequestRecord) -> dict[str, Any]:
    notes = record.release_notes
    return {"number": record.number, "title": record.title, "html_url": record.html_url, "merge_commit_sha": record.merge_commit_sha, "merged_at": record.merged_at, "body": record.body, "category": notes.category, "bullets": list(notes.bullets), "summary": list(notes.bullets), "compatibility_rationale": notes.compatibility_rationale, "compatibility_review": notes.compatibility_rationale or "Not applicable", "commits": list(record.commits), "changed_files": list(record.changed_files), "files": list(record.changed_files)}


def record_from_dict(raw: Mapping[str, Any]) -> PullRequestRecord:
    bullets = raw.get("bullets", raw.get("summary", ()))
    rationale = raw.get("compatibility_rationale") or raw.get("compatibility_review")
    if rationale == "Not applicable":
        rationale = None
    parsed = ParsedReleaseNotes(raw.get("category"), tuple(bullets), rationale)
    changed = raw.get("changed_files", raw.get("files", ()))
    if not isinstance(changed, (list, tuple)):
        changed = ()
    return PullRequestRecord(int(raw["number"]), str(raw.get("title", "")), str(raw.get("html_url", "")), str(raw.get("merge_commit_sha", "")), str(raw.get("merged_at", "")), str(raw.get("body", "")), parsed, tuple(raw.get("commits", ())), tuple(str(item) for item in changed if isinstance(item, str)))


def bootstrap_changelog(existing: str, *, bundle_version: str, release_date: date | str) -> str:
    """Promote the initial ``[Unreleased]`` body into a first release.

    Legacy content is intentionally not attributed to pull requests.  The
    body is copied byte-for-byte apart from heading and surrounding spacing.
    """
    if isinstance(release_date, str):
        release_date = date.fromisoformat(release_date)
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", bundle_version):
        raise ValueError(f"Invalid bundle version: {bundle_version}")
    if re.search(rf"(?m)^##\s+\[{re.escape(bundle_version)}\](?:\s|$)", existing):
        raise ValueError(f"Changelog already contains bundle version {bundle_version}")
    match = re.search(r"(?m)^##\s+\[Unreleased\]\s*$", existing)
    if not match:
        raise ValueError("CHANGELOG.md has no recognisable ## [Unreleased] section")
    next_heading = re.search(r"(?m)^##\s+", existing[match.end():])
    body_end = match.end() + (next_heading.start() if next_heading else len(existing[match.end():]))
    body = existing[match.end():body_end].lstrip("\r\n")
    tail = existing[body_end:].lstrip("\r\n")
    prefix = existing[:match.start()].rstrip("\r\n")
    result = prefix + "\n\n## [Unreleased]\n\n## [" + bundle_version + "] - " + release_date.isoformat() + "\n\n"
    if body:
        result += body.rstrip("\r\n") + "\n"
    if tail:
        result += "\n" + tail
    return result.rstrip("\r\n") + "\n"


render_bootstrap_changelog = bootstrap_changelog


def promote_changelog(existing: str, *, stable_version: str, previous_prerelease: str, release_date: date | str) -> str:
    """Insert a concise prerelease-to-stable promotion entry."""
    if isinstance(release_date, str):
        release_date = date.fromisoformat(release_date)
    if re.search(rf"(?m)^##\s+\[{re.escape(stable_version)}\](?:\s|$)", existing):
        raise ValueError(f"Changelog already contains bundle version {stable_version}")
    entry = f"## [{stable_version}] - {release_date.isoformat()}\n\n### Changed\n\n- Automated promotion of [{previous_prerelease}] to [{stable_version}].\n"
    return insert_changelog_entry(existing, entry)


promotion_changelog = promote_changelog


def build_parser() -> argparse.ArgumentParser:
    """Build the release-notes CLI parser (also used by help tests)."""
    parser = argparse.ArgumentParser(description="Validate PR notes and render release changelogs. Example: validate-pr --body-file pr.md")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate-pr", help="Validate one PR body. Example: validate-pr --body-file pr.md", description="Check the strict release-note and compatibility sections. Example: validate-pr --body-file pr.md")
    validate.add_argument("--body-file", type=Path, required=True, metavar="PATH", help=help_with_example("Markdown PR body", "--body-file pr.md"))
    validate.add_argument("-v", "--verbose", action="store_true", help=help_with_example("Print diagnostic details", "--verbose"))
    render = sub.add_parser("render", help="Render changelog text from a PR snapshot. Example: render --bundle-version 2.0.0", description="Render one release section without writing files. Example: render --bundle-version 2.0.0")
    render.add_argument("--bundle-version", required=True, help=help_with_example("Release SemVer", "--bundle-version 2.0.0"))
    render.add_argument("--release-date", required=True, help=help_with_example("UTC release date", "--release-date 2026-08-17"))
    render.add_argument("--prs-json", type=Path, required=True, metavar="PATH", help=help_with_example("Collected PR snapshot", "--prs-json pr.json"))
    render.add_argument("-v", "--verbose", action="store_true", help=help_with_example("Print a short summary", "-v"))
    collect = sub.add_parser("collect", help="Collect merged PRs between refs. Example: collect --source-sha abc123", description="Collect and validate release PR metadata with GitHub. Example: collect --source-sha abc123")
    collect.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"), required=not bool(os.environ.get("GITHUB_REPOSITORY")), help=help_with_example("GitHub owner/repo", "--repository owner/repo"))
    collect.add_argument("--previous-ref", metavar="REF", help=help_with_example("Previous release ref", "--previous-ref v1.2.3"))
    collect.add_argument("--source-sha", required=True, help=help_with_example("Current main commit SHA", "--source-sha abc123"))
    collect.add_argument("--output", type=Path, required=True, metavar="PATH", help=help_with_example("PR snapshot output path", "--output pr.json"))
    collect.add_argument("-v", "--verbose", action="store_true", help=help_with_example("Print collection counts", "-v"))
    changelog = sub.add_parser("changelog", help="Insert a release section into CHANGELOG.md. Example: changelog --bundle-version 2.0.0", description="Render and optionally write a release changelog section. Example: changelog --bundle-version 2.0.0")
    changelog.add_argument("--bundle-version", required=True, help=help_with_example("Release SemVer", "--bundle-version 2.0.0"))
    changelog.add_argument("--release-date", required=True, help=help_with_example("UTC release date", "--release-date 2026-08-17"))
    changelog.add_argument("--prs-json", type=Path, required=True, metavar="PATH", help=help_with_example("Collected PR snapshot", "--prs-json pr.json"))
    changelog.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"), metavar="PATH", help=help_with_example("Existing changelog path", "--changelog CHANGELOG.md"))
    changelog.add_argument("--write", action="store_true", help=help_with_example("Write the rendered changelog", "--write"))
    changelog.add_argument("--dry-run", action="store_true", help=help_with_example("Preview without writing (default)", "--dry-run"))
    changelog.add_argument("-v", "--verbose", action="store_true", help=help_with_example("Print a short summary", "-v"))
    bootstrap = sub.add_parser("bootstrap-changelog", help="Create the first release entry. Example: bootstrap-changelog --bundle-version 1.0.0", description="Promote legacy Unreleased content into the first release. Example: bootstrap-changelog --bundle-version 1.0.0")
    bootstrap.add_argument("--bundle-version", required=True, help=help_with_example("Release SemVer", "--bundle-version 1.0.0"))
    bootstrap.add_argument("--release-date", required=True, help=help_with_example("UTC release date", "--release-date 2026-08-17"))
    bootstrap.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"), metavar="PATH", help=help_with_example("Existing changelog path", "--changelog CHANGELOG.md"))
    bootstrap.add_argument("--output", type=Path, required=True, metavar="PATH", help=help_with_example("Rendered changelog path", "--output next.md"))
    promotion = sub.add_parser("promote-changelog", help="Promote a prerelease entry. Example: promote-changelog --bundle-version 1.0.0", description="Create a stable promotion changelog entry. Example: promote-changelog --bundle-version 1.0.0")
    promotion.add_argument("--bundle-version", required=True, help=help_with_example("Stable release SemVer", "--bundle-version 1.0.0"))
    promotion.add_argument("--previous-prerelease", required=True, help=help_with_example("Previous prerelease SemVer", "--previous-prerelease 1.0.0-rc.1"))
    promotion.add_argument("--release-date", required=True, help=help_with_example("UTC release date", "--release-date 2026-08-17"))
    promotion.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"), metavar="PATH", help=help_with_example("Existing changelog path", "--changelog CHANGELOG.md"))
    promotion.add_argument("--output", type=Path, required=True, metavar="PATH", help=help_with_example("Rendered changelog path", "--output next.md"))
    return parser


def _cli(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-pr":
            diagnostics = validate_pr_body(args.body_file.read_text(encoding="utf-8"))
            if diagnostics:
                for diagnostic in diagnostics:
                    print(str(diagnostic))
                return 1
            print("valid")
            return 0
        if args.command == "collect":
            result = collect_pull_requests(args.repository, args.previous_ref, args.source_sha, GhApiClient())
            args.output.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            if args.verbose:
                categories: dict[str, int] = {}
                for record in result.pull_requests:
                    category = record.category or "invalid"
                    categories[category] = categories.get(category, 0) + 1
                summary = ", ".join(f"{key}={value}" for key, value in sorted(categories.items())) or "none"
                print(f"collected {len(result.pull_requests)} pull requests; {len(result.commits)} commits accounted; {summary}")
            return 0
        if args.command == "bootstrap-changelog":
            result = bootstrap_changelog(args.changelog.read_text(encoding="utf-8"), bundle_version=args.bundle_version, release_date=args.release_date)
            args.output.write_text(result, encoding="utf-8")
            print(result, end="")
            return 0
        if args.command == "promote-changelog":
            result = promote_changelog(args.changelog.read_text(encoding="utf-8"), stable_version=args.bundle_version, previous_prerelease=args.previous_prerelease, release_date=args.release_date)
            args.output.write_text(result, encoding="utf-8")
            print(result, end="")
            return 0
        raw = json.loads(args.prs_json.read_text(encoding="utf-8"))
        values = raw.get("pull_requests", raw) if isinstance(raw, Mapping) else raw
        records = [record_from_dict(value) for value in values]
        entry = render_changelog_entry(args.bundle_version, date.fromisoformat(args.release_date), records)
        if args.command == "render":
            print(entry, end="")
        elif args.command == "changelog":
            result = insert_changelog_entry(args.changelog.read_text(encoding="utf-8"), entry)
            if args.write:
                args.changelog.write_text(result, encoding="utf-8")
            else:
                print(result, end="")
        return 0
    except (OSError, ValueError, json.JSONDecodeError, CollectionError) as exc:
        print(f"ERROR: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
