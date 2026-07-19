"""Unit tests for recurring-event support in ``ha_config_set_calendar_event``.

The ``calendar.create_event`` REST service schema has no ``rrule`` field —
recurrence is only accepted by the WebSocket command ``calendar/event/create``.
Since issue #1813 that WS command routes through the shared pooled client
(``client.send_websocket_message``) rather than a per-call dedicated connection.

These tests pin the transport routing: ``rrule`` present → pooled WS command
carrying an RFC 5545 event payload (``dtstart``/``dtend`` keys); ``rrule``
absent → the REST service call. Datetime values use ``start_date_time``/
``end_date_time`` while date-only values use ``start_date``/``end_date``.

They also cover the rrule-branch failure path: a failed WS command comes back
from the pooled client as ``{"success": False, ...}`` and is re-raised so the
caller's handler still attaches the RRULE-syntax suggestion. ``raise_tool_error``
serialises the structured error as JSON into the ``ToolError`` message, so the
failure-path tests parse it back to assert on suggestions.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tools import tools_calendar
from ha_mcp.tools.tools_calendar import CalendarTools


def _make_mock_client(ws_return: dict | None = None) -> MagicMock:
    client = MagicMock()
    client.base_url = "http://ha.local:8123"
    client.token = "test-token"
    client.verify_ssl = True
    client.call_service = AsyncMock(return_value={"success": True})
    client.send_websocket_message = AsyncMock(
        return_value=ws_return or {"success": True, "result": None}
    )
    return client


@pytest.mark.asyncio
async def test_rrule_routes_via_websocket_event_create():
    """rrule present → calendar/event/create pooled WS command, REST untouched."""
    client = _make_mock_client()

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

    client.send_websocket_message.assert_awaited_once()
    message = client.send_websocket_message.await_args.args[0]
    assert message["type"] == "calendar/event/create"
    assert message["entity_id"] == "calendar.test"
    assert message["event"] == {
        "summary": "Weekly sync",
        "dtstart": "2026-06-15T10:00:00",
        "dtend": "2026-06-15T10:30:00",
        "rrule": "FREQ=WEEKLY;BYDAY=MO;COUNT=10",
        "description": "desc",
        "location": "loc",
    }
    client.call_service.assert_not_awaited()
    # The dedicated-connection helper is gone from the module namespace, so the
    # tool cannot fall back to a per-call connect/auth handshake.
    assert not hasattr(tools_calendar, "get_connected_ws_client")
    assert result["success"] is True
    assert result["event"]["rrule"] == "FREQ=WEEKLY;BYDAY=MO;COUNT=10"


@pytest.mark.asyncio
async def test_rrule_omits_unset_optional_fields_from_ws_event():
    """Unset description/location must not appear in the WS event payload."""
    client = _make_mock_client()

    tools = CalendarTools(client)
    await tools.ha_config_set_calendar_event(
        entity_id="calendar.test",
        summary="Bare recurring",
        start="2026-06-20T09:00:00",
        end="2026-06-20T09:30:00",
        rrule="FREQ=MONTHLY;BYDAY=3SA",
    )

    message = client.send_websocket_message.await_args.args[0]
    assert message["event"] == {
        "summary": "Bare recurring",
        "dtstart": "2026-06-20T09:00:00",
        "dtend": "2026-06-20T09:30:00",
        "rrule": "FREQ=MONTHLY;BYDAY=3SA",
    }


@pytest.mark.asyncio
async def test_rrule_forwards_explicit_empty_string_fields_to_ws_event():
    """An explicitly-passed "" must reach HA (it clears the field there),
    unlike an unset (None) field, which is omitted entirely."""
    client = _make_mock_client()

    tools = CalendarTools(client)
    await tools.ha_config_set_calendar_event(
        entity_id="calendar.test",
        summary="Bare recurring",
        start="2026-06-20T09:00:00",
        end="2026-06-20T09:30:00",
        description="",
        location="",
        rrule="FREQ=MONTHLY;BYDAY=3SA",
    )

    message = client.send_websocket_message.await_args.args[0]
    assert message["event"]["description"] == ""
    assert message["event"]["location"] == ""


@pytest.mark.asyncio
async def test_no_rrule_keeps_rest_service_path():
    """rrule absent → existing calendar.create_event service call, no WS."""
    client = _make_mock_client()

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
    client.send_websocket_message.assert_not_awaited()
    assert result["success"] is True
    assert result["event"]["rrule"] is None


@pytest.mark.asyncio
async def test_no_rrule_date_only_values_create_all_day_event():
    """Date-only values must use HA's all-day service fields."""
    client = _make_mock_client()

    tools = CalendarTools(client)
    await tools.ha_config_set_calendar_event(
        entity_id="calendar.test",
        summary="School holidays",
        start="2026-07-04",
        end="2026-08-10",
    )

    client.call_service.assert_awaited_once_with(
        "calendar",
        "create_event",
        {
            "entity_id": "calendar.test",
            "summary": "School holidays",
            "start_date": "2026-07-04",
            "end_date": "2026-08-10",
        },
    )


@pytest.mark.asyncio
async def test_no_rrule_rejects_mixed_date_and_datetime_values():
    """Mixed all-day and timed boundaries are ambiguous and invalid."""
    client = _make_mock_client()

    tools = CalendarTools(client)
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_calendar_event(
            entity_id="calendar.test",
            summary="Mixed event",
            start="2026-07-04",
            end="2026-07-04T12:00:00",
        )

    error = _structured_error(exc_info.value)["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    client.call_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_rrule_forwards_explicit_empty_string_fields_to_rest_call():
    """Same "" vs None distinction on the non-rrule REST service path."""
    client = _make_mock_client()

    tools = CalendarTools(client)
    await tools.ha_config_set_calendar_event(
        entity_id="calendar.test",
        summary="One-off",
        start="2026-06-15T10:00:00",
        end="2026-06-15T11:00:00",
        description="",
        location="",
    )

    client.call_service.assert_awaited_once_with(
        "calendar",
        "create_event",
        {
            "entity_id": "calendar.test",
            "summary": "One-off",
            "start_date_time": "2026-06-15T10:00:00",
            "end_date_time": "2026-06-15T11:00:00",
            "description": "",
            "location": "",
        },
    )


def _structured_error(exc: ToolError) -> dict:
    """Parse the JSON structured-error payload carried by a ToolError."""
    return json.loads(str(exc))


# ---------------------------------------------------------------------------
# Error-path coverage for the rrule WebSocket branch. The pooled client
# collapses a WS command failure into ``{"success": False, ...}``; the tool
# re-raises so the failure reaches the same handler that used to catch the
# dedicated ``send_command`` exception.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rrule_failure_prepends_rrule_suggestion():
    """A failed rrule create surfaces the RRULE-syntax hint as the first suggestion."""
    client = _make_mock_client(
        ws_return={
            "success": False,
            "error": "Command failed: backend rejected rrule",
        }
    )

    tools = CalendarTools(client)
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_calendar_event(
            entity_id="calendar.test",
            summary="Weekly sync",
            start="2026-06-15T10:00:00",
            end="2026-06-15T10:30:00",
            rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=10",
        )

    suggestions = _structured_error(exc_info.value)["error"]["suggestions"]
    assert suggestions[0].startswith("Check RRULE syntax")


@pytest.mark.asyncio
async def test_non_rrule_failure_omits_rrule_suggestion():
    """The RRULE-syntax hint must not appear when no rrule was supplied."""
    client = _make_mock_client()
    client.call_service = AsyncMock(
        side_effect=HomeAssistantCommandError("Command failed: backend boom")
    )

    tools = CalendarTools(client)
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_calendar_event(
            entity_id="calendar.test",
            summary="One-off",
            start="2026-06-15T10:00:00",
            end="2026-06-15T11:00:00",
        )

    suggestions = _structured_error(exc_info.value)["error"]["suggestions"]
    assert all(not s.startswith("Check RRULE syntax") for s in suggestions)
    client.send_websocket_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_rrule_transport_failure_maps_to_connection_error():
    """A connection-shaped pooled-WS failure surfaces as CONNECTION_FAILED with
    connectivity guidance — not rrule/calendar suggestions (issue #1832 review)."""
    client = _make_mock_client(
        ws_return={
            "success": False,
            "error": "Failed to connect to Home Assistant WebSocket",
        }
    )

    tools = CalendarTools(client)
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_calendar_event(
            entity_id="calendar.test",
            summary="Weekly sync",
            start="2026-06-15T10:00:00",
            end="2026-06-15T10:30:00",
            rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=10",
        )

    err = _structured_error(exc_info.value)["error"]
    assert err["code"] in ("CONNECTION_FAILED", "CONNECTION_TIMEOUT")
    assert not any("RRULE" in s for s in err.get("suggestions", []))
