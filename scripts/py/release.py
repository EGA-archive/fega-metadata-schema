#!/usr/bin/env python3
"""Discover, compare, generate, normalise, and verify schema releases."""
from __future__ import annotations

import argparse
import json
import subprocess
import tarfile
import tempfile
import io
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Sequence

from fega_tools.release import (
    Version,
    analyse_release,
    associate_pr_rationales,
    create_manifest,
    discover_components,
    discover_standard_groups,
    repository_identity,
    rewrite_repository_uris,
    verify_release,
    write_json,
)
from fega_tools.release_notes import parse_pr_body, record_from_dict
from fega_tools.release_policy import check_release_policy
from fega_tools.schema_diff import compare_schemas
from fega_tools.cli_utils import help_with_example


def _load(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _dump(value: object, output: Path | None = None) -> None:
    rendered = json.dumps(value, indent=2, ensure_ascii=False) + "\n"
    if output:
        output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


def _summarise_analysis(report: dict[str, object]) -> None:
    components = report.get("components", [])
    if isinstance(components, list):
        for component in components:
            if isinstance(component, dict):
                print(f"{component.get('name')}: {component.get('required_change')} (declared {component.get('version')})", file=sys.stderr)
    errors = report.get("errors", [])
    print(f"Summary: {len(components) if isinstance(components, list) else 0} component(s), {len(errors) if isinstance(errors, list) else 0} error(s)", file=sys.stderr)


@contextmanager
def _previous_tree(root: Path, previous: Path | None, previous_ref: str | None):
    """Yield a previous checkout path, archiving a git ref when requested."""
    if previous is not None and previous_ref is not None:
        raise ValueError("--previous and --previous-ref are mutually exclusive")
    if previous_ref is None:
        yield previous
        return
    with tempfile.TemporaryDirectory(prefix="fega-release-previous-") as temporary:
        archive = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", previous_ref], check=True, stdout=subprocess.PIPE)
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as handle:
            handle.extractall(temporary, filter="data")
        yield Path(temporary)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect, prepare, and verify schema releases. Example: release.py --root . discover")
    parser.add_argument("--root", type=Path, default=Path.cwd(), metavar="PATH", help=help_with_example("Repository checkout", "--root ."))
    sub = parser.add_subparsers(dest="command", required=True)
    discover = sub.add_parser("discover", help="List components and standards. Example: discover", description="List discovered release components and standards. Example: discover")
    discover.add_argument("-v", "--verbose", action="store_true", help=help_with_example("Print a short summary", "-v"))
    semver = sub.add_parser("semver", help="Compare schemas and report a bump. Example: semver before.json after.json", description="Compare two schemas or analyse all components. Example: semver --all")
    semver.add_argument("before", nargs="?", help=help_with_example("Old schema JSON path", "before.json"))
    semver.add_argument("after", nargs="?", help=help_with_example("New schema JSON path", "after.json"))
    semver.add_argument("--all", action="store_true", help=help_with_example("Analyse every component", "--all"))
    semver.add_argument("--component", help=help_with_example("Limit analysis to a component name", "--component widget"))
    semver.add_argument("--previous-ref", help=help_with_example("Git ref for the previous tree", "--previous-ref v1.2.3"))
    semver.add_argument("-v", "--verbose", action="store_true", help=help_with_example("Print a short summary", "-v"))
    semver.add_argument("-o", "--output", type=Path, metavar="PATH", help=help_with_example("Write JSON report", "--output report.json"))
    manifest = sub.add_parser("manifest", help="Generate a release manifest. Example: manifest --bundle-version 2.0.0", description="Generate an inventory-only release manifest. Example: manifest --bundle-version 2.0.0")
    manifest.add_argument("--bundle-version", required=True, help=help_with_example("Bundle SemVer", "--bundle-version 2.0.0"))
    manifest.add_argument("--repository", help=help_with_example("GitHub owner/repo", "--repository owner/repo"))
    manifest.add_argument("--source-commit", required=True, help=help_with_example("Source commit SHA", "--source-commit abc123"))
    manifest.add_argument("--date", dest="release_date", help=help_with_example("UTC release date", "--date 2026-08-17"))
    manifest.add_argument("--write", action="store_true", help=help_with_example("Write build/release_manifest.json", "--write"))
    manifest.add_argument("--dry-run", action="store_true", help=help_with_example("Do not write files", "--dry-run"))
    manifest.add_argument("-o", "--output", type=Path, metavar="PATH", help=help_with_example("Write JSON report", "--output report.json"))
    manifest.add_argument("-v", "--verbose", action="store_true", help=help_with_example("Print a short summary", "-v"))
    normalise = sub.add_parser("normalise", help="Rewrite release URIs. Example: normalise --from-ref v2.0.0 --to-ref main", description="Rewrite release URIs back to a development ref. Example: normalise --from-ref v2.0.0 --to-ref main")
    normalise.add_argument("--repository", help=help_with_example("GitHub owner/repo", "--repository owner/repo"))
    normalise.add_argument("--from-ref", required=True, help=help_with_example("Current URI ref", "--from-ref v2.0.0"))
    normalise.add_argument("--to-ref", required=True, help=help_with_example("Target URI ref", "--to-ref main"))
    normalise.add_argument("--check", action="store_true", help=help_with_example("Verify instead of rewriting", "--check"))
    verify = sub.add_parser("verify", help="Verify a release tree. Example: verify --mode release --ref v2.0.0", description="Verify a development, candidate, or tag tree. Example: verify --mode release --ref v2.0.0")
    verify.add_argument("--mode", choices=("development", "candidate", "tag", "release"), required=True, help=help_with_example("Verification mode", "--mode release"))
    verify.add_argument("--tag", help=help_with_example("Release tag", "--tag v2.0.0"))
    verify.add_argument("--ref", help=help_with_example("Git ref to verify", "--ref v2.0.0"))
    verify.add_argument("--repository", help=help_with_example("GitHub owner/repo", "--repository owner/repo"))
    verify.add_argument("-v", "--verbose", action="store_true", help=help_with_example("Print a success message", "-v"))
    analyse = sub.add_parser("analyse", help="Analyse component versions. Example: analyse --bundle-version 2.0.0", description="Analyse component versions against a previous checkout. Example: analyse --bundle-version 2.0.0")
    previous_group = analyse.add_mutually_exclusive_group()
    previous_group.add_argument("--previous", type=Path, help="Previous checkout directory. Example: --previous previous-tree")
    previous_group.add_argument("--previous-ref", help="Git ref to archive as the previous checkout. Example: --previous-ref v1.2.3")
    analyse.add_argument("--bundle-version", help=help_with_example("Requested bundle SemVer", "--bundle-version 2.0.0"))
    analyse.add_argument("--repository", help=help_with_example("GitHub owner/repo", "--repository owner/repo"))
    analyse.add_argument("--bootstrap", action="store_true", help=help_with_example("Analyse as the first release", "--bootstrap"))
    analyse.add_argument("--prs-json", type=Path, metavar="PATH", help=help_with_example("Collected PR snapshot", "--prs-json pr.json"))
    analyse.add_argument("--pr-body-file", type=Path, metavar="PATH", help=help_with_example("Validated PR body", "--pr-body-file pr.md"))
    analyse.add_argument("-o", "--output", type=Path, metavar="PATH", help=help_with_example("Write JSON report", "--output report.json"))
    analyse.add_argument("-v", "--verbose", action="store_true", help=help_with_example("Print a short summary", "-v"))
    prepare = sub.add_parser("prepare", help="Materialise a release snapshot. Example: prepare --bundle-version 2.0.0", description="Materialise URIs, citation, manifest, and changelog. Example: prepare --bundle-version 2.0.0")
    previous_group = prepare.add_mutually_exclusive_group()
    previous_group.add_argument("--previous", type=Path, help="Previous checkout directory. Example: --previous previous-tree")
    previous_group.add_argument("--previous-ref", help="Git ref to archive as the previous checkout. Example: --previous-ref v1.2.3")
    prepare.add_argument("--bundle-version", required=True, help=help_with_example("Requested bundle SemVer", "--bundle-version 2.0.0"))
    prepare.add_argument("--repository", help=help_with_example("GitHub owner/repo", "--repository owner/repo"))
    prepare.add_argument("--source-commit", required=True, help=help_with_example("Source commit SHA", "--source-commit abc123"))
    prepare.add_argument("--date", required=True, help=help_with_example("UTC release date", "--date 2026-08-17"))
    prepare.add_argument("--dry-run", action="store_true", help=help_with_example("Analyse without writing", "--dry-run"))
    prepare.add_argument("--bootstrap", action="store_true", help=help_with_example("Prepare the first release", "--bootstrap"))
    prepare.add_argument("--rationales", type=Path, metavar="PATH", help=help_with_example("JSON compatibility rationales", "--rationales rationales.json"))
    prepare.add_argument("--prs-json", type=Path, metavar="PATH", help=help_with_example("Collected PR snapshot", "--prs-json pr.json"))
    prepare.add_argument("--changelog", type=Path, metavar="PATH", help=help_with_example("Generated changelog", "--changelog CHANGELOG.next.md"))
    prepare.add_argument("-o", "--output", type=Path, metavar="PATH", help=help_with_example("Write JSON report", "--output report.json"))
    prepare.add_argument("-v", "--verbose", action="store_true", help=help_with_example("Print a short summary", "-v"))
    policy = sub.add_parser("policy", help="Check ordinary PR policy. Example: policy --base base --changed-file files.txt", description="Check ordinary PR policy files. Example: policy --base base --changed-file files.txt")
    policy.add_argument("--base", type=Path, required=True, metavar="PATH", help=help_with_example("Base checkout", "--base previous"))
    policy.add_argument("--head", type=Path, default=Path.cwd(), metavar="PATH", help=help_with_example("Head checkout", "--head ."))
    policy.add_argument("--changed-file", type=Path, required=True, metavar="PATH", help=help_with_example("File listing changed paths", "--changed-file changed.txt"))
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "discover":
            try:
                discovered_repository = repository_identity(root)
            except ValueError:
                discovered_repository = None
            result = {"repository": discovered_repository, "components": [{"name": item.name, "schema": item.schema.as_posix(), "id": item.id, "version": str(item.version), "context": item.context.as_posix() if item.context else None, "frame": item.frame.as_posix() if item.frame else None} for item in discover_components(root)], "standards": discover_standard_groups(root)}
            _dump(result)
            if args.verbose:
                print(f"Summary: {len(result['components'])} component(s), {len(result['standards'])} standards group(s)", file=sys.stderr)
            return
        if args.command == "semver":
            if args.before and args.after:
                result = compare_schemas(_load(Path(args.before)), _load(Path(args.after))).as_dict()
                _dump(result, args.output)
                if args.verbose:
                    print(f"Summary: {result['severity']} ({len(result['changes'])} detected change(s))", file=sys.stderr)
                return
            if args.all or args.component:
                with _previous_tree(root, None, args.previous_ref) as previous_tree:
                    result = analyse_release(root, previous_tree, requested_version=None)
                if args.component:
                    selected = [item for item in result["components"] if item["name"] == args.component]
                    if not selected:
                        raise ValueError(f"Unknown component: {args.component}")
                    result["components"] = selected
                    result["errors"] = [error for error in result["errors"] if f"Component '{args.component}'" in error]
                    result["removed_components"] = [item for item in result["removed_components"] if item["name"] == args.component]
                    result["bundle_change"] = selected[0]["required_change"]
                _dump(result, args.output)
                if args.verbose:
                    _summarise_analysis(result)
                return
            raise ValueError("semver requires BEFORE AFTER or --all/--component")
        if args.command == "manifest":
            result = create_manifest(root, bundle_version=args.bundle_version, repository=args.repository, source_commit=args.source_commit, release_date=args.release_date)
            if args.write and not args.dry_run:
                write_json(root / "build/release_manifest.json", result)
            _dump(result, args.output)
            if args.verbose:
                print(f"Summary: {len(result['components'])} component(s), {len(result['standards'])} standards group(s)", file=sys.stderr)
            return
        if args.command == "normalise":
            target_ref = args.to_ref
            if args.check:
                verify_release(root, mode="tag" if target_ref.startswith("v") else "development", expected_ref=target_ref, repository=args.repository)
                return
            changed = rewrite_repository_uris(root, args.from_ref, target_ref, repository=args.repository)
            _dump({"changed": changed, "from": args.from_ref, "to": target_ref})
            return
        if args.command == "verify":
            verify_release(root, mode=args.mode, expected_ref=args.ref or args.tag, repository=args.repository)
            if args.verbose:
                print(f"Verified {args.mode} release state at {args.ref or args.tag or 'the inferred ref'}", file=sys.stderr)
            return
        if args.command == "analyse":
            rationales = None
            if args.prs_json:
                raw = json.loads(args.prs_json.read_text(encoding="utf-8"))
                values = raw.get("pull_requests", raw) if isinstance(raw, dict) else raw
                records = [record_from_dict(value) for value in values]
                rationales = associate_pr_rationales(root, records)
            if args.pr_body_file:
                parsed = parse_pr_body(args.pr_body_file.read_text(encoding="utf-8"))
                if parsed.diagnostics:
                    raise ValueError(f"PR body is invalid: {parsed.diagnostics[0].message}")
                if parsed.compatibility_rationale:
                    # A single PR's rationale is applied only to unknown
                    # results from this base/head analysis; known changes are
                    # unaffected by the mapping.
                    rationales = {item.name: parsed.compatibility_rationale for item in discover_components(root)}
            with _previous_tree(root, args.previous, args.previous_ref) as previous_tree:
                report = analyse_release(root, previous_tree, bootstrap=args.bootstrap, approved_rationales=rationales, repository=args.repository, requested_version=args.bundle_version)
            _dump(report, args.output)
            if args.verbose:
                _summarise_analysis(report)
            if report["errors"]:
                raise ValueError("Release analysis found version or compatibility policy errors")
            return
        if args.command == "prepare":
            rationales = None
            if args.rationales:
                rationales = json.loads(args.rationales.read_text(encoding="utf-8"))
            if args.prs_json:
                raw = json.loads(args.prs_json.read_text(encoding="utf-8"))
                values = raw.get("pull_requests", raw) if isinstance(raw, dict) else raw
                records = [record_from_dict(value) for value in values]
                rationales = associate_pr_rationales(root, records)
            changelog_text = args.changelog.read_text(encoding="utf-8") if args.changelog else None
            with _previous_tree(root, args.previous, args.previous_ref) as previous_tree:
                report = __import__("fega_tools.release", fromlist=["prepare_release"]).prepare_release(root, previous_tree, release_date=args.date, source_commit=args.source_commit, requested_version=args.bundle_version, repository=args.repository, bootstrap=args.bootstrap, write=not args.dry_run, changelog_text=changelog_text, approved_rationales=rationales)
            _dump(report, args.output)
            if args.verbose:
                _summarise_analysis(report)
            return
        errors = check_release_policy(args.base.resolve(), args.head.resolve(), args.changed_file.read_text(encoding="utf-8").splitlines())
        if errors:
            raise ValueError("Release policy failed:\n- " + "\n- ".join(errors))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
