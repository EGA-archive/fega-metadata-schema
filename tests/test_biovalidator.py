from __future__ import annotations

import json

import requests

from fega_tools import biovalidator
from fega_tools.validation_common import (
    INVALID_STATUS,
    REQUEST_ERROR_STATUS,
    UNKNOWN_STATUS,
    VALID_STATUS,
)


def test_validate_document_normalizes_response_statuses(monkeypatch) -> None:
    """Check that validator responses map to the expected status values."""
    responses = iter([[], [{"message": "bad"}], {"unexpected": True}])
    monkeypatch.setattr(
        biovalidator,
        "post_to_validator",
        lambda document, url: next(responses),
    )

    assert biovalidator.validate_document({}, "http://validator")["status"] == VALID_STATUS
    invalid = biovalidator.validate_document({}, "http://validator")
    assert invalid["status"] == INVALID_STATUS
    assert invalid["errors"] == [{"message": "bad"}]
    unknown = biovalidator.validate_document({}, "http://validator")
    assert unknown["status"] == UNKNOWN_STATUS


def test_validate_document_normalizes_request_and_decode_errors(monkeypatch) -> None:
    """Check that request and response decoding errors become stable results."""
    def raise_request(document, url):
        """Raise a request error for the validation call."""
        raise requests.RequestException("offline")

    monkeypatch.setattr(biovalidator, "post_to_validator", raise_request)
    request_error = biovalidator.validate_document({}, "http://validator")
    assert request_error == {
        "status": REQUEST_ERROR_STATUS,
        "errors": ["offline"],
    }

    def raise_decode(document, url):
        """Raise a JSON decoding error for the validation call."""
        raise json.JSONDecodeError("bad", "", 0)

    monkeypatch.setattr(biovalidator, "post_to_validator", raise_decode)
    malformed = biovalidator.validate_document({}, "http://validator")
    assert malformed["status"] == UNKNOWN_STATUS
    assert "Malformed validator response" in malformed["errors"][0]
