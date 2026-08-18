from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import requests

from scripts.py import verify_remote_release


TAG = "v2.0.0"


class _Response:
    def __init__(
        self,
        *,
        content: bytes = b"",
        payload: object | None = None,
        error: Exception | None = None,
    ):
        self.content = content
        self._payload = payload
        self._error = error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    def json(self) -> object:
        assert self._payload is not None
        return self._payload


class _Session:
    def __init__(self, responses: dict[str, _Response | Exception]):
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, *, timeout: float) -> _Response:
        self.urls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return response


def _manifest(root: Path, *, status: str = "released", tag: str = TAG) -> tuple[Path, dict]:
    payload = {
        "manifest_version": 1,
        "status": status,
        "tag": tag,
        "resources": [{"path": "schemas/widget/schema.json", "sha256": ""}],
    }
    resource = b'{"type":"object"}\n'
    payload["resources"][0]["sha256"] = hashlib.sha256(resource).hexdigest()
    path = root / "build/release_manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path, payload


def _urls(resource_path: str) -> tuple[str, str]:
    base = "https://raw.githubusercontent.com/EGA-archive/fega-metadata-schema/v2.0.0/"
    return (
        base + "build/release_manifest.json",
        base + resource_path,
    )


def test_invalid_tag_is_rejected(tmp_path: Path) -> None:
    path, _ = _manifest(tmp_path)
    with pytest.raises(ValueError, match="Invalid release tag"):
        verify_remote_release.verify(
            "release-2.0.0", "EGA-archive/fega-metadata-schema", path, 1.0
        )


@pytest.mark.parametrize("status, tag", [("unreleased", TAG), ("released", "v2.0.1")])
def test_local_manifest_tag_or_status_mismatch(tmp_path: Path, status: str, tag: str) -> None:
    path, _ = _manifest(tmp_path, status=status, tag=tag)
    with pytest.raises(ValueError, match="Local manifest"):
        verify_remote_release.verify(
            TAG, "EGA-archive/fega-metadata-schema", path, 1.0
        )


def test_remote_manifest_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, manifest = _manifest(tmp_path)
    manifest_url, _ = _urls("schemas/widget/schema.json")
    session = _Session({manifest_url: _Response(payload={**manifest, "tag": "v2.0.1"})})
    monkeypatch.setattr(verify_remote_release, "_session", lambda: session)
    with pytest.raises(ValueError, match="Remote manifest differs"):
        verify_remote_release.verify(
            TAG, "EGA-archive/fega-metadata-schema", path, 1.0
        )


def test_resource_http_failure_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, manifest = _manifest(tmp_path)
    manifest_url, resource_url = _urls(manifest["resources"][0]["path"])
    session = _Session({
        manifest_url: _Response(payload=manifest),
        resource_url: requests.RequestException("temporary outage"),
    })
    monkeypatch.setattr(verify_remote_release, "_session", lambda: session)
    with pytest.raises(ValueError, match="temporary outage"):
        verify_remote_release.verify(
            TAG, "EGA-archive/fega-metadata-schema", path, 1.0
        )


def test_resource_checksum_mismatch_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, manifest = _manifest(tmp_path)
    manifest_url, resource_url = _urls(manifest["resources"][0]["path"])
    session = _Session({
        manifest_url: _Response(payload=manifest),
        resource_url: _Response(content=b"different bytes"),
    })
    monkeypatch.setattr(verify_remote_release, "_session", lambda: session)
    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_remote_release.verify(
            TAG, "EGA-archive/fega-metadata-schema", path, 1.0
        )


def test_success_reports_checked_resource_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, manifest = _manifest(tmp_path)
    manifest_url, resource_url = _urls(manifest["resources"][0]["path"])
    session = _Session({
        manifest_url: _Response(payload=manifest),
        resource_url: _Response(content=b'{"type":"object"}\n'),
    })
    monkeypatch.setattr(verify_remote_release, "_session", lambda: session)
    result = verify_remote_release.verify(
        TAG, "EGA-archive/fega-metadata-schema", path, 1.0
    )
    assert result["tag"] == TAG
    assert result["resources_checked"] == 1
    assert session.urls == [manifest_url, resource_url]


def test_manifest_v2_prerelease_checks_repository_and_citation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    content = b'{"type":"object"}\n'
    digest = hashlib.sha256(content).hexdigest()
    manifest = {
        "$schema": "release_manifest.schema.json",
        "manifest_version": 2,
        "bundle_version": "2.0.0-draft.1",
        "tag": "v2.0.0-draft.1",
        "release_date": "2026-08-06",
        "source_repository": "owner/repository",
        "source_commit": "abcdef0",
        "components": [{"name": "widget", "schema": "schemas/widget/schema.json", "id": "https://raw.githubusercontent.com/owner/repository/v2.0.0-draft.1/schemas/widget/schema.json", "version": "1.0.0", "dependencies": [], "checksums": {"schema": digest}}],
        "standards": [],
    }
    path = tmp_path / "build/release_manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    base = "https://raw.githubusercontent.com/owner/repository/v2.0.0-draft.1/"
    urls = {
        base + "build/release_manifest.json": _Response(payload=manifest),
        base + "schemas/widget/schema.json": _Response(content=content),
        base + "CITATION.cff": _Response(content=b"version: 2.0.0-draft.1\n"),
    }
    session = _Session(urls)
    monkeypatch.setattr(verify_remote_release, "_session", lambda: session)
    result = verify_remote_release.verify("v2.0.0-draft.1", "owner/repository", path, 1.0)
    assert result["resources_checked"] == 1
