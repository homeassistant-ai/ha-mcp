"""Tool result views for the visibility outbound scan.

The scan needs both the flattened text (exact hidden-id and embedded-term
matching) and the structured values (trace ``domain`` + ``item_id`` records that
name no entity_id). ``serialize_result`` builds the text the regexes match;
``result_payloads`` returns the whole structured content plus each text block,
JSON-parsed when it parses, for the shape-aware checks.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ha_mcp._vendor.fastmcp.tools import ToolResult


def serialize_result(result: ToolResult) -> str:
    """Flatten a tool result's text content and structured content for scanning."""
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        try:
            parts.append(json.dumps(structured, default=str))
        except TypeError, ValueError:
            parts.append(str(structured))
    return "\n".join(parts)


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
