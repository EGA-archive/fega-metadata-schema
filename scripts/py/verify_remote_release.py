#!/usr/bin/env python3
"""Verify an immutable release manifest and its raw GitHub resources."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from fega_tools.release import Version, repository_identity
from fega_tools.cli_utils import help_with_example


TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify immutable release resources. Example: verify_remote_release --tag v2.0.0")
    parser.add_argument("--tag", required=True, help=help_with_example("Release tag", "--tag v2.0.0"))
    parser.add_argument("--repository", help=help_with_example("GitHub owner/repo", "--repository owner/repo"))
    parser.add_argument("--manifest", type=Path, default=Path("build/release_manifest.json"), metavar="PATH", help=help_with_example("Local release manifest", "--manifest build/release_manifest.json"))
    parser.add_argument("--timeout", type=float, default=20.0, metavar="SECONDS", help=help_with_example("HTTP timeout", "--timeout 20"))
    return parser


def _session() -> requests.Session:
    session = requests.Session()
    retries = Retry(total=4, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


def _remote_manifest_path(manifest_path: Path) -> str:
    path = manifest_path.as_posix()
    if manifest_path.is_absolute():
        marker = "/build/"
        if marker not in path:
            raise ValueError("Manifest path must point to a repository build/ file")
        path = "build/" + path.rsplit(marker, 1)[1]
    return path


def _manifest_resources(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    resources: list[tuple[str, str]] = []
    for component in manifest.get("components", []):
        if not isinstance(component, dict):
            continue
        checksums = component.get("checksums", {})
        if isinstance(checksums, dict):
            base = Path(component.get("schema", "")).parent
            for asset, digest in checksums.items():
                filename = {"schema": Path(component.get("schema", "")).name, "context": "context.jsonld", "frame": "frame.jsonld"}.get(asset)
                if filename and isinstance(digest, str):
                    resources.append(((base / filename).as_posix(), digest))
    for standard in manifest.get("standards", []):
        if isinstance(standard, dict) and isinstance(standard.get("path"), str) and isinstance(standard.get("sha256"), str):
            # The grouped digest is checked from a downloaded archive when
            # available; the group directory itself is not a raw file.
            continue
    # Compatibility with older local manifests is harmless and useful for
    # callers upgrading the verifier independently.
    for resource in manifest.get("resources", []):
        if isinstance(resource, dict) and isinstance(resource.get("path"), str) and isinstance(resource.get("sha256"), str):
            resources.append((resource["path"], resource["sha256"]))
    return sorted(set(resources))


def _archive_group_digests(content: bytes, groups: list[dict[str, Any]]) -> dict[str, tuple[str, int]]:
    """Recompute grouped standard hashes from a GitHub source archive."""
    wanted = {item.get("path"): item for item in groups if isinstance(item, dict)}
    files: dict[str, list[tuple[str, bytes]]] = {path: [] for path in wanted if isinstance(path, str)}
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile() or "/" not in member.name:
                continue
            relative = member.name.split("/", 1)[1]
            for group_path in files:
                prefix = group_path.rstrip("/") + "/"
                if relative.startswith(prefix):
                    child = relative[len(prefix):]
                    handle = archive.extractfile(member)
                    if handle is not None:
                        files[group_path].append((child, handle.read()))
    result: dict[str, tuple[str, int]] = {}
    for group_path, entries in files.items():
        digest = hashlib.sha256()
        for path, data in sorted(entries):
            encoded = path.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
        result[group_path] = (digest.hexdigest(), len(entries))
    return result


def verify(tag: str, repository: str | None = None, manifest_path: Path = Path("build/release_manifest.json"), timeout: float = 20.0, session: Any | None = None) -> dict[str, object]:
    if not TAG_RE.fullmatch(tag):
        raise ValueError(f"Invalid release tag: {tag}")
    # Exercise the strict SemVer parser as well as the URL-safe tag pattern;
    # this rejects leading-zero numeric prerelease identifiers.
    Version.parse(tag[1:])
    local_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(local_manifest, dict) or local_manifest.get("tag") != tag or local_manifest.get("status", "released") != "released":
        raise ValueError("Local manifest does not describe the requested released tag")
    repository = repository or local_manifest.get("source_repository")
    if not isinstance(repository, str):
        repository = repository_identity(manifest_path.parent.parent)
    if local_manifest.get("source_repository") not in {None, repository}:
        raise ValueError("Manifest repository does not match requested repository")
    base = f"https://raw.githubusercontent.com/{repository}/{tag}/"
    session = session or _session()
    manifest_url = base + quote(_remote_manifest_path(manifest_path), safe="/")
    response = session.get(manifest_url, timeout=timeout)
    response.raise_for_status()
    remote_manifest = response.json()
    if remote_manifest != local_manifest:
        raise ValueError("Remote manifest differs from the reviewed local manifest")
    failures: list[str] = []
    checked = 0
    for relative, expected in _manifest_resources(local_manifest):
        url = base + quote(relative, safe="/")
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            failures.append(f"{relative}: {exc}")
            continue
        digest = hashlib.sha256(response.content).hexdigest()
        if digest != expected:
            failures.append(f"{relative}: checksum mismatch; expected {expected}, got {digest}")
        checked += 1
    if "source_repository" in local_manifest:
        citation_url = base + "CITATION.cff"
        try:
            citation_response = session.get(citation_url, timeout=timeout)
            citation_response.raise_for_status()
            matches = re.findall(r"(?m)^version\s*:\s*([^\r\n#]+?)\s*(?:#.*)?$", citation_response.content.decode("utf-8"))
            if len(matches) != 1 or matches[0].strip() != str(local_manifest.get("bundle_version")):
                failures.append("CITATION.cff version does not match manifest bundle_version")
        except (requests.RequestException, UnicodeDecodeError) as exc:
            failures.append(f"CITATION.cff verification failed: {exc}")
    standards = local_manifest.get("standards")
    if isinstance(standards, list) and standards:
        archive_url = f"https://github.com/{repository}/archive/refs/tags/{quote(tag, safe='')}.tar.gz"
        try:
            archive_response = session.get(archive_url, timeout=timeout)
            archive_response.raise_for_status()
            actual_groups = _archive_group_digests(archive_response.content, standards)
            for group in standards:
                if not isinstance(group, dict):
                    failures.append("manifest contains malformed standard group")
                    continue
                path = group.get("path")
                actual_digest, actual_count = actual_groups.get(path, (None, None))
                if actual_digest != group.get("sha256") or actual_count != group.get("file_count"):
                    failures.append(f"{path}: grouped standards checksum mismatch")
        except (requests.RequestException, tarfile.TarError, OSError, ValueError) as exc:
            failures.append(f"standards archive verification failed: {exc}")
    if failures:
        raise ValueError("Remote release verification failed:\n- " + "\n- ".join(failures))
    return {"tag": tag, "repository": repository, "manifest_url": manifest_url, "resources_checked": checked}


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        print(json.dumps(verify(args.tag, args.repository, args.manifest, args.timeout), indent=2))
    except (OSError, ValueError, json.JSONDecodeError, requests.RequestException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
