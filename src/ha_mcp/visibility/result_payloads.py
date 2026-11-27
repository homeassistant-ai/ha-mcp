"""Tool result views for the visibility outbound scan.

The scan needs both the flattened text (exact hidden-id and embedded-term
matching) and the structured values (trace ``domain`` + ``item_id`` records that
name no entity_id). ``serialize_result`` builds the text the regexes match;
``result_payloads`` returns the whole structured content plus each text block,
JSON-parsed when it parses, for the shape-aware checks. ``serialize_error``
builds the text for a tool error's payload.
"""

from __future__ import annotations

import json
from collections.abc import Collection
from typing import TYPE_CHECKING, Any

from ..tools.helpers import BP_WARNINGS_SUGGESTION_PREFIX

if TYPE_CHECKING:
    from ha_mcp._vendor.fastmcp.tools import ToolResult


def _without(payload: Any, keys: Collection[str]) -> Any:
    """``payload`` without its top-level ``keys``, when it is a mapping."""
    if not isinstance(payload, dict) or payload.keys().isdisjoint(keys):
        return payload
    return {name: value for name, value in payload.items() if name not in keys}


def _text_without(text: str, keys: Collection[str]) -> str:
    """A JSON text block without its top-level ``keys``; any other text as it is."""
    if not any(f'"{key}"' in text for key in keys):
        return text
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    return json.dumps(_without(payload, keys), default=str)


def serialize_result(result: ToolResult, without: Collection[str] = ()) -> str:
    """Flatten a tool result's text content and structured content for scanning.

    ``without`` names top-level keys of the result to leave out.
    """
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(_text_without(text, without))
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        try:
            parts.append(json.dumps(_without(structured, without), default=str))
        except TypeError, ValueError:
            parts.append(str(structured))
    return "\n".join(parts)


def serialize_error(exc: Exception, without: Collection[str] = ()) -> str:
    """Flatten a tool error's payload args for scanning.

    ``without`` names top-level keys of a JSON payload to leave out; the
    checker's warnings, which an error carries as one of its suggestions, are
    left out with them.
    """
    parts = [str(arg) for arg in exc.args]
    if without:
        parts = [_error_without(part, without) for part in parts]
    return "\n".join(parts)


def _error_without(text: str, keys: Collection[str]) -> str:
    """A JSON error payload without its top-level ``keys`` and the checker's
    entry in ``error.suggestions``; any other text as it is."""
    try:
        payload = json.loads(text)
    except ValueError:
        return text
    payload = _without(payload, keys)
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("suggestions"), list):
        error["suggestions"] = [
            hint
            for hint in error["suggestions"]
            if not (
                isinstance(hint, str) and hint.startswith(BP_WARNINGS_SUGGESTION_PREFIX)
            )
        ]
    return json.dumps(payload, default=str)


def result_payloads(result: ToolResult) -> list[Any]:
    """A tool result's structured content and text blocks, JSON-parsed if possible."""
    payloads: list[Any] = []
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        payloads.append(structured)
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            try:
                payloads.append(json.loads(text))
            except json.JSONDecodeError:
                payloads.append(text)
    return payloads
