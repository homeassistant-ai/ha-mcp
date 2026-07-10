"""Unit tests for recurring-event support in ``ha_config_set_calendar_event``.

The ``calendar.create_event`` REST service schema has no ``rrule`` field —
recurrence is only accepted by the WebSocket command
``calendar/event/create``. These tests pin the transport routing: ``rrule``
present → WebSocket command carrying an RFC 5545 event payload
(``dtstart``/``dtend`` keys); ``rrule`` absent → the pre-existing REST
service call (``start_date_time``/``end_date_time`` keys), unchanged.

They also cover the three failure paths unique to the rrule branch: a
WebSocket connect failure (both the supplied-error and the synthesised
``CONNECTION_FAILED`` cases), the guarded disconnect that must not mask the
original ``send_command`` error, and the rrule-specific error suggestion.
``raise_tool_error`` serialises the structured error as JSON into the
``ToolError`` message, so the failure-path tests parse it back to assert on
code/context/suggestions.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.errors import ErrorCode
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
async def test_rrule_forwards_explicit_empty_string_fields_to_ws_event():
    """An explicitly-passed "" must reach HA (it clears the field there),
    unlike an unset (None) field, which is omitted entirely."""
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
            description="",
            location="",
            rrule="FREQ=MONTHLY;BYDAY=3SA",
        )

    _, kwargs = ws.send_command.await_args
    assert kwargs["event"]["description"] == ""
    assert kwargs["event"]["location"] == ""


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


@pytest.mark.asyncio
async def test_no_rrule_forwards_explicit_empty_string_fields_to_rest_call():
    """Same "" vs None distinction on the non-rrule REST service path."""
    client = _make_mock_client()

    with patch("ha_mcp.tools.tools_calendar.get_connected_ws_client"):
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
# Error-path coverage for the rrule WebSocket branch. These three branches are
# all new with the recurring-event feature and only reachable on the rrule
# path; the happy-path tests above never exercise them.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_connect_failure_surfaces_supplied_error_verbatim():
    """rrule path, get_connected_ws_client returns (None, error) → that exact
    error is raised and the REST service is never touched."""
    client = _make_mock_client()
    conn_error = {
        "success": False,
        "error": {
            "code": ErrorCode.CONNECTION_FAILED.value,
            "message": "ws factory said no",
        },
    }

    with patch(
        "ha_mcp.tools.tools_calendar.get_connected_ws_client",
        return_value=(None, conn_error),
    ):
        tools = CalendarTools(client)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_calendar_event(
                entity_id="calendar.test",
                summary="Weekly sync",
                start="2026-06-15T10:00:00",
                end="2026-06-15T10:30:00",
                rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=10",
            )

    assert _structured_error(exc_info.value) == conn_error
    client.call_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_ws_connect_failure_without_error_raises_connection_failed():
    """rrule path, get_connected_ws_client returns (None, None) → a synthesised
    CONNECTION_FAILED error carrying the entity_id, REST service untouched."""
    client = _make_mock_client()

    with patch(
        "ha_mcp.tools.tools_calendar.get_connected_ws_client",
        return_value=(None, None),
    ):
        tools = CalendarTools(client)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_calendar_event(
                entity_id="calendar.test",
                summary="Weekly sync",
                start="2026-06-15T10:00:00",
                end="2026-06-15T10:30:00",
                rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=10",
            )

    err = _structured_error(exc_info.value)
    assert err["error"]["code"] == ErrorCode.CONNECTION_FAILED.value
    # create_error_response merges context at the top level, not under "error".
    assert err["entity_id"] == "calendar.test"
    client.call_service.assert_not_awaited()


@pytest.mark.asyncio
async def test_disconnect_error_does_not_mask_send_command_error():
    """If send_command AND the guarded disconnect both raise, the caller must
    see the send_command error, not the teardown error."""
    client = _make_mock_client()
    ws = AsyncMock()
    ws.send_command = AsyncMock(
        side_effect=HomeAssistantCommandError("Command failed: backend rejected rrule")
    )
    ws.disconnect = AsyncMock(side_effect=RuntimeError("socket already torn down"))

    with patch(
        "ha_mcp.tools.tools_calendar.get_connected_ws_client",
        return_value=(ws, None),
    ):
        tools = CalendarTools(client)
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_calendar_event(
                entity_id="calendar.test",
                summary="Weekly sync",
                start="2026-06-15T10:00:00",
                end="2026-06-15T10:30:00",
                rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=10",
            )

    message = _structured_error(exc_info.value)["error"]["message"]
    assert "backend rejected rrule" in message
    assert "socket already torn down" not in message
    ws.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_disconnect_error_swallowed_on_success():
    """A disconnect failure after a successful send_command must not fail the tool."""
    client = _make_mock_client()
    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value={"success": True, "result": None})
    ws.disconnect = AsyncMock(side_effect=RuntimeError("socket already torn down"))

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
            rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=10",
        )

    assert result["success"] is True
    ws.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_rrule_failure_prepends_rrule_suggestion():
    """A failed rrule create surfaces the RRULE-syntax hint as the first suggestion."""
    client = _make_mock_client()
    ws = AsyncMock()
    ws.send_command = AsyncMock(
        side_effect=HomeAssistantCommandError("Command failed: backend rejected rrule")
    )

    with patch(
        "ha_mcp.tools.tools_calendar.get_connected_ws_client",
        return_value=(ws, None),
    ):
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

    with patch("ha_mcp.tools.tools_calendar.get_connected_ws_client") as ws_factory:
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
    ws_factory.assert_not_called()
