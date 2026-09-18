"""Unit tests for update mode in ``ha_config_set_calendar_event`` (#2482).

Home Assistant registers only ``create_event`` and ``get_events`` as REST
calendar services — updating an event lives exclusively on the WebSocket
command ``calendar/event/update``, routed here through the shared pooled client
(``client.send_websocket_message``) like the create-with-rrule and delete
paths. A failed WS command comes back as ``{"success": False, ...}`` and is
re-raised so the failure reaches the tool's error handler, which serialises the
structured error as JSON into the ``ToolError`` message.

The tests pin the transport split (``uid`` present → WS update; ``uid`` absent
→ unchanged create routing), the exact message shape, the validation guards
that fire before any WS round-trip, and the update-specific suggestions.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_calendar import (
    CalendarTools,
    _calendar_event_backup_id,
)


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


def _suggestions(exc: ToolError) -> list[str]:
    """Parse the JSON structured-error payload carried by a ToolError."""
    err = json.loads(str(exc))["error"]
    return err.get("suggestions") or [err.get("suggestion", "")]


@pytest.mark.asyncio
async def test_update_routes_via_pooled_websocket():
    """uid present → calendar/event/update pooled WS command, REST untouched."""
    client = _make_mock_client(ws_return={"success": True, "result": {"ok": True}})

    tools = CalendarTools(client)
    result = await tools.ha_config_set_calendar_event(
        entity_id="calendar.test",
        summary="Renamed meeting",
        start="2026-06-15T10:00:00",
        end="2026-06-15T11:00:00",
        uid="evt-123",
    )

    client.send_websocket_message.assert_awaited_once()
    message = client.send_websocket_message.await_args.args[0]
    assert message["type"] == "calendar/event/update"
    assert message["entity_id"] == "calendar.test"
    assert message["uid"] == "evt-123"
    assert message["event"] == {
        "summary": "Renamed meeting",
        "dtstart": "2026-06-15T10:00:00",
        "dtend": "2026-06-15T11:00:00",
    }
    # Unset optional fields stay out of the replacement event entirely.
    assert "description" not in message["event"]
    assert "location" not in message["event"]
    assert "rrule" not in message["event"]
    # Recurrence params omitted when unset.
    assert "recurrence_id" not in message
    assert "recurrence_range" not in message

    client.call_service.assert_not_awaited()
    assert result["success"] is True
    assert result["uid"] == "evt-123"
    assert "updated" in result["message"]


@pytest.mark.asyncio
async def test_update_forwards_recurrence_and_optional_event_fields():
    """Recurrence targeting rides the envelope; optional fields ride the event."""
    client = _make_mock_client()

    tools = CalendarTools(client)
    result = await tools.ha_config_set_calendar_event(
        entity_id="calendar.test",
        summary="Team sync",
        start="2026-06-15T10:00:00",
        end="2026-06-15T10:30:00",
        description="desc",
        location="loc",
        rrule="FREQ=WEEKLY;BYDAY=MO",
        uid="series-1",
        recurrence_id="20260615T100000",
        recurrence_range="THISANDFUTURE",
    )

    message = client.send_websocket_message.await_args.args[0]
    assert message["recurrence_id"] == "20260615T100000"
    assert message["recurrence_range"] == "THISANDFUTURE"
    assert message["event"] == {
        "summary": "Team sync",
        "dtstart": "2026-06-15T10:00:00",
        "dtend": "2026-06-15T10:30:00",
        "description": "desc",
        "location": "loc",
        "rrule": "FREQ=WEEKLY;BYDAY=MO",
    }
    assert result["recurrence_id"] == "20260615T100000"
    assert result["recurrence_range"] == "THISANDFUTURE"


@pytest.mark.asyncio
async def test_update_unsupported_calendar_surfaces_update_suggestion():
    """HA's not_supported message reaches the client on the WS path."""
    client = _make_mock_client(
        ws_return={
            "success": False,
            "error": "Calendar does not support event update",
        }
    )

    tools = CalendarTools(client)
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_calendar_event(
            entity_id="calendar.test",
            summary="Renamed meeting",
            start="2026-06-15T10:00:00",
            end="2026-06-15T11:00:00",
            uid="evt-123",
        )

    suggestions = _suggestions(exc_info.value)
    assert any("does not support event update" in s for s in suggestions)
    assert all("event creation" not in s for s in suggestions)


@pytest.mark.asyncio
async def test_update_not_found_suggestion_names_the_uid():
    client = _make_mock_client(
        ws_return={"success": False, "error": "Command failed: event not found"}
    )

    tools = CalendarTools(client)
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_calendar_event(
            entity_id="calendar.test",
            summary="Renamed meeting",
            start="2026-06-15T10:00:00",
            end="2026-06-15T11:00:00",
            uid="missing-uid",
        )

    suggestions = _suggestions(exc_info.value)
    assert any("missing-uid" in s and "not found" in s.lower() for s in suggestions)
    assert any("ha_config_get_calendar_events" in s and "UID" in s for s in suggestions)


@pytest.mark.asyncio
async def test_update_transport_failure_maps_to_connection_error():
    """A connection-shaped WS failure yields connectivity guidance only."""
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
            summary="Renamed meeting",
            start="2026-06-15T10:00:00",
            end="2026-06-15T11:00:00",
            uid="evt-123",
        )

    err = json.loads(str(exc_info.value))["error"]
    assert err["code"] in ("CONNECTION_FAILED", "CONNECTION_TIMEOUT")
    suggestions = err.get("suggestions") or [err.get("suggestion", "")]
    assert all("UID" not in s for s in suggestions)


@pytest.mark.parametrize(
    "recurrence_kwargs",
    [
        pytest.param({"recurrence_id": "20260615T100000"}, id="recurrence_id"),
        pytest.param({"recurrence_range": "THISANDFUTURE"}, id="recurrence_range"),
    ],
)
@pytest.mark.asyncio
async def test_recurrence_params_without_uid_are_rejected(recurrence_kwargs):
    """Recurrence targeting is meaningless when creating a new event."""
    client = _make_mock_client()

    tools = CalendarTools(client)
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_calendar_event(
            entity_id="calendar.test",
            summary="New event",
            start="2026-06-15T10:00:00",
            end="2026-06-15T11:00:00",
            **recurrence_kwargs,
        )

    assert "VALIDATION_INVALID_PARAMETER" in str(exc_info.value)
    client.send_websocket_message.assert_not_awaited()
    client.call_service.assert_not_awaited()


@pytest.mark.parametrize("bad", ["", "   "])
@pytest.mark.asyncio
async def test_empty_uid_is_rejected_before_any_round_trip(bad):
    """An empty uid would reach HA as a misleading "event not found"."""
    client = _make_mock_client()

    tools = CalendarTools(client)
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_calendar_event(
            entity_id="calendar.test",
            summary="Renamed meeting",
            start="2026-06-15T10:00:00",
            end="2026-06-15T11:00:00",
            uid=bad,
        )

    message = str(exc_info.value)
    assert "VALIDATION_INVALID_PARAMETER" in message
    assert '"parameter": "uid"' in message, message
    client.send_websocket_message.assert_not_awaited()
    client.call_service.assert_not_awaited()


@pytest.mark.parametrize("bad", ["", "   "])
@pytest.mark.asyncio
async def test_blank_recurrence_id_is_rejected(bad):
    """A blank recurrence_id is truthy to HA and forks against nothing."""
    client = _make_mock_client()

    tools = CalendarTools(client)
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_calendar_event(
            entity_id="calendar.test",
            summary="Renamed meeting",
            start="2026-06-15T10:00:00",
            end="2026-06-15T11:00:00",
            uid="series-1",
            recurrence_id=bad,
        )

    message = str(exc_info.value)
    assert "VALIDATION_INVALID_PARAMETER" in message
    assert '"parameter": "recurrence_id"' in message, message
    client.send_websocket_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_recurrence_range_without_recurrence_id_is_rejected():
    """The range starts AT the occurrence, so HA ignores it without the id.

    ical gates the entire fork on a truthy recurrence_id, so a range with no
    id silently edits the master event instead of this occurrence onwards.
    """
    client = _make_mock_client()

    tools = CalendarTools(client)
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_calendar_event(
            entity_id="calendar.test",
            summary="Renamed meeting",
            start="2026-06-15T10:00:00",
            end="2026-06-15T11:00:00",
            uid="series-1",
            recurrence_range="THISANDFUTURE",
        )

    assert "VALIDATION_INVALID_PARAMETER" in str(exc_info.value)
    assert "recurrence_id" in str(exc_info.value)
    client.send_websocket_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_rejects_mixed_date_and_datetime_values():
    """The date/datetime guard runs on the update path too."""
    client = _make_mock_client()

    tools = CalendarTools(client)
    with pytest.raises(ToolError) as exc_info:
        await tools.ha_config_set_calendar_event(
            entity_id="calendar.test",
            summary="Mixed event",
            start="2026-07-04",
            end="2026-07-04T12:00:00",
            uid="evt-123",
        )

    assert (
        json.loads(str(exc_info.value))["error"]["code"]
        == "VALIDATION_INVALID_PARAMETER"
    )
    client.send_websocket_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_uid_keeps_rest_create_path():
    """Regression: omitting uid must not divert the plain create path."""
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
    assert "created" in result["message"]
    assert "uid" not in result


@pytest.mark.asyncio
async def test_no_uid_with_rrule_keeps_websocket_create_path():
    """Regression: rrule without uid still creates a series, not an update."""
    client = _make_mock_client()

    tools = CalendarTools(client)
    await tools.ha_config_set_calendar_event(
        entity_id="calendar.test",
        summary="Weekly sync",
        start="2026-06-15T10:00:00",
        end="2026-06-15T10:30:00",
        rrule="FREQ=WEEKLY;BYDAY=MO",
    )

    message = client.send_websocket_message.await_args.args[0]
    assert message["type"] == "calendar/event/create"
    assert "uid" not in message
    client.call_service.assert_not_awaited()


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        pytest.param(
            {"entity_id": "calendar.fam", "uid": "evt-1"},
            "calendar.fam::evt-1",
            id="whole_event",
        ),
        pytest.param(
            {
                "entity_id": "calendar.fam",
                "uid": "evt-1",
                "recurrence_id": "20260615T090000",
            },
            "calendar.fam::evt-1::20260615T090000",
            id="one_occurrence",
        ),
        pytest.param({"entity_id": "calendar.fam"}, "", id="create_has_no_uid"),
        pytest.param({"uid": "evt-1"}, "", id="no_entity"),
    ],
)
def test_backup_key_identifies_the_targeted_occurrence(kwargs, expected):
    """The uid alone cannot name one occurrence of a series.

    Every expanded occurrence shares the uid, so the recurrence_id has to be
    part of the key for the capture to snapshot the right one.
    """
    assert _calendar_event_backup_id(kwargs) == expected
