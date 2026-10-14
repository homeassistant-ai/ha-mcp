"""Remind agents of the home context documents covering what they just changed.

The home context is enforced softly: a write is never blocked, but when a
write tool succeeds and a stored document covers one of the entities, devices,
areas or floors in its arguments (or the 'home' document exists), the result
gains a warning naming those documents and their revisions. An agent that did
not read them learns about them, and one that did can tell whether its change
made them outdated.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from ha_mcp._vendor.fastmcp.server.middleware.middleware import (
    CallNext,
    Middleware,
    MiddlewareContext,
)
from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp._vendor.fastmcp.tools.base import default_serializer
from ha_mcp._vendor.mcp_types import TextContent

from ..read_only import is_read_safe
from ..renamed_tools import current_tool_name
from ..tool_dispatch import CALL_PROXY_META_TOOLS
from .component_home_context import match_scopes

logger = logging.getLogger(__name__)

HOME_CONTEXT_TOOLS = frozenset({"ha_get_home_context", "ha_manage_home_context"})
_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_ID_KEYS = {
    "device_id": "device_ids",
    "device_ids": "device_ids",
    "area_id": "area_ids",
    "area_ids": "area_ids",
    "floor_id": "floor_ids",
    "floor_ids": "floor_ids",
}
_MAX_DEPTH = 8
_MAX_IDS = 200


def _add(targets: dict[str, list[str]], kind: str, value: Any) -> None:
    values = value if isinstance(value, list) else [value]
    bucket = targets.setdefault(kind, [])
    for item in values:
        if isinstance(item, str) and item and item not in bucket:
            if len(bucket) < _MAX_IDS:
                bucket.append(item)


def _entity_ids_in(text: str) -> list[str]:
    # ha_call_service accepts several ids comma-separated.
    parts = (part.strip() for part in text.split(","))
    return [part for part in parts if _ENTITY_ID.match(part)]


def collect_targets(arguments: Any) -> dict[str, list[str]]:
    """The entity, device, area and floor ids named anywhere in a call's arguments.

    Entity ids are recognised by shape wherever they appear, so the targets
    inside an automation or dashboard config count too. A service name such
    as ``light.turn_on`` has the same shape; it matches no stored document,
    so it costs nothing.
    """
    targets: dict[str, list[str]] = {}

    def walk(node: Any, depth: int) -> None:
        if depth > _MAX_DEPTH:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                kind = _ID_KEYS.get(key) if isinstance(key, str) else None
                if kind is not None:
                    _add(targets, kind, value)
                else:
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
        elif isinstance(node, str):
            _add(targets, "entity_ids", _entity_ids_in(node))

    walk(arguments, 0)
    return targets


def hint_text(scopes: Sequence[dict[str, Any]]) -> str:
    listed = ", ".join(f"{s['scope']} (revision {s['revision']})" for s in scopes)
    return (
        f"Home context covers this change: {listed}. Read it with "
        "ha_get_home_context before further changes here, and update it with "
        "ha_manage_home_context if this change made it outdated."
    )


def add_warning(result: Any, warning: str) -> None:
    """Add ``warning`` to a tool result's ``warnings`` and its text form."""
    structured = getattr(result, "structured_content", None)
    content = getattr(result, "content", None)
    if not isinstance(content, list):
        return
    text_mirrors_structured = (
        isinstance(structured, dict)
        and len(content) == 1
        and isinstance(content[0], TextContent)
        and content[0].text == default_serializer(structured)
    )
    if isinstance(structured, dict) and isinstance(
        structured.get("warnings", []), list
    ):
        structured["warnings"] = [*structured.get("warnings", []), warning]
        if text_mirrors_structured:
            content[0].text = default_serializer(structured)
            return
    content.append(TextContent(type="text", text=warning))


class HomeContextHintMiddleware(Middleware):
    """Append a home context warning to successful write tool results."""

    def __init__(
        self,
        *,
        get_client: Callable[[], Any],
        list_tools: Callable[[], Awaitable[Sequence[Tool]]],
    ) -> None:
        self._get_client = get_client
        self._list_tools = list_tools
        self._read_safe: dict[str, bool] = {}

    async def _is_write(self, name: str) -> bool:
        if name not in self._read_safe:
            tools = await self._list_tools()
            self._read_safe = {t.name: is_read_safe(t) for t in tools}
        return self._read_safe.get(name) is False

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        result = await call_next(context)
        name = current_tool_name(context.message.name)
        if name in HOME_CONTEXT_TOOLS or name in CALL_PROXY_META_TOOLS:
            return result
        try:
            if not await self._is_write(name):
                return result
            targets = collect_targets(context.message.arguments or {})
            scopes = await match_scopes(self._get_client(), targets)
            if scopes:
                add_warning(result, hint_text(scopes))
        except Exception:
            logger.warning("home context hint for %s failed", name, exc_info=True)
        return result
