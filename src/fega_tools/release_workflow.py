"""Small, testable orchestration helpers for the release workflows.

The release domain algorithms live in :mod:`fega_tools.release` and release-note
parsing lives in :mod:`fega_tools.release_notes`.  This module deliberately only
coordinates those algorithms and the handful of git/GitHub reads needed by CI.
The workflow files keep all high-impact writes (commits, tags and ``gh release``
commands) visible; these helpers are consequently safe to exercise with fakes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .release import Version, repository_identity, rewrite_repository_uris, verify_release
from .release_notes import (
    GhApiClient,
    bootstrap_changelog,
    collect_pull_requests,
    insert_changelog_entry,
    promote_changelog,
    record_from_dict,
    render_changelog_entry,
)


class WorkflowError(ValueError):
    """Raised when release orchestration invariants are not satisfied."""


@dataclass(frozen=True)
class ReleasePlan:
    version: str
    tag: str
    branch: str
    previous: str | None
    release_date: str
    source: str

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


@dataclass(frozen=True)
class NotesPlan:
    source: str
    bootstrap: bool
    promotion: bool
    pull_request_count: int
    snapshot: Path
    changelog: Path

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["snapshot"] = str(self.snapshot)
        value["changelog"] = str(self.changelog)
        return value


@dataclass(frozen=True)
class CandidateResolution:
    base: str
    r1: str
    r2: str
    merge: str
    tag: str
    version: str
    branch: str
    tag_exists: bool = False
    release_exists: bool = False
    release_draft: bool = False

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ReleaseAssets:
    source_archive: Path
    manifest: Path
    checksums: Path
    release_notes: Path

    def as_dict(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items()}


def _run(args: Sequence[str], *, cwd: Path | None = None, text: bool = True, check: bool = True) -> subprocess.CompletedProcess[str | bytes]:
    return subprocess.run(list(args), cwd=str(cwd) if cwd else None, check=check, text=text, capture_output=True)


def _output_path(path: str | Path | None, default: Path) -> Path:
    return Path(path) if path is not None else default


def normalise_version(value: str) -> str:
    """Return a validated SemVer without a leading ``v``."""
    version = value.removeprefix("v")
    Version.parse(version)
    return version


def latest_semver_tag(tags: Iterable[str]) -> str | None:
    """Select the greatest valid ``vX.Y.Z`` tag, ignoring unrelated tags."""
    valid: list[tuple[Version, str]] = []
    for tag in tags:
        raw = tag.strip()
        if not raw.startswith("v"):
            continue
        try:
            valid.append((Version.parse(raw[1:]), raw))
        except ValueError:
            continue
    return max(valid)[1] if valid else None


def exact_remote_ref_exists(ref: str, *, remote_refs: Iterable[str]) -> bool:
    """Check an exact ref name, never a prefix or glob match."""
    target = ref.removeprefix("refs/")
    return any(item.strip().split()[-1].removeprefix("refs/") == target for item in remote_refs if item.strip())


def remote_tag_commit(remote_output: str, tag: str) -> tuple[bool, str | None]:
    """Return an exact remote tag's commit, preferring an annotated tag's peeled SHA."""
    direct: str | None = None
    peeled: str | None = None
    for line in remote_output.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        if fields[1] == f"refs/tags/{tag}^{{}}":
            peeled = fields[0]
        elif fields[1] == f"refs/tags/{tag}":
            direct = fields[0]
    return direct is not None or peeled is not None, peeled or direct


def plan_release(
    root: Path,
    requested_version: str,
    *,
    remote: str = "origin",
    repository: str | None = None,
    release_date: str | None = None,
    source: str | None = None,
    git: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
) -> ReleasePlan:
    """Resolve release metadata and assert branch/tag names are available."""
    root = Path(root).resolve()
    run = git or _run
    version = normalise_version(requested_version)
    tag, branch = f"v{version}", f"release/v{version}"
    # ``check-ref-format`` protects both the branch and later git writes.
    run(["git", "check-ref-format", "--branch", branch], cwd=root)
    branch_lines = run(["git", "ls-remote", "--heads", remote, f"refs/heads/{branch}"], cwd=root).stdout
    tag_lines = run(["git", "ls-remote", "--tags", remote, f"refs/tags/{tag}"], cwd=root).stdout
    if isinstance(branch_lines, bytes):
        branch_lines = branch_lines.decode()
    if isinstance(tag_lines, bytes):
        tag_lines = tag_lines.decode()
    if str(branch_lines).strip():
        raise WorkflowError(f"Remote branch already exists: {branch}")
    if str(tag_lines).strip():
        raise WorkflowError(f"Remote tag already exists: {tag}")
    local_tags = run(["git", "tag", "--list", "v[0-9]*"], cwd=root).stdout
    if isinstance(local_tags, bytes):
        local_tags = local_tags.decode()
    previous = latest_semver_tag(str(local_tags).splitlines())
    if source is None:
        source_value = run(["git", "rev-parse", "main"], cwd=root).stdout
        source = source_value.decode() if isinstance(source_value, bytes) else str(source_value)
        source = source.strip()
    release_date = release_date or datetime.now(timezone.utc).date().isoformat()
    date.fromisoformat(release_date)
    # Repository identity is intentionally resolved here: a malformed origin
    # should fail during planning rather than halfway through materialisation.
    if repository is not None:
        repository_identity(root, repository)
    return ReleasePlan(version, tag, branch, previous, release_date, source)


def write_outputs(values: Mapping[str, object], destination: str | Path | None = None) -> None:
    """Write simple key/value outputs in GitHub's output-file format."""
    target = destination or os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    path = Path(target)
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            rendered = "" if value is None else (
                str(value).lower() if isinstance(value, bool) else str(value)
            )
            handle.write(f"{key}={rendered}\n")


def _is_stable_promotion(previous: str | None, current: str, public_count: int) -> bool:
    if not previous or public_count:
        return False
    try:
        old = Version.parse(previous.removeprefix("v"))
        new = Version.parse(current.removeprefix("v"))
    except ValueError:
        return False
    return (old.major, old.minor, old.patch) == (new.major, new.minor, new.patch) and bool(old.prerelease) and not bool(new.prerelease)


def prepare_notes(
    root: Path,
    *,
    repository: str,
    source: str,
    version: str,
    release_date: str,
    previous: str | None = None,
    snapshot: str | Path = "/tmp/prs.json",
    changelog: str | Path = "/tmp/CHANGELOG.next.md",
    client: Any | None = None,
) -> NotesPlan:
    """Collect PRs and render the appropriate normal, promotion, or bootstrap notes."""
    root = Path(root).resolve()
    snapshot_path, changelog_path = Path(snapshot), Path(changelog)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    changelog_path.parent.mkdir(parents=True, exist_ok=True)
    changelog_source = root / "CHANGELOG.md"
    existing = changelog_source.read_text(encoding="utf-8") if changelog_source.is_file() else "# Changelog\n"
    bootstrap = previous is None
    promotion = False
    if bootstrap:
        result = {"pull_requests": [], "commits": [], "accounted_commits": []}
        rendered = bootstrap_changelog(existing, bundle_version=version, release_date=release_date)
    else:
        collected = collect_pull_requests(repository, previous, source, client or GhApiClient())
        result = collected.to_dict()
        public_count = sum(1 for item in collected.pull_requests if item.category != "None")
        promotion = _is_stable_promotion(previous, version, public_count)
        if promotion:
            rendered = promote_changelog(existing, stable_version=version, previous_prerelease=previous.removeprefix("v"), release_date=release_date)
        else:
            records = [record_from_dict(value) for value in result["pull_requests"]]
            entry = render_changelog_entry(version, date.fromisoformat(release_date), records)
            rendered = insert_changelog_entry(existing, entry)
    snapshot_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    changelog_path.write_text(rendered, encoding="utf-8")
    return NotesPlan(source, bootstrap, promotion, len(result.get("pull_requests", [])), snapshot_path, changelog_path)


def _expect_sha(value: str, name: str) -> str:
    if not value or value == "null":
        raise WorkflowError(f"Missing {name} commit SHA")
    return value


def validate_candidate_chain(
    *,
    base: str,
    r1: str,
    r2: str,
    merge: str,
    merge_parents: Sequence[str],
    r1_parent: str,
    r2_parent: str,
    branch: str,
    manifest: Mapping[str, Any],
    expected_tag: str | None = None,
) -> CandidateResolution:
    """Validate the exact two-commit candidate chain and manifest invariants."""
    for value, name in ((base, "base"), (r1, "R1"), (r2, "R2"), (merge, "merge")):
        _expect_sha(value, name)
    if len(merge_parents) != 2:
        raise WorkflowError("Merged release PR must have exactly two parents")
    if merge_parents[0] != base:
        raise WorkflowError(f"Merge first parent {merge_parents[0]} does not equal base {base}")
    if r1_parent != base:
        raise WorkflowError(f"R1 parent {r1_parent} does not equal base {base}")
    if r2_parent != r1:
        raise WorkflowError(f"R2 parent {r2_parent} does not equal R1 {r1}")
    if merge_parents[1] != r2:
        raise WorkflowError(f"Merge second parent {merge_parents[1]} does not equal R2 {r2}")
    tag = str(manifest.get("tag", ""))
    version = str(manifest.get("bundle_version", manifest.get("version", "")))
    source = str(manifest.get("source_commit", ""))
    try:
        parsed_version = str(Version.parse(version))
    except ValueError as exc:
        raise WorkflowError("R1 manifest contains an invalid bundle version") from exc
    if tag != f"v{parsed_version}":
        raise WorkflowError("R1 manifest tag does not match its bundle version")
    if source != base:
        raise WorkflowError(f"R1 manifest source_commit {source} does not equal base {base}")
    if branch != f"release/{tag}":
        raise WorkflowError(f"Release branch {branch} does not match manifest tag {tag}")
    if expected_tag and expected_tag.removeprefix("v") != tag.removeprefix("v"):
        raise WorkflowError(f"Expected tag {expected_tag} does not match manifest tag {tag}")
    return CandidateResolution(base, r1, r2, merge, tag, parsed_version, branch)


def _gh_release_state(client: Any, tag: str) -> tuple[bool, bool]:
    """Read release state while distinguishing a real 404 from other errors."""
    try:
        value = client.release_view(tag) if hasattr(client, "release_view") else client.view_release(tag)
    except Exception as exc:  # pragma: no cover - concrete adapters vary
        if isinstance(exc, FileNotFoundError):
            return False, False
        status = getattr(exc, "status", getattr(exc, "returncode", None))
        text = str(exc).lower()
        if status == 404 or "404" in text or "not found" in text:
            return False, False
        raise
    if value is None:
        return False, False
    if isinstance(value, str):
        value = json.loads(value)
    return True, bool(value.get("isDraft", value.get("draft", False)))


def resolve_release_candidate(
    *,
    base: str,
    r1: str,
    r2: str,
    merge: str,
    merge_parents: Sequence[str],
    r1_parent: str,
    r2_parent: str,
    branch: str,
    manifest: Mapping[str, Any],
    expected_tag: str | None = None,
    remote_tag_commit: str | None = None,
    release_client: Any | None = None,
) -> CandidateResolution:
    result = validate_candidate_chain(base=base, r1=r1, r2=r2, merge=merge, merge_parents=merge_parents, r1_parent=r1_parent, r2_parent=r2_parent, branch=branch, manifest=manifest, expected_tag=expected_tag)
    if remote_tag_commit is not None and remote_tag_commit != r1:
        raise WorkflowError(f"Existing remote tag {result.tag} resolves to {remote_tag_commit}, not R1 {r1}")
    if release_client is not None:
        exists, draft = _gh_release_state(release_client, result.tag)
        result = CandidateResolution(**{**result.as_dict(), "release_exists": exists, "release_draft": draft})
    return result


def _json_command(command: Sequence[str], *, cwd: Path) -> Any:
    result = _run(command, cwd=cwd)
    raw = result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout
    return json.loads(str(raw))


def resolve_from_github(
    root: Path,
    *,
    repository: str,
    pull_request: int,
    expected_tag: str | None = None,
    remote: str = "origin",
) -> CandidateResolution:
    """Resolve a merged release PR using read-only ``gh``/git calls."""
    root = Path(root).resolve()
    pr = _json_command(["gh", "api", f"repos/{repository}/pulls/{pull_request}"], cwd=root)
    if pr.get("base", {}).get("ref") != "main" or pr.get("merged") is not True:
        raise WorkflowError("Release PR must be merged into main")
    branch = str(pr.get("head", {}).get("ref", ""))
    if not branch.startswith("release/v"):
        raise WorkflowError(f"Release PR head must be a release/v* branch (got {branch!r})")
    merge = _expect_sha(str(pr.get("merge_commit_sha", "")), "merge")
    commit = _json_command(["gh", "api", f"repos/{repository}/commits/{merge}"], cwd=root)
    merge_parents = [str(item.get("sha", "")) for item in commit.get("parents", [])]
    commits = _json_command(["gh", "api", "--paginate", f"repos/{repository}/pulls/{pull_request}/commits"], cwd=root)
    if not isinstance(commits, list) or len(commits) != 2:
        raise WorkflowError("Release PR must contain exactly two candidate commits")
    r1, r2 = (str(commits[0].get("sha", "")), str(commits[1].get("sha", "")))
    base = merge_parents[0] if merge_parents else ""
    for sha in (base, r1, r2, merge):
        _run(["git", "fetch", remote, sha], cwd=root)
    r1_parent = str(_run(["git", "rev-parse", f"{r1}^"], cwd=root).stdout).strip()
    r2_parent = str(_run(["git", "rev-parse", f"{r2}^"], cwd=root).stdout).strip()
    manifest_raw = _run(["git", "show", f"{r1}:build/release_manifest.json"], cwd=root).stdout
    manifest = json.loads(manifest_raw.decode() if isinstance(manifest_raw, bytes) else str(manifest_raw))
    remote_result = _run(["git", "ls-remote", "--tags", remote, f"refs/tags/{manifest.get('tag', '')}", f"refs/tags/{manifest.get('tag', '')}^{{}}"], cwd=root)
    remote_text = remote_result.stdout.decode() if isinstance(remote_result.stdout, bytes) else str(remote_result.stdout)
    remote_tag_seen, existing_tag_commit = remote_tag_commit(remote_text, str(manifest.get("tag", "")))
    result = resolve_release_candidate(base=base, r1=r1, r2=r2, merge=merge, merge_parents=merge_parents, r1_parent=r1_parent, r2_parent=r2_parent, branch=branch, manifest=manifest, expected_tag=expected_tag, remote_tag_commit=existing_tag_commit)
    tag_exists = remote_tag_seen
    release_exists, release_draft = _gh_release_state(GhCliClient(repository), result.tag)
    if release_exists and not release_draft:
        raise WorkflowError(f"Existing GitHub release {result.tag} is not a draft")
    return CandidateResolution(**{**result.as_dict(), "tag_exists": tag_exists, "release_exists": release_exists, "release_draft": release_draft})


class GhCliClient:
    """Tiny adapter used by :func:`resolve_from_github` and easy to fake."""

    def __init__(self, repository: str):
        self.repository = repository

    def release_view(self, tag: str) -> Mapping[str, Any]:
        result = subprocess.run(
            ["gh", "release", "view", tag, "--repo", self.repository, "--json", "isDraft,tagName"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            message = f"{result.stdout}\n{result.stderr}".lower()
            if "404" in message or "not found" in message:
                raise FileNotFoundError(tag)
            raise WorkflowError(f"Unable to inspect GitHub release {tag}: {result.stderr.strip()}")
        return json.loads(result.stdout)


def verify_candidate(
    root: Path,
    *,
    r1: str,
    r2: str,
    tag: str,
    repository: str | None = None,
    command: Callable[..., Any] | None = None,
) -> None:
    """Verify R1 and compare its main-normalised tree to R2, always cleaning up."""
    root = Path(root).resolve()
    run = command or _run
    worktree = Path(tempfile.mkdtemp(prefix="fega-release-verify-"))
    try:
        run(["git", "worktree", "add", "--detach", str(worktree), r1], cwd=root)
        verify_release(worktree, mode="release", expected_ref=tag, repository=repository)
        rewrite_repository_uris(worktree, tag, "main", repository=repository)
        result = run(["git", "diff", "--exit-code", r2, "--"], cwd=worktree, check=False)
        if getattr(result, "returncode", 0) != 0:
            raise WorkflowError("Normalised R1 does not match R2")
    finally:
        try:
            run(["git", "worktree", "remove", "--force", str(worktree)], cwd=root, check=False)
        finally:
            run(["git", "worktree", "prune"], cwd=root, check=False)
            shutil.rmtree(worktree, ignore_errors=True)


def extract_changelog_section(changelog: str, version: str) -> str:
    """Extract one ``## [version]`` section without fragile shell ``awk``."""
    target = version.removeprefix("v")
    lines = changelog.splitlines(keepends=True)
    heading = re.compile(r"^##\s+\[([^]]+)\](?:\s|$)")
    start: int | None = None
    end = len(lines)
    for index, line in enumerate(lines):
        match = heading.match(line)
        if not match:
            continue
        if start is not None:
            end = index
            break
        if match.group(1) == target:
            start = index + 1
    if start is None:
        raise WorkflowError(f"Changelog has no section for {target}")
    section = "".join(lines[start:end]).strip("\n")
    if not section.strip():
        raise WorkflowError(f"Changelog section for {target} is empty")
    return section + "\n"


def build_assets(
    root: Path,
    *,
    r1: str,
    version: str,
    output_dir: str | Path = "release-assets",
    command: Callable[..., Any] | None = None,
) -> ReleaseAssets:
    """Build deterministic archive, manifest, checksums, and release notes."""
    root = Path(root).resolve()
    output = Path(output_dir)
    if not output.is_absolute():
        output = root / output
    output.mkdir(parents=True, exist_ok=True)
    run = command or _run
    version = normalise_version(version)
    archive = output / f"fega-metadata-schema-{version}.zip"
    run(["git", "archive", "--format=zip", f"--prefix=fega-metadata-schema-{version}/", f"--output={archive}", r1], cwd=root)
    manifest = output / "release_manifest.json"
    manifest.write_bytes(run(["git", "show", f"{r1}:build/release_manifest.json"], cwd=root, text=False).stdout)  # type: ignore[arg-type]
    checksums = output / "SHA256SUMS"
    checksums.write_text("".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in (archive, manifest)), encoding="utf-8")
    changelog_bytes = run(["git", "show", f"{r1}:CHANGELOG.md"], cwd=root, text=False).stdout
    changelog = changelog_bytes.decode() if isinstance(changelog_bytes, bytes) else str(changelog_bytes)
    notes = output / "release-notes.md"
    notes.write_text(extract_changelog_section(changelog, version), encoding="utf-8")
    # Publication historically uploads the manifest from build/ as well as the
    # copy in release-assets.  Keep that compatibility while returning the
    # deterministic asset path used by tests and callers.
    local_manifest = root / "build/release_manifest.json"
    local_manifest.parent.mkdir(parents=True, exist_ok=True)
    local_manifest.write_bytes(manifest.read_bytes())
    return ReleaseAssets(archive, manifest, checksums, notes)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan and test release-workflow phases. Example: plan --version 2.0.0")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser(
        "plan",
        help="Validate a release version and choose the previous tag. Example: plan --version 2.0.0",
        description="Validate release inputs and calculate the candidate branch, tag, date, source, and previous release. Example: plan --version 2.0.0",
    )
    plan.add_argument("--version", required=True, help="Release SemVer, such as 2.0.0. Example: --version 2.0.0")
    plan.add_argument("--root", type=Path, default=Path.cwd(), help="Repository checkout. Example: --root .")
    plan.add_argument("--output", type=Path, help="Optional JSON output path. Example: --output plan.json")
    notes = sub.add_parser(
        "notes",
        help="Collect PRs and prepare the next changelog. Example: notes --version 2.0.0",
        description="Collect merged PR metadata and prepare normal, promotion, or bootstrap changelog files. Example: notes --version 2.0.0",
    )
    notes.add_argument("--root", type=Path, default=Path.cwd(), help="Repository checkout. Example: --root .")
    notes.add_argument("--repository", required=True, help="GitHub repository owner/repo. Example: --repository owner/repo")
    notes.add_argument("--version", required=True, help="Release SemVer. Example: --version 2.0.0")
    notes.add_argument("--source", required=True, help="Main commit SHA. Example: --source abc123")
    notes.add_argument("--release-date", required=True, help="UTC release date. Example: --release-date 2026-08-17")
    notes.add_argument("--previous", help="Previous release tag, if any. Example: --previous v1.2.3")
    notes.add_argument("--snapshot", type=Path, default=Path("/tmp/prs.json"), help="PR snapshot path. Example: --snapshot pr.json")
    notes.add_argument("--changelog", type=Path, default=Path("/tmp/CHANGELOG.next.md"), help="Rendered changelog path. Example: --changelog CHANGELOG.next.md")
    assets = sub.add_parser(
        "assets",
        help="Build release archive and checksums. Example: assets --r1 abc123 --version 2.0.0",
        description="Build the source archive, release manifest, checksums, and release notes from R1. Example: assets --r1 abc123 --version 2.0.0",
    )
    assets.add_argument("--root", type=Path, default=Path.cwd(), help="Repository checkout. Example: --root .")
    assets.add_argument("--r1", required=True, help="R1 candidate SHA. Example: --r1 abc123")
    assets.add_argument("--version", required=True, help="Release SemVer. Example: --version 2.0.0")
    assets.add_argument("--output-dir", type=Path, default=Path("release-assets"), help="Asset directory. Example: --output-dir release-assets")
    resolve = sub.add_parser("resolve", help="Resolve and validate a merged release PR. Example: resolve --repository owner/repo --pull-request 123", description="Validate the exact base/R1/R2/merge chain and existing remote state. Example: resolve --repository owner/repo --pull-request 123")
    resolve.add_argument("--root", type=Path, default=Path.cwd(), help="Repository checkout. Example: --root .")
    resolve.add_argument("--repository", required=True, help="GitHub owner/repo. Example: --repository owner/repo")
    resolve.add_argument("--pull-request", type=int, required=True, help="Merged release PR number. Example: --pull-request 123")
    resolve.add_argument("--expected-tag", help="Optional expected tag. Example: --expected-tag v2.0.0")
    verify = sub.add_parser("verify-candidate", help="Verify R1 and the normalised R2 tree. Example: verify-candidate --r1 abc123 --r2 def456 --tag v2.0.0", description="Check release invariants in a temporary detached worktree and clean it up. Example: verify-candidate --r1 abc123 --r2 def456 --tag v2.0.0")
    verify.add_argument("--root", type=Path, default=Path.cwd(), help="Repository checkout. Example: --root .")
    verify.add_argument("--r1", required=True, help="R1 candidate SHA. Example: --r1 abc123")
    verify.add_argument("--r2", required=True, help="R2 candidate SHA. Example: --r2 def456")
    verify.add_argument("--tag", required=True, help="Release tag. Example: --tag v2.0.0")
    verify.add_argument("--repository", help="GitHub owner/repo. Example: --repository owner/repo")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            plan = plan_release(args.root, args.version)
            result = plan.as_dict()
            write_outputs(result)
            rendered = json.dumps(result, indent=2) + "\n"
            if args.output:
                args.output.write_text(rendered, encoding="utf-8")
            print(rendered, end="")
        elif args.command == "notes":
            notes = prepare_notes(args.root, repository=args.repository, source=args.source, version=args.version, release_date=args.release_date, previous=args.previous, snapshot=args.snapshot, changelog=args.changelog)
            write_outputs({"source": notes.source, "bootstrap": notes.bootstrap, "promotion": notes.promotion, "pr_count": notes.pull_request_count})
            print(json.dumps(notes.as_dict(), indent=2))
        elif args.command == "assets":
            print(json.dumps(build_assets(args.root, r1=args.r1, version=args.version, output_dir=args.output_dir).as_dict(), indent=2))
        elif args.command == "resolve":
            result = resolve_from_github(args.root, repository=args.repository, pull_request=args.pull_request, expected_tag=args.expected_tag)
            write_outputs(result.as_dict())
            print(json.dumps(result.as_dict(), indent=2))
        else:
            verify_candidate(args.root, r1=args.r1, r2=args.r2, tag=args.tag, repository=args.repository)
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=os.sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
