from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import fega_tools.release_workflow as release_workflow
from fega_tools.release_workflow import (
    WorkflowError,
    build_assets,
    extract_changelog_section,
    exact_remote_ref_exists,
    latest_semver_tag,
    normalise_version,
    plan_release,
    prepare_notes,
    remote_tag_commit,
    resolve_release_candidate,
    validate_candidate_chain,
    verify_candidate,
)


def test_plan_helpers_select_valid_semver_and_exact_refs() -> None:
    assert normalise_version("v2.0.0-draft.1") == "2.0.0-draft.1"
    assert latest_semver_tag(["v1.2.0", "v1.1.9", "v2.0.0-rc.1", "v2.0.0", "release/x"]) == "v2.0.0"
    assert exact_remote_ref_exists("refs/tags/v1.2.3", remote_refs=["refs/tags/v1.2.3"])
    assert not exact_remote_ref_exists("refs/tags/v1.2.3", remote_refs=["refs/tags/v1.2.30"])
    assert exact_remote_ref_exists("refs/tags/v1.2.3", remote_refs=["abc123\trefs/tags/v1.2.3"])


def test_remote_tag_commit_prefers_peeled_annotated_tag_in_any_order() -> None:
    direct = "tag-object\trefs/tags/v2.0.0"
    peeled = "r1\trefs/tags/v2.0.0^{}"
    assert remote_tag_commit(f"{direct}\n{peeled}\n", "v2.0.0") == (True, "r1")
    assert remote_tag_commit(f"{peeled}\n{direct}\n", "v2.0.0") == (True, "r1")


def test_candidate_chain_and_manifest_invariants() -> None:
    result = validate_candidate_chain(
        base="base",
        r1="r1",
        r2="r2",
        merge="merge",
        merge_parents=("base", "r2"),
        r1_parent="base",
        r2_parent="r1",
        branch="release/v2.0.0",
        manifest={"tag": "v2.0.0", "bundle_version": "2.0.0", "source_commit": "base"},
    )
    assert result.tag == "v2.0.0"
    assert result.version == "2.0.0"
    with pytest.raises(WorkflowError, match="does not equal R2"):
        validate_candidate_chain(
            base="base", r1="r1", r2="r2", merge="merge", merge_parents=("base", "other"),
            r1_parent="base", r2_parent="r1", branch="release/v2.0.0",
            manifest={"tag": "v2.0.0", "source_commit": "base"},
        )

    with pytest.raises(WorkflowError, match="first parent"):
        validate_candidate_chain(
            base="base", r1="r1", r2="r2", merge="merge", merge_parents=("other", "r2"),
            r1_parent="base", r2_parent="r1", branch="release/v2.0.0",
            manifest={"tag": "v2.0.0", "bundle_version": "2.0.0", "source_commit": "base"},
        )

    with pytest.raises(WorkflowError, match="does not match its bundle version"):
        validate_candidate_chain(
            base="base", r1="r1", r2="r2", merge="merge", merge_parents=("base", "r2"),
            r1_parent="base", r2_parent="r1", branch="release/v2.0.0",
            manifest={"tag": "v2.0.0", "bundle_version": "2.0.1", "source_commit": "base"},
        )


def test_extract_changelog_section_is_precise() -> None:
    content = "# Changelog\n\n## [2.0.0] - 2026-08-17\n\n### Added\n\n- New.\n\n## [1.0.0]\n\n- Old.\n"
    assert extract_changelog_section(content, "v2.0.0") == "### Added\n\n- New.\n"
    with pytest.raises(WorkflowError, match="no section"):
        extract_changelog_section(content, "3.0.0")


def test_plan_release_uses_exact_remote_refs_and_latest_semver(tmp_path: Path) -> None:
    outputs = {
        ("git", "check-ref-format", "--branch", "release/v2.0.0"): "",
        ("git", "ls-remote", "--heads", "origin", "refs/heads/release/v2.0.0"): "",
        ("git", "ls-remote", "--tags", "origin", "refs/tags/v2.0.0"): "",
        ("git", "tag", "--list", "v[0-9]*"): "v1.2.0\nv1.10.0\nnot-a-version\n",
        ("git", "rev-parse", "main"): "source-sha\n",
    }

    def git(args, **_kwargs):
        key = tuple(args)
        return subprocess.CompletedProcess(args, 0, stdout=outputs[key], stderr="")

    result = plan_release(tmp_path, "v2.0.0", release_date="2026-08-17", git=git)
    assert result.previous == "v1.10.0"
    assert result.source == "source-sha"
    assert result.branch == "release/v2.0.0"


def test_prepare_notes_selects_stable_promotion(tmp_path: Path) -> None:
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n", encoding="utf-8")

    class EmptyClient:
        def list_commits(self, repository, previous, source):
            return []

    result = prepare_notes(
        tmp_path,
        repository="owner/repo",
        source="source-sha",
        version="2.0.0",
        release_date="2026-08-17",
        previous="v2.0.0-rc.1",
        snapshot=tmp_path / "prs.json",
        changelog=tmp_path / "next.md",
        client=EmptyClient(),
    )
    assert result.promotion is True
    assert result.bootstrap is False
    assert json.loads(result.snapshot.read_text(encoding="utf-8"))["pull_requests"] == []
    assert "Automated promotion" in result.changelog.read_text(encoding="utf-8")


def _candidate_kwargs() -> dict[str, object]:
    return {
        "base": "base",
        "r1": "r1",
        "r2": "r2",
        "merge": "merge",
        "merge_parents": ("base", "r2"),
        "r1_parent": "base",
        "r2_parent": "r1",
        "branch": "release/v2.0.0",
        "manifest": {"tag": "v2.0.0", "bundle_version": "2.0.0", "source_commit": "base"},
    }


def test_candidate_resolution_distinguishes_absent_and_failed_release() -> None:
    class MissingRelease:
        def release_view(self, tag):
            raise FileNotFoundError(tag)

    result = resolve_release_candidate(**_candidate_kwargs(), release_client=MissingRelease())
    assert result.release_exists is False

    class FailedRelease:
        def release_view(self, tag):
            raise RuntimeError("authentication failed")

    with pytest.raises(RuntimeError, match="authentication failed"):
        resolve_release_candidate(**_candidate_kwargs(), release_client=FailedRelease())
    with pytest.raises(WorkflowError, match="not R1"):
        resolve_release_candidate(**_candidate_kwargs(), remote_tag_commit="other")


def test_build_assets_uses_root_relative_output_and_python_changelog_extraction(tmp_path: Path) -> None:
    manifest = b'{"tag":"v2.0.0"}\n'
    changelog = b"# Changelog\n\n## [2.0.0] - 2026-08-17\n\n### Added\n\n- New.\n"

    def command(args, **kwargs):
        if args[1] == "archive":
            output_arg = next(item for item in args if item.startswith("--output="))
            Path(output_arg.split("=", 1)[1]).write_bytes(b"archive")
            stdout = b"" if kwargs.get("text") is False else ""
        elif args[-1].endswith("release_manifest.json"):
            stdout = manifest
        else:
            stdout = changelog
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr=b"")

    assets = build_assets(tmp_path, r1="r1", version="2.0.0", output_dir="assets", command=command)
    assert assets.source_archive.parent == tmp_path / "assets"
    assert assets.manifest.read_bytes() == manifest
    assert assets.release_notes.read_text(encoding="utf-8") == "### Added\n\n- New.\n"
    assert "release_manifest.json" in assets.checksums.read_text(encoding="utf-8")
    assert (tmp_path / "build/release_manifest.json").read_bytes() == manifest


def test_verify_candidate_cleans_worktree_after_verification_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[tuple[str, ...]] = []

    def command(args, **_kwargs):
        commands.append(tuple(args))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(release_workflow, "verify_release", lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("invalid R1")))
    with pytest.raises(ValueError, match="invalid R1"):
        verify_candidate(tmp_path, r1="r1", r2="r2", tag="v2.0.0", command=command)
    assert any(command[:4] == ("git", "worktree", "remove", "--force") for command in commands)
    assert ("git", "worktree", "prune") in commands
