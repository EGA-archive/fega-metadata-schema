"""Shared Biovalidator HTTP helpers."""
from __future__ import annotations

import json
from typing import Any, Dict

import requests
from requests.exceptions import ConnectionError, Timeout

from fega_tools.validation_common import (
    INVALID_STATUS,
    REQUEST_ERROR_STATUS,
    UNKNOWN_STATUS,
    VALID_STATUS,
)

DEFAULT_VALIDATOR_URL = "http://localhost:3020/validate"


def assert_validator_reachable(url: str, timeout_seconds: int = 5) -> None:
    """Raise RuntimeError if the Biovalidator endpoint is not reachable."""
    try:
        requests.get(url, timeout=timeout_seconds)
    except (ConnectionError, Timeout, requests.RequestException) as exc:
        raise RuntimeError(f"Cannot reach Biovalidator endpoint '{url}': {exc}") from exc


def post_to_validator(document: Dict[str, Any], url: str) -> Any:
    """Send a wrapper document to Biovalidator and return the parsed JSON body."""
    response = requests.post(
        url,
        json=document,
        headers={"Content-Type": "application/json"},
        timeout=300,
    )
    response.raise_for_status()
    return response.json()


def classify_response(response: Any) -> str:
    """Classify Biovalidator's response shape."""
    if isinstance(response, list) and not response:
        return VALID_STATUS
    if isinstance(response, list):
        return INVALID_STATUS
    return UNKNOWN_STATUS


def validate_document(document: Dict[str, Any], url: str) -> Dict[str, Any]:
    """Submit one document and return a normalized validation result."""
    try:
        response = post_to_validator(document, url)
    except requests.RequestException as exc:
        return {"status": REQUEST_ERROR_STATUS, "errors": [str(exc)]}
    except (json.JSONDecodeError, ValueError) as exc:
        return {
            "status": UNKNOWN_STATUS,
            "errors": [f"Malformed validator response: {exc}"],
        }

    status = classify_response(response)
    result: Dict[str, Any] = {"status": status}
    if status != VALID_STATUS:
        result["errors"] = response if isinstance(response, list) else [response]
    return result
