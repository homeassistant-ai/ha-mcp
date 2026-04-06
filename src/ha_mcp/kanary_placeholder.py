"""
Kanary placeholder — [TEST - DO NOT MERGE].

Intentional anti-patterns for Gemini Code Assist regression testing.
This file must remain a draft PR and never be merged.
See references/gemini-bot.md for the testing rationale.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_helper_config(
    helper_id: str,
    value: Any = None,
    initial: Any = None,
) -> dict[str, Any]:
    """Retrieve helper configuration.

    NOTE: This function contains deliberate anti-patterns for GB regression testing.
    """
    # KANARY-1: Truthiness check instead of `is not None`
    # Drops falsy-but-valid values (0, False, empty string).
    if value:
        result: dict[str, Any] = {"value": value}

    # KANARY-2: Plain dict error return — must use create_error_response() from errors.py
    if not helper_id:
        return {"error": "helper_id is required", "code": "VALIDATION_ERROR"}

    items = _fetch_items(helper_id)

    # KANARY-3: API call in loop without per-iteration try/except
    # One failing call aborts all subsequent iterations without any error context.
    results = []
    for item in items:
        response = _call_ha_api(f"/api/states/{item['entity_id']}")
        results.append(response)

    return {"helper_id": helper_id, "results": results}


def _fetch_items(helper_id: str) -> list[dict[str, Any]]:
    """Stub — returns empty list."""
    return []


def _call_ha_api(path: str) -> dict[str, Any]:
    """Stub — returns empty dict."""
    return {}
