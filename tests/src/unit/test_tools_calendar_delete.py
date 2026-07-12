"""Unit tests for ``ha_config_remove_calendar_event`` pooled-WS routing (#1813).

``calendar.delete_event`` is not a REST service — delete lives exclusively on
the WebSocket command ``calendar/event/delete``, now routed through the shared
pooled client (``client.send_websocket_message``) instead of a per-call
dedicated connection. A failed WS command comes back as ``{"success": False}``
and is re-raised so the failure reaches the tool's existing error handler.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools import tools_calendar
from ha_mcp.tools.tools_calendar import CalendarTools


def _make_mock_client(ws_return: dict | None = None) -> MagicMock:
    client = MagicMock()
    client.base_url = "http://ha.local:8123"
    client.token = "test-token"
    client.verify_ssl = True
    client.send_websocket_message = AsyncMock(
        return_value=ws_return or {"success": True, "result": None}
    )
    return client


@pytest.mark.asyncio
async def test_delete_routes_via_pooled_websocket():
    client = _make_mock_client(ws_return={"success": True, "result": {"ok": True}})

    tools = CalendarTools(client)
    result = await tools.ha_config_remove_calendar_event(
        entity_id="calendar.test",
        uid="evt-123",
    )

    client.send_websocket_message.assert_awaited_once()
    message = client.send_websocket_message.await_args.args[0]
    assert message["type"] == "calendar/event/delete"
    assert message["entity_id"] == "calendar.test"
    assert message["uid"] == "evt-123"
    # recurrence params omitted when unset.
    assert "recurrence_id" not in message
    assert "recurrence_range" not in message
    assert result["success"] is True
    assert result["uid"] == "evt-123"
    # The dedicated-connection helper is gone from the module namespace.
    assert not hasattr(tools_calendar, "get_connected_ws_client")


@pytest.mark.asyncio
async def test_delete_forwards_recurrence_params():
    client = _make_mock_client()

    tools = CalendarTools(client)
    await tools.ha_config_remove_calendar_event(
        entity_id="calendar.test",
        uid="evt-123",
        recurrence_id="20260101T100000",
        recurrence_range="THIS_AND_FUTURE",
    )

    message = client.send_websocket_message.await_args.args[0]
    assert message["recurrence_id"] == "20260101T100000"
    assert message["recurrence_range"] == "THIS_AND_FUTURE"


@pytest.mark.asyncio
async def test_delete_failure_surfaces_structured_error():
    # Pooled client collapses a failed WS command into {"success": False}; the
    # tool re-raises so the outer handler builds the delete-specific suggestions.
    client = _make_mock_client(
        ws_return={"success": False, "error": "Command failed: event not found"}
    )

    tools = CalendarTools(client)
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_remove_calendar_event(
            entity_id="calendar.test",
            uid="missing",
        )

    err = json.loads(str(exc_info.value))["error"]
    suggestions = err.get("suggestions") or [err.get("suggestion", "")]
    # The delete handler prepends a not-found hint referencing entity/uid.
    assert any(
        "not found" in s.lower() or "uid" in s.lower() or "event" in s.lower()
        for s in suggestions
    )
