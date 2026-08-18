from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from fega_tools.release_notes import (
    CollectionError,
    GhApiClient,
    PullRequestRecord,
    collect_pull_requests,
    insert_changelog_entry,
    parse_pr_body,
    render_changelog_entry,
    bootstrap_changelog,
    promote_changelog,
    record_from_dict,
    record_to_dict,
    validate_pr_body,
)


VALID = """## Release notes

Category: Added

- Add a searchable metadata endpoint.

## Compatibility review

Not applicable

## Unrelated heading

This section is ignored.
"""


def _pr(number: int, category: str = "Added", merged_at: str = "2026-08-06T10:00:00Z") -> PullRequestRecord:
    bullet = f"\n- Summary {number}\n" if category != "None" else "\n"
    return PullRequestRecord(number, f"Feature {number}", f"https://github.com/owner/repo/pull/{number}", f"sha{number}", merged_at, f"## Release notes\n\nCategory: {category}{bullet}")


def test_parse_valid_and_compatibility_rationale() -> None:
    parsed = parse_pr_body(VALID)
    assert parsed.valid
    assert parsed.category == "Added"
    assert parsed.bullets == ("Add a searchable metadata endpoint.",)
    assert parsed.compatibility_rationale is None

    rationale = parse_pr_body("## Release notes\n\nCategory: Changed\n\n- Keep old fields.\n\n## Compatibility review\n\nExisting valid data remains valid because the field is optional.\n")
    assert rationale.compatibility_rationale == "Existing valid data remains valid because the field is optional."


@pytest.mark.parametrize("body,code", [
    ("## Release notes\n\nCategory: None\n\n- no bullet\n", "none-with-bullets"),
    ("## Release notes\n\nCategory: Unknown\n\n- Summary\n", "unknown-category"),
    ("## Release notes\n\nCategory: Added\n\n-\n", "empty-bullet"),
    ("## Release notes\n\nCategory: Added\n\n- [Describe the change]\n", "placeholder"),
    ("## Release notes\n\nCategory: Added\n\n- Summary\n\n## Release notes\n\nCategory: Fixed\n\n- Other\n", "duplicate-release-notes"),
])
def test_strict_validation(body: str, code: str) -> None:
    assert any(item.code == code for item in validate_pr_body(body))


def test_default_template_summary_is_not_accepted() -> None:
    body = "## Release notes\n\nCategory: Added\n\n- This PR adds X and Y, because of ...\n"
    assert any(item.code == "placeholder" for item in validate_pr_body(body))


def test_repository_template_keeps_guidance_but_rejects_visible_mock() -> None:
    body = (Path(__file__).parents[1] / ".github/pull_request_template.md").read_text(encoding="utf-8")
    diagnostics = validate_pr_body(body)
    assert {item.code for item in diagnostics} == {"placeholder", "missing-summary"}


def test_permanent_template_comments_are_ignored() -> None:
    body = "## Release notes\n\n<!-- Explain the required Category line and bullets.\nThis guidance stays in the template. -->\nCategory: Added\n\n- A real user-visible change.\n\n## Compatibility review\n\n<!-- Explain when prose is required. -->\nNot applicable\n"
    parsed = parse_pr_body(body)
    assert parsed.valid
    assert parsed.bullets == ("A real user-visible change.",)


def test_render_categories_and_none_are_deterministic() -> None:
    entry = render_changelog_entry("2.0.0-draft.1", date(2026, 8, 6), [_pr(2, "Fixed"), _pr(1, "Added"), _pr(3, "None")])
    assert entry.index("### Added") < entry.index("### Fixed")
    assert "[#1](https://github.com/owner/repo/pull/1)" in entry
    assert "#3" not in entry


def test_render_sanitises_hostile_pr_metadata() -> None:
    record = PullRequestRecord(4, "Title\n\n### Inject", "javascript:alert(1)", "sha4", "2026-08-06T10:00:00Z", "## Release notes\n\nCategory: Added\n\n- Safe summary.\n")
    entry = render_changelog_entry("2.0.0", date(2026, 8, 6), [record])
    assert "javascript:" not in entry
    assert "### Inject" not in entry
    assert "Untitled" not in entry


def test_insert_after_unreleased_and_reject_duplicate() -> None:
    existing = "# Changelog\n\nPreamble.\n\n## [Unreleased]\n\n### Added\n\n- Existing.\n\n## [1.0.0] - 2026-01-01\n\n- Old.\n"
    entry = render_changelog_entry("2.0.0", date(2026, 8, 6), [_pr(1)])
    result = insert_changelog_entry(existing, entry)
    assert result.index("## [2.0.0]") < result.index("## [1.0.0]")
    with pytest.raises(ValueError, match="already contains"):
        insert_changelog_entry(result, entry)


def test_bootstrap_preserves_unreleased_body_and_rejects_duplicate() -> None:
    existing = "# Changelog\n\n## [Unreleased]\n\n### Added\n\n- Legacy bullet.\n"
    rendered = bootstrap_changelog(existing, bundle_version="2.0.0-draft.1", release_date="2026-08-06")
    assert "## [Unreleased]\n\n## [2.0.0-draft.1] - 2026-08-06" in rendered
    assert "### Added\n\n- Legacy bullet." in rendered
    with pytest.raises(ValueError, match="already contains"):
        bootstrap_changelog(rendered, bundle_version="2.0.0-draft.1", release_date=date(2026, 8, 6))


def test_stable_promotion_has_automated_statement_without_pr_subsection() -> None:
    rendered = promote_changelog("# Changelog\n\n## [Unreleased]\n", stable_version="2.0.0", previous_prerelease="2.0.0-draft.1", release_date=date(2026, 8, 6))
    assert "Automated promotion of [2.0.0-draft.1] to [2.0.0]." in rendered
    assert "#### [#" not in rendered


def test_changed_files_round_trip() -> None:
    record = _pr(7)
    record = PullRequestRecord(record.number, record.title, record.html_url, record.merge_commit_sha, record.merged_at, record.body, record.parsed, record.commits, ("schemas/widget/schema.json",))
    raw = record_to_dict(record)
    assert raw["changed_files"] == ["schemas/widget/schema.json"]
    assert record_from_dict(raw).changed_files == ("schemas/widget/schema.json",)


class FakeClient:
    def list_commits(self, repository: str, previous: str | None, source: str):
        return [{"sha": "a", "commit": {"message": "A"}}, {"sha": "b", "commit": {"message": "B"}}]

    def associated_pull_requests(self, repository: str, sha: str):
        if sha == "a":
            return [{"number": 1, "title": "One", "html_url": "https://github.com/o/r/pull/1", "merge_commit_sha": "a", "merged_at": "2026-08-06T01:00:00Z", "body": VALID}]
        return [{"number": 1, "title": "One", "html_url": "https://github.com/o/r/pull/1", "merge_commit_sha": "a", "merged_at": "2026-08-06T01:00:00Z", "body": VALID}]

    def get_pull_files(self, repository: str, number: int):
        return [{"filename": "schemas/widget/schema.json"}, {"filename": None}]


def test_collection_deduplicates_prs_and_accounts_commits() -> None:
    result = collect_pull_requests("o/r", "v1", "source", FakeClient())
    assert [record.number for record in result.pull_requests] == [1]
    assert result.accounted_commits == ("a", "b")


def test_collection_fails_on_direct_commit() -> None:
    class Missing(FakeClient):
        def associated_pull_requests(self, repository: str, sha: str):
            return [] if sha == "b" else super().associated_pull_requests(repository, sha)

    with pytest.raises(CollectionError, match="Unaccounted"):
        collect_pull_requests("o/r", "v1", "source", Missing())


def test_gh_api_client_flattens_paginated_compare_without_reversing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class Result:
        stdout = "[{\"commits\":[{\"sha\":\"old\"}]},{\"commits\":[{\"sha\":\"new\"}]}]"

    def run(command, **kwargs):
        calls.append(command)
        return Result()

    monkeypatch.setattr("fega_tools.release_notes.subprocess.run", run)
    commits = GhApiClient().list_commits("o/r", "v1", "source")
    assert [commit["sha"] for commit in commits] == ["old", "new"]
    assert "--paginate" in calls[0]
    assert "--slurp" in calls[0]


def test_gh_api_client_unwraps_slurped_pull_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    pr_one = {"number": 1}
    pr_two = {"number": 2}
    pull = {"number": 3, "title": "Three"}
    client = GhApiClient()
    responses = iter([[[pr_one], [pr_two]], [pull]])
    monkeypatch.setattr(client, "_api", lambda *args, **kwargs: next(responses))
    assert client.associated_pull_requests("o/r", "sha") == [pr_one, pr_two]
    assert client.get_pull("o/r", 3) == pull
