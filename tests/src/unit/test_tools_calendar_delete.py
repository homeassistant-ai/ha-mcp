"""Unit tests for ``ha_config_remove_calendar_event`` pooled-WS routing (#1813).

``calendar.delete_event`` is not a REST service — delete lives exclusively on
the WebSocket command ``calendar/event/delete``, now routed through the shared
pooled client (``client.send_websocket_message``) instead of a per-call
dedicated connection. A failed WS command comes back as ``{"success": False}``
and is re-raised so the failure reaches the tool's existing error handler.

The tool runs on the server's own client against a real ``hass`` whose
``calendar.test`` is Core's Local Calendar; what a delete removed is read back
from Core's calendar view.
"""

import datetime as dt
import json
from contextlib import AbstractContextManager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import MockHAClientWebSocket, WebSocketGenerator

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_calendar
from ha_mcp.tools.tools_calendar import CalendarTools

from ._calendars import calendar_config_dir, listed_events, local_calendar, write_event

START = dt.datetime(2026, 1, 1, 10)


# Core's config directory is this test's ``tmp_path``.
hass_config_dir = calendar_config_dir


@pytest.fixture
async def ws(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_ws_client: WebSocketGenerator,
) -> MockHAClientWebSocket:
    """A WebSocket client on ``hass``, whose ``calendar.test`` is Local Calendar."""
    await local_calendar(hass, "Test")
    return await hass_ws_client(hass)


def _spy(
    client: HomeAssistantClient,
) -> AbstractContextManager[MagicMock | AsyncMock]:
    """A spy on the client's WebSocket bridge; Core still answers."""
    return patch.object(
        client, "send_websocket_message", wraps=client.send_websocket_message
    )


async def test_delete_routes_via_pooled_websocket(
    ws: MockHAClientWebSocket, core_client: HomeAssistantClient
) -> None:
    await write_event(ws, "calendar.test", "Dentist", START)
    [event] = await listed_events(core_client, "calendar.test")
    tools = CalendarTools(core_client)
    with _spy(core_client) as send:
        result = await tools.ha_config_remove_calendar_event(
            entity_id="calendar.test",
            uid=event["uid"],
        )

    send.assert_awaited_once()
    message = send.await_args_list[0].args[0]
    assert message["type"] == "calendar/event/delete"
    assert message["entity_id"] == "calendar.test"
    assert message["uid"] == event["uid"]
    # recurrence params omitted when unset.
    assert "recurrence_id" not in message
    assert "recurrence_range" not in message
    assert result["success"] is True
    assert result["uid"] == event["uid"]
    assert await listed_events(core_client, "calendar.test") == []
    # The dedicated-connection helper is gone from the module namespace.
    assert not hasattr(tools_calendar, "get_connected_ws_client")


async def test_delete_forwards_recurrence_params(
    ws: MockHAClientWebSocket, core_client: HomeAssistantClient
) -> None:
    # Only the occurrence id together with the range leaves the first day alone
    # and removes the two after it.
    await write_event(ws, "calendar.test", "Standup", START, rrule="FREQ=DAILY;COUNT=3")
    first, second, _ = await listed_events(core_client, "calendar.test")
    tools = CalendarTools(core_client)
    with _spy(core_client) as send:
        await tools.ha_config_remove_calendar_event(
            entity_id="calendar.test",
            uid=second["uid"],
            recurrence_id=second["recurrence_id"],
            recurrence_range="THISANDFUTURE",
        )

    message = send.await_args_list[0].args[0]
    assert message["recurrence_id"] == second["recurrence_id"]
    assert message["recurrence_range"] == "THISANDFUTURE"
    left = await listed_events(core_client, "calendar.test")
    assert [event["recurrence_id"] for event in left] == [first["recurrence_id"]]


async def test_delete_failure_surfaces_structured_error(
    ws: MockHAClientWebSocket, core_client: HomeAssistantClient
) -> None:
    # Core refuses the delete (no such calendar); the pooled client collapses
    # that into {"success": False} and the tool re-raises so the outer handler
    # builds the delete-specific suggestions.
    await write_event(ws, "calendar.test", "Dentist", START)
    [event] = await listed_events(core_client, "calendar.test")
    tools = CalendarTools(core_client)
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_remove_calendar_event(
            entity_id="calendar.missing",
            uid=event["uid"],
        )

    err = json.loads(str(exc_info.value))["error"]
    suggestions = err.get("suggestions") or [err.get("suggestion", "")]
    # The delete handler prepends a not-found hint referencing entity/uid.
    assert any(
        "not found" in s.lower() or "uid" in s.lower() or "event" in s.lower()
        for s in suggestions
    )
    assert await listed_events(core_client, "calendar.test") == [event]


@pytest.mark.parametrize("bad", ["", "   "])
async def test_delete_rejects_blank_recurrence_id(
    core_client: HomeAssistantClient, bad: str
) -> None:
    """A blank recurrence_id reaches HA as a real one and matches nothing."""
    tools = CalendarTools(core_client)
    with _spy(core_client) as send, pytest.raises(ToolError) as exc_info:
        await tools.ha_config_remove_calendar_event(
            entity_id="calendar.test",
            uid="series-1",
            recurrence_id=bad,
        )

    message = str(exc_info.value)
    assert "VALIDATION_INVALID_PARAMETER" in message
    assert '"parameter": "recurrence_id"' in message, message
    send.assert_not_awaited()


async def test_delete_rejects_recurrence_range_without_recurrence_id(
    core_client: HomeAssistantClient,
) -> None:
    """Without an occurrence id, ical deletes the ENTIRE series instead."""
    tools = CalendarTools(core_client)
    with _spy(core_client) as send, pytest.raises(ToolError) as exc_info:
        await tools.ha_config_remove_calendar_event(
            entity_id="calendar.test",
            uid="series-1",
            recurrence_range="THISANDFUTURE",
        )

    assert "VALIDATION_INVALID_PARAMETER" in str(exc_info.value)
    assert "recurrence_id" in str(exc_info.value)
    send.assert_not_awaited()
