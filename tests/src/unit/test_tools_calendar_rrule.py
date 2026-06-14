"""Unit tests for recurring-event support in ``ha_config_set_calendar_event``.

The ``calendar.create_event`` REST service schema has no ``rrule`` field —
recurrence is only accepted by the WebSocket command
``calendar/event/create``. These tests pin the transport routing: ``rrule``
present → WebSocket command carrying an RFC 5545 event payload
(``dtstart``/``dtend`` keys); ``rrule`` absent → the pre-existing REST
service call (``start_date_time``/``end_date_time`` keys), unchanged.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.tools.tools_calendar import CalendarTools


def _make_mock_client() -> MagicMock:
    client = MagicMock()
    client.base_url = "http://ha.local:8123"
    client.token = "test-token"
    client.verify_ssl = True
    client.call_service = AsyncMock(return_value={"success": True})
    return client


def _make_mock_ws() -> AsyncMock:
    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value={"success": True, "result": None})
    return ws


@pytest.mark.asyncio
async def test_rrule_routes_via_websocket_event_create():
    """rrule present → calendar/event/create WS command, REST service untouched."""
    client = _make_mock_client()
    ws = _make_mock_ws()

    with patch(
        "ha_mcp.tools.tools_calendar.get_connected_ws_client",
        return_value=(ws, None),
    ):
        tools = CalendarTools(client)
        result = await tools.ha_config_set_calendar_event(
            entity_id="calendar.test",
            summary="Weekly sync",
            start="2026-06-15T10:00:00",
            end="2026-06-15T10:30:00",
            description="desc",
            location="loc",
            rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=10",
        )

    ws.send_command.assert_awaited_once()
    args, kwargs = ws.send_command.await_args
    assert args == ("calendar/event/create",)
    assert kwargs["entity_id"] == "calendar.test"
    assert kwargs["event"] == {
        "summary": "Weekly sync",
        "dtstart": "2026-06-15T10:00:00",
        "dtend": "2026-06-15T10:30:00",
        "rrule": "FREQ=WEEKLY;BYDAY=MO;COUNT=10",
        "description": "desc",
        "location": "loc",
    }
    ws.disconnect.assert_awaited_once()
    client.call_service.assert_not_awaited()
    assert result["success"] is True
    assert result["event"]["rrule"] == "FREQ=WEEKLY;BYDAY=MO;COUNT=10"


@pytest.mark.asyncio
async def test_rrule_omits_unset_optional_fields_from_ws_event():
    """Unset description/location must not appear in the WS event payload."""
    client = _make_mock_client()
    ws = _make_mock_ws()

    with patch(
        "ha_mcp.tools.tools_calendar.get_connected_ws_client",
        return_value=(ws, None),
    ):
        tools = CalendarTools(client)
        await tools.ha_config_set_calendar_event(
            entity_id="calendar.test",
            summary="Bare recurring",
            start="2026-06-20T09:00:00",
            end="2026-06-20T09:30:00",
            rrule="FREQ=MONTHLY;BYDAY=3SA",
        )

    _, kwargs = ws.send_command.await_args
    assert kwargs["event"] == {
        "summary": "Bare recurring",
        "dtstart": "2026-06-20T09:00:00",
        "dtend": "2026-06-20T09:30:00",
        "rrule": "FREQ=MONTHLY;BYDAY=3SA",
    }


@pytest.mark.asyncio
async def test_no_rrule_keeps_rest_service_path():
    """rrule absent → existing calendar.create_event service call, no WS."""
    client = _make_mock_client()

    with patch("ha_mcp.tools.tools_calendar.get_connected_ws_client") as ws_factory:
        tools = CalendarTools(client)
        result = await tools.ha_config_set_calendar_event(
            entity_id="calendar.test",
            summary="One-off",
            start="2026-06-15T10:00:00",
            end="2026-06-15T11:00:00",
        )

    client.call_service.assert_awaited_once_with(
        "calendar",
        "create_event",
        {
            "entity_id": "calendar.test",
            "summary": "One-off",
            "start_date_time": "2026-06-15T10:00:00",
            "end_date_time": "2026-06-15T11:00:00",
        },
    )
    ws_factory.assert_not_called()
    assert result["success"] is True
    assert result["event"]["rrule"] is None
