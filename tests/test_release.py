from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from fega_tools.release import (
    Version,
    associate_pr_rationales,
    analyse_release,
    create_manifest,
    discover_components,
    discover_standard_groups,
    prepare_release,
    repository_identity,
    rewrite_repository_uris,
    verify_release,
    write_json,
)
from fega_tools.release_policy import check_release_policy
from fega_tools.schema_diff import Severity
from fega_tools.release_notes import PullRequestRecord


REPOSITORY = "owner/repository"


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _repo(root: Path, version: str = "1.0.0") -> None:
    schema = {
        "$id": f"https://raw.githubusercontent.com/{REPOSITORY}/main/schemas/widget/schema.json",
        "meta:version": version,
        "type": "object",
        "properties": {"name": {"type": "string"}},
    }
    _write(root / "schemas/widget/schema.json", schema)
    (root / "schemas/widget/context.jsonld").write_text('{"@context": {}}\n', encoding="utf-8")
    (root / "schemas/widget/frame.jsonld").write_text('{"@context": "https://raw.githubusercontent.com/owner/repository/main/schemas/widget/context.jsonld"}\n', encoding="utf-8")
    _write(root / "standards/json-schema/example/schema.json", {"type": "string"})
    (root / "CITATION.cff").write_text("cff-version: 1.2.0\nversion: 0.0.0\n", encoding="utf-8")
    (root / "build").mkdir(parents=True, exist_ok=True)
    (root / "build/release_manifest.schema.json").write_text((Path(__file__).parents[1] / "build/release_manifest.schema.json").read_text(), encoding="utf-8")


def test_semver_prerelease_precedence_and_bumps() -> None:
    assert Version.parse("2.0.0-draft.1") < Version.parse("2.0.0")
    assert Version.parse("2.0.0-alpha") < Version.parse("2.0.0-alpha.1")
    assert str(Version.parse("1.2.3").bump(Severity.MINOR)) == "1.3.0"
    with pytest.raises(ValueError):
        Version.parse("1.2")


def test_repository_identity_cli_environment_and_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert repository_identity(tmp_path, REPOSITORY) == REPOSITORY
    monkeypatch.setenv("GITHUB_REPOSITORY", "fork/schema")
    assert repository_identity(tmp_path) == "fork/schema"
    monkeypatch.delenv("GITHUB_REPOSITORY")
    with pytest.raises(ValueError, match="Cannot resolve repository"):
        repository_identity(tmp_path)


def test_discovery_and_standard_group_hashes(tmp_path: Path) -> None:
    _repo(tmp_path)
    components = discover_components(tmp_path)
    assert [item.name for item in components] == ["widget"]
    groups = discover_standard_groups(tmp_path)
    assert groups[0]["name"] == "example"
    before = groups[0]["sha256"]
    (tmp_path / "standards/json-schema/example/second.txt").write_text("x", encoding="utf-8")
    assert discover_standard_groups(tmp_path)[0]["sha256"] != before


def test_prepare_manifest_and_two_uri_states(tmp_path: Path) -> None:
    _repo(tmp_path)
    (tmp_path / "CITATION.cff").write_text("cff-version: 1.2.0\nversion: 2.0.0-draft.1\n", encoding="utf-8")
    rewrite_repository_uris(tmp_path, "main", "v2.0.0-draft.1", repository=REPOSITORY)
    manifest = create_manifest(tmp_path, bundle_version="2.0.0-draft.1", repository=REPOSITORY, source_commit="abcdef0", release_date="2026-08-06")
    write_json(tmp_path / "build/release_manifest.json", manifest)
    verify_release(tmp_path, mode="tag", expected_ref="v2.0.0-draft.1", repository=REPOSITORY)
    rewrite_repository_uris(tmp_path, "v2.0.0-draft.1", "main", repository=REPOSITORY)
    # Development verification permits a manifest inventory for the release
    # while live first-party URIs point back to main.
    verify_release(tmp_path, mode="development", repository=REPOSITORY)


def test_version_assertion_and_rationale_hook(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    current = tmp_path / "current"
    _repo(previous, "1.0.0")
    _repo(current, "1.0.0")
    schema_path = current / "schemas/widget/schema.json"
    schema = json.loads(schema_path.read_text())
    schema["patternProperties"] = {"^x": {"type": "string"}}
    _write(schema_path, schema)
    report = analyse_release(current, previous, repository=REPOSITORY)
    assert any("unchanged" in error for error in report["errors"])
    schema["meta:version"] = "2.0.0"
    _write(schema_path, schema)
    report = analyse_release(current, previous, repository=REPOSITORY, approved_rationales={"widget": "reviewed"})
    assert report["errors"] == []


def test_rationale_association_requires_affected_changed_path(tmp_path: Path) -> None:
    root = tmp_path / "current"
    _repo(root, "1.0.0")
    body = "## Release notes\n\nCategory: Changed\n\n- Review schema.\n\n## Compatibility review\n\nThe change is backwards compatible because it is optional.\n"
    direct = PullRequestRecord(123, "Schema", "https://github.com/o/r/pull/123", "sha", "2026-08-06T00:00:00Z", body, changed_files=("schemas/widget/schema.json",))
    unrelated = PullRequestRecord(124, "Docs", "https://github.com/o/r/pull/124", "sha2", "2026-08-06T00:00:00Z", body, changed_files=("docs/README.md",))
    assert associate_pr_rationales(root, [unrelated]) == {}
    assert associate_pr_rationales(root, [direct]) == {"widget": "PR #123: The change is backwards compatible because it is optional."}


def test_nested_standard_dependency_is_reachable(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    current = tmp_path / "current"
    _repo(previous, "1.0.0")
    _repo(current, "1.0.1")
    for root in (previous, current):
        _write(root / "standards/json-schema/example/inner.json", {"type": "string"})
        _write(root / "standards/json-schema/example/outer.json", {"$ref": "inner.json"})
        schema_path = root / "schemas/widget/schema.json"
        schema = json.loads(schema_path.read_text())
        schema["properties"] = {"value": {"$ref": "../../standards/json-schema/example/outer.json"}}
        _write(schema_path, schema)
    _write(current / "standards/json-schema/example/inner.json", {"type": "number"})
    report = analyse_release(current, previous, repository=REPOSITORY, approved_rationales={"widget": "reviewed"})
    component = report["components"][0]
    assert "standard:standards/json-schema/example" in component["dependencies"]
    assert any("reachable standard group" in detail["message"] for detail in component["details"])


def test_policy_has_no_fragment_or_config_dependency(tmp_path: Path) -> None:
    assert check_release_policy(tmp_path, tmp_path, ["schemas/widget/schema.json"]) == []
    assert check_release_policy(tmp_path, tmp_path, [".changes/one.json"])
    assert check_release_policy(tmp_path, tmp_path, ["release.toml"])


def test_initial_changelog_is_allowed_as_bootstrap_input(tmp_path: Path) -> None:
    base = tmp_path / "base"
    head = tmp_path / "head"
    head.mkdir(parents=True)
    (head / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n", encoding="utf-8")
    assert check_release_policy(base, head, ["CHANGELOG.md"]) == []


def test_existing_changelog_remains_protected(tmp_path: Path) -> None:
    base = tmp_path / "base"
    head = tmp_path / "head"
    base.mkdir(parents=True)
    head.mkdir(parents=True)
    (base / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n", encoding="utf-8")
    (head / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n\nChanged\n", encoding="utf-8")
    errors = check_release_policy(base, head, ["CHANGELOG.md"])
    assert errors == ["Ordinary PRs must not edit generated CHANGELOG.md"]


def test_removed_component_is_analysis_only_and_manifest_has_no_history(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    current = tmp_path / "current"
    _repo(previous, "1.0.0")
    _repo(current, "1.0.0")
    (current / "schemas/widget/schema.json").unlink()
    report = analyse_release(current, previous, repository=REPOSITORY, requested_version="2.0.0")
    assert report["removed_components"]
    assert report["errors"] == []
    manifest = create_manifest(current, bundle_version="2.0.0", repository=REPOSITORY, source_commit="abcdef0")
    assert "status" not in manifest
    assert "removed_components" not in manifest


def test_release_identity_pairs_components_across_repository_and_ref_changes(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    current = tmp_path / "current"
    _repo(previous, "1.0.0")
    _repo(current, "1.0.1")
    schema_path = current / "schemas/widget/schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema["$id"] = schema["$id"].replace(
        "owner/repository/main",
        "fork/project/refs/heads/feature",
    )
    _write(schema_path, schema)

    report = analyse_release(
        current,
        previous,
        repository=REPOSITORY,
        approved_rationales={"widget": "Reviewed repository migration."},
    )

    assert report["removed_components"] == []
    assert report["components"][0]["previous_version"] == "1.0.0"


def test_fork_uri_rewrite_and_bootstrap_verification(tmp_path: Path) -> None:
    _repo(tmp_path)
    path = tmp_path / "schemas/widget/schema.json"
    text = path.read_text().replace("owner/repository/main", "upstream/project/main")
    path.write_text(text, encoding="utf-8")
    assert rewrite_repository_uris(tmp_path, "main", "v1.0.0", repository=REPOSITORY) > 0
    verify_release(tmp_path, mode="development", repository=REPOSITORY)


def test_release_uri_rewrite_filters_source_ref_and_canonicalises_prefix(tmp_path: Path) -> None:
    _repo(tmp_path)
    path = tmp_path / "schemas/widget/schema.json"
    path.write_text(
        path.read_text().replace(
            "owner/repository/main/schemas/widget/schema.json",
            "fork/project/refs/heads/main/schemas/widget/schema.json?download=1#schema",
        ),
        encoding="utf-8",
    )
    assert rewrite_repository_uris(tmp_path, "refs/heads/main", "refs/tags/v1.0.0", repository=REPOSITORY) > 0
    rewritten = path.read_text(encoding="utf-8")
    assert "https://raw.githubusercontent.com/owner/repository/v1.0.0/" in rewritten
    assert "schemas/widget/schema.json?download=1#schema" in rewritten

    path.write_text(rewritten.replace("/v1.0.0/", "/refs/tags/v1.0.0/"), encoding="utf-8")
    assert rewrite_repository_uris(tmp_path, "main", "v2.0.0", repository=REPOSITORY) == 0
    assert "/refs/tags/v1.0.0/" in path.read_text(encoding="utf-8")


def test_release_uri_rewrite_ignores_non_local_and_traversal_paths(tmp_path: Path) -> None:
    _repo(tmp_path)
    (tmp_path / "schemas/widget/schema.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "build/release_manifest.schema.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "outside.json").write_text("{}\n", encoding="utf-8")
    path = tmp_path / "schemas/widget/frame.jsonld"
    original = "\n".join(
        [
            "https://example.org/schemas/widget/schema.json",
            "https://raw.githubusercontent.com/owner/repository/main/docs/missing.json",
            "https://raw.githubusercontent.com/owner/repository/main/schemas/missing.json",
            "https://raw.githubusercontent.com/owner/repository/main/schemas/../outside.json",
        ]
    ) + "\n"
    path.write_text(original, encoding="utf-8")

    assert rewrite_repository_uris(tmp_path, "main", "v1.0.0", repository=REPOSITORY) == 0
    assert path.read_text(encoding="utf-8") == original


def test_cli_smoke_paths(tmp_path: Path) -> None:
    _repo(tmp_path)
    repository = Path(__file__).parents[1]
    env = {"PYTHONPATH": str(repository / "src")}
    result = subprocess.run([sys.executable, "scripts/py/release.py", "--root", str(tmp_path), "discover"], cwd=repository, env=env, capture_output=True, text=True)
    assert result.returncode == 0


def test_cli_semver_previous_ref_uses_archived_tree(tmp_path: Path) -> None:
    _repo(tmp_path, "1.0.0")
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.org"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "remote", "add", "origin", "https://github.com/owner/repository.git"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "initial"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "tag", "v1.0.0"], check=True)
    schema_path = tmp_path / "schemas/widget/schema.json"
    schema = json.loads(schema_path.read_text())
    schema["description"] = "changed"
    schema["meta:version"] = "1.0.1"
    _write(schema_path, schema)
    repository = Path(__file__).parents[1]
    result = subprocess.run([sys.executable, "scripts/py/release.py", "--root", str(tmp_path), "semver", "--all", "--previous-ref", "v1.0.0"], cwd=repository, env={"PYTHONPATH": str(repository / "src")}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert '"widget"' in result.stdout


def test_cli_analyse_fails_when_declared_version_is_too_low(tmp_path: Path) -> None:
    previous = tmp_path / "previous"
    current = tmp_path / "current"
    _repo(previous, "1.0.0")
    _repo(current, "1.0.0")
    schema_path = current / "schemas/widget/schema.json"
    schema = json.loads(schema_path.read_text())
    schema["description"] = "changed"
    _write(schema_path, schema)
    repository = Path(__file__).parents[1]
    result = subprocess.run([sys.executable, "scripts/py/release.py", "--root", str(current), "analyse", "--previous", str(previous), "--repository", REPOSITORY], cwd=repository, env={"PYTHONPATH": str(repository / "src")}, capture_output=True, text=True)
    assert result.returncode == 2
    assert "meta:version is unchanged" in result.stdout
