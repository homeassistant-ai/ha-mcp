"""Todo item reads and writes through the component's ``ha_mcp_tools/todo_item``.

The component resolves an item with Core's own lookup, writes through Core's
todo services and reads the stored rows back from the list entity.
``todo_item`` returns ``None`` when the component did not run the action, so
the caller uses Core's WebSocket commands instead: the capability is absent,
the command was not sent, Core answered ``unknown_command``, or the component
lacks Core's todo parts. A reply with ``success: False`` is the component's
refusal: a check before the write, or Core's schema or validation error after
which the list's rows were unchanged. The one exception is ``move_failed``: the
update was applied and the move after it failed, as its message says.
"""

from __future__ import annotations

from typing import Any

from ..client.rest_client import HomeAssistantCommandNotSent, HomeAssistantError
from ..errors import ErrorCode, create_error_response
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
)
from .helpers import raise_tool_error

WS_TODO_ITEM = "ha_mcp_tools/todo_item"
CAPABILITY = "todo_item"

# ``unsupported`` is absent: ``todo_item`` turns it into the fallback.
_REFUSAL_CODES = {
    "not_loaded": ErrorCode.ENTITY_UNAVAILABLE,
    "list_unavailable": ErrorCode.ENTITY_UNAVAILABLE,
    "item_not_found": ErrorCode.RESOURCE_NOT_FOUND,
    "not_supported": ErrorCode.VALIDATION_INVALID_PARAMETER,
    "namesake": ErrorCode.VALIDATION_INVALID_PARAMETER,
    "invalid": ErrorCode.VALIDATION_INVALID_PARAMETER,
}


async def todo_item(
    client: Any, entity_id: str, action: str, **fields: Any
) -> dict[str, Any] | None:
    """The component's reply to one action on ``entity_id``; None when it did
    not run it. A sent command that got no answer, or that the component
    failed to answer, raises ``HomeAssistantError``: a write's outcome is then
    unknown."""
    if not component_supports(await get_component_caps(client), CAPABILITY):
        return None
    try:
        result = await client.send_websocket_message(
            {"type": WS_TODO_ITEM, "entity_id": entity_id, "action": action, **fields}
        )
    except HomeAssistantCommandNotSent:
        return None
    if not result.get("success"):
        if result.get("error_code") == "unknown_command":
            invalidate_caps(client)
            return None
        raise HomeAssistantError(f"{WS_TODO_ITEM} failed: {result.get('error')}")
    reply = result.get("result")
    if not isinstance(reply, dict):
        raise HomeAssistantError(f"{WS_TODO_ITEM} answered without a reply: {result}")
    if (reply.get("error") or {}).get("code") == "unsupported":
        return None
    return reply


async def todo_call(
    client: Any, entity_id: str, action: str, **fields: Any
) -> dict[str, Any] | None:
    """``todo_item`` for a tool: a refusal raises its tool error, and a write
    sent without an answer is reported as unknown."""
    try:
        reply = await todo_item(client, entity_id, action, **fields)
    except HomeAssistantError as e:
        if action == "resolve":
            raise
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"{WS_TODO_ITEM} {action} did not return its outcome, so "
                f"whether it was applied is unknown: {e}",
                context={"entity_id": entity_id, "write_outcome_unknown": True},
                suggestions=["Check with ha_get_todo() before retrying"],
            )
        )
    if reply is None or reply.get("success"):
        return reply
    error = reply.get("error") or {}
    code = str(error.get("code"))
    raise_tool_error(
        create_error_response(
            _REFUSAL_CODES.get(code, ErrorCode.SERVICE_CALL_FAILED),
            str(error.get("message", "Unknown error")),
            context={"entity_id": entity_id, **fields},
            suggestions=["Rename the item named after the uid first"]
            if code == "namesake"
            else None,
        )
    )
    return None  # py/mixed-returns: unreachable, raise_tool_error raises


async def todo_write(
    client: Any, entity_id: str, action: str, **fields: Any
) -> dict[str, Any]:
    """``todo_call`` for a write whose item the component resolved: a None
    reply raises instead of falling back to Core's services."""
    reply = await todo_call(client, entity_id, action, **fields)
    if reply is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                f"{WS_TODO_ITEM} {action} was not sent",
                context={"entity_id": entity_id},
            )
        )
    return reply
