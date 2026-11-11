"""Unit tests for ``ha_config_remove_calendar_event`` pooled-WS routing (#1813).

``calendar.delete_event`` is not a REST service — delete lives exclusively on
the WebSocket command ``calendar/event/delete``, now routed through the shared
pooled client (``client.send_websocket_message``) instead of a per-call
dedicated connection. A failed WS command comes back as ``{"success": False}``
and is re-raised so the failure reaches the tool's existing error handler.

The tool runs on the server's own client against a real ``hass``, whose
``calendar`` integration serves a calendar of a test integration that
supports deleting events.
"""

import datetime as dt
import json
from contextlib import AbstractContextManager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.calendar import (
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import setup_test_component_platform

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_calendar
from ha_mcp.tools.tools_calendar import CalendarTools


class DeletableCalendar(CalendarEntity):
    """``calendar.test``: records each delete Core hands it."""

    _attr_name = "Test"
    _attr_supported_features = CalendarEntityFeature.DELETE_EVENT

    def __init__(self) -> None:
        self.deleted: list[tuple[str, str | None, str | None]] = []

    @property
    def event(self) -> CalendarEvent | None:
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: dt.datetime, end_date: dt.datetime
    ) -> list[CalendarEvent]:
        return []

    async def async_delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        self.deleted.append((uid, recurrence_id, recurrence_range))


@pytest.fixture
async def calendar(hass: HomeAssistant) -> DeletableCalendar:
    entity = DeletableCalendar()
    setup_test_component_platform(hass, "calendar", [entity])
    assert await async_setup_component(
        hass, "calendar", {"calendar": {"platform": "test"}}
    )
    await hass.async_block_till_done()
    return entity


def _spy(
    client: HomeAssistantClient,
) -> AbstractContextManager[MagicMock | AsyncMock]:
    """A spy on the client's WebSocket bridge; Core still answers."""
    return patch.object(
        client, "send_websocket_message", wraps=client.send_websocket_message
    )


async def test_delete_routes_via_pooled_websocket(
    calendar: DeletableCalendar, core_client: HomeAssistantClient
) -> None:
    tools = CalendarTools(core_client)
    with _spy(core_client) as send:
        result = await tools.ha_config_remove_calendar_event(
            entity_id="calendar.test",
            uid="evt-123",
        )

    send.assert_awaited_once()
    message = send.await_args_list[0].args[0]
    assert message["type"] == "calendar/event/delete"
    assert message["entity_id"] == "calendar.test"
    assert message["uid"] == "evt-123"
    # recurrence params omitted when unset.
    assert "recurrence_id" not in message
    assert "recurrence_range" not in message
    assert result["success"] is True
    assert result["uid"] == "evt-123"
    assert calendar.deleted == [("evt-123", None, None)]
    # The dedicated-connection helper is gone from the module namespace.
    assert not hasattr(tools_calendar, "get_connected_ws_client")


async def test_delete_forwards_recurrence_params(
    calendar: DeletableCalendar, core_client: HomeAssistantClient
) -> None:
    tools = CalendarTools(core_client)
    with _spy(core_client) as send:
        await tools.ha_config_remove_calendar_event(
            entity_id="calendar.test",
            uid="evt-123",
            recurrence_id="20260101T100000",
            recurrence_range="THISANDFUTURE",
        )

    message = send.await_args_list[0].args[0]
    assert message["recurrence_id"] == "20260101T100000"
    assert message["recurrence_range"] == "THISANDFUTURE"
    assert calendar.deleted == [("evt-123", "20260101T100000", "THISANDFUTURE")]


async def test_delete_failure_surfaces_structured_error(
    calendar: DeletableCalendar, core_client: HomeAssistantClient
) -> None:
    # Core refuses the delete (no such calendar); the pooled client collapses
    # that into {"success": False} and the tool re-raises so the outer handler
    # builds the delete-specific suggestions.
    tools = CalendarTools(core_client)
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_remove_calendar_event(
            entity_id="calendar.missing",
            uid="missing",
        )

    err = json.loads(str(exc_info.value))["error"]
    suggestions = err.get("suggestions") or [err.get("suggestion", "")]
    # The delete handler prepends a not-found hint referencing entity/uid.
    assert any(
        "not found" in s.lower() or "uid" in s.lower() or "event" in s.lower()
        for s in suggestions
    )
    assert calendar.deleted == []


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
