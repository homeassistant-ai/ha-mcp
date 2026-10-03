"""Simple-helper schemas, reads and writes through the component's collection commands.

Every function returns ``None`` when the component cannot serve the request
and the caller should use Core's WebSocket commands instead. A write falls back
only when it provably never ran: the capability is absent, the command was not
sent, Core answered ``unknown_command``, or the component found no collection.
"""

from __future__ import annotations

import logging
import time
import weakref
from typing import Any, NoReturn

from ..client.rest_client import HomeAssistantCommandNotSent
from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)

WS_HELPER_SCHEMAS = "ha_mcp_tools/helper_schemas"
WS_HELPER_ITEM = "ha_mcp_tools/helper_item"
WS_HELPER_WRITE = "ha_mcp_tools/helper_write"
HELPER_CAPABILITIES = ("helper_schemas", "helper_item", "helper_write")

# Core's schemas change only with a Core upgrade, which restarts the WS session.
_SCHEMA_TTL_S = 300.0
_SCHEMA_CACHE: weakref.WeakKeyDictionary[Any, tuple[float, dict[str, Any]]] = (
    weakref.WeakKeyDictionary()
)


async def _component_ws(client: Any) -> Any | None:
    """The client's WS connection when the component serves every helper command."""
    if not getattr(client, "base_url", None) or not getattr(client, "token", None):
        return None
    caps = await get_component_caps(client)
    if not all(component_supports(caps, name) for name in HELPER_CAPABILITIES):
        return None
    return await get_websocket_client(
        url=client.base_url,
        token=client.token,
        verify_ssl=getattr(client, "verify_ssl", None),
    )


async def _read(client: Any, command: str, **kwargs: Any) -> dict[str, Any] | None:
    """Send a read command; any failure means "use the legacy path"."""
    try:
        ws = await _component_ws(client)
        if ws is None:
            return None
        raw = await ws.send_command(command, **kwargs)
    except Exception as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        logger.warning(
            "%s failed; using Core's commands: %r", command, exc, exc_info=True
        )
        return None
    result = raw.get("result") if isinstance(raw, dict) else None
    return result if isinstance(result, dict) else None


async def fetch_helper_schemas(client: Any) -> dict[str, Any] | None:
    """Core's ``{helper_type: {"create": [...], "update": [...]}}`` field lists."""
    cached = _SCHEMA_CACHE.get(client)
    if cached is not None and time.monotonic() - cached[0] < _SCHEMA_TTL_S:
        return cached[1]
    result = await _read(client, WS_HELPER_SCHEMAS)
    types = result.get("types") if result else None
    if not isinstance(types, dict):
        return None
    _SCHEMA_CACHE[client] = (time.monotonic(), types)
    return types


async def read_helper_item(
    client: Any,
    helper_type: str,
    *,
    entity_id: str | None = None,
    item_id: str | None = None,
) -> dict[str, Any] | None:
    """The stored item and its id; ``None`` lets the legacy path read (and report)."""
    target = {"item_id": item_id} if item_id is not None else {"entity_id": entity_id}
    result = await _read(client, WS_HELPER_ITEM, helper_type=helper_type, **target)
    if not result or result.get("success") is not True:
        return None
    return result


def _raise_outcome_unknown(
    helper_type: str, action: str, exc: BaseException
) -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"The {helper_type} {action} was sent but its outcome is unknown: {exc}",
            context={"helper_type": helper_type, "write_outcome_unknown": True},
            suggestions=[
                "Check with ha_config_list_helpers before retrying, so a create "
                "is not repeated"
            ],
        )
    )


def _write_outcome(
    result: Any, helper_type: str, action: str, error_context: dict[str, Any]
) -> dict[str, Any] | None:
    """A success result, ``None`` when nothing ran, else the structured error."""
    if not isinstance(result, dict):
        _raise_outcome_unknown(helper_type, action, ValueError("malformed reply"))
    if result.get("success") is True:
        return result
    error = result.get("error") or {}
    if error.get("code") == "unavailable":
        return None
    code = (
        ErrorCode.CONFIG_NOT_FOUND
        if error.get("code") == "not_found"
        else ErrorCode.VALIDATION_INVALID_PARAMETER
    )
    raise_tool_error(
        create_error_response(
            code,
            f"Failed to {action} {helper_type}: {error.get('message', 'unknown error')}",
            context=error_context,
        )
    )
    return None  # py/mixed-returns: unreachable, raise_tool_error raises


async def write_helper_item(
    client: Any,
    helper_type: str,
    action: str,
    data: dict[str, Any],
    *,
    item_id: str | None = None,
    registry: dict[str, Any] | None = None,
    error_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Create/update a stored item plus its registry fields in one in-process call."""
    try:
        ws = await _component_ws(client)
    except Exception:
        logger.warning("Component helper_write unavailable", exc_info=True)
        return None
    if ws is None:
        return None
    kwargs: dict[str, Any] = {
        "helper_type": helper_type,
        "action": action,
        "data": data,
    }
    if item_id is not None:
        kwargs["item_id"] = item_id
    if registry:
        kwargs["registry"] = registry
    try:
        raw = await ws.send_command(WS_HELPER_WRITE, **kwargs)
    except HomeAssistantCommandNotSent:
        return None
    except Exception as exc:  # noqa: BLE001 - ends in a tool error
        # Cancellation propagates; only a definitive unknown_command falls back.
        if is_unknown_command(exc):
            invalidate_caps(client)
            return None
        _raise_outcome_unknown(helper_type, action, exc)
    return _write_outcome(
        raw.get("result") if isinstance(raw, dict) else None,
        helper_type,
        action,
        error_context or {"helper_type": helper_type},
    )


def collection_payload(helper_type: str, message: dict[str, Any]) -> dict[str, Any]:
    """A ``{type}/create|update`` WS message's item fields.

    ``<type>_id`` addresses the item on update; on a tag create it is a field.
    """
    drop = {"type"}
    if str(message.get("type", "")).endswith("/update"):
        drop.add(f"{helper_type}_id")
    return {key: value for key, value in message.items() if key not in drop}


def native_result(
    helper_type: str, result: dict[str, Any]
) -> tuple[dict[str, Any], str, list[str]]:
    """Split a component write into (helper data, entity_id, warnings)."""
    item = result.get("item") or {}
    entity_id = result.get("entity_id") or f"{helper_type}.{item.get('id')}"
    data = {**item, **(result.get("registry_applied") or {})}
    return data, entity_id, list(result.get("warnings") or [])


async def _tag_entities(client: Any) -> list[dict[str, Any]]:
    listed = await client.send_websocket_message(
        {"type": "config/entity_registry/list"}
    )
    return [e for e in listed.get("result") or [] if e.get("platform") == "tag"]


async def tag_entity_id(client: Any, tag_id: str | None) -> str | None:
    """The entity of a tag: its entity_id follows the tag's name, not its ID."""
    if not tag_id:
        return None
    return next(
        (
            e.get("entity_id")
            for e in await _tag_entities(client)
            if e.get("unique_id") == tag_id
        ),
        None,
    )


async def tag_item_id(client: Any, helper_id: str) -> str:
    """A tag's ID from its entity_id, its ID, or ``tag.<id>``."""
    if not helper_id.startswith("tag."):
        return helper_id
    return next(
        (
            e["unique_id"]
            for e in await _tag_entities(client)
            if e.get("entity_id") == helper_id and e.get("unique_id")
        ),
        helper_id.removeprefix("tag."),
    )
