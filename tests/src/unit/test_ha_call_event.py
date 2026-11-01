"""Unit tests for ha_call_event tool."""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import event_call
from ha_mcp.tools.tools_service import ServiceTools


def _make_tools(
    fire_event_return: dict | Exception,
    listener_counts: dict[str, int] | Exception | None = None,
) -> ServiceTools:
    client = MagicMock()
    if isinstance(fire_event_return, Exception):
        client.fire_event = AsyncMock(side_effect=fire_event_return)
    else:
        client.fire_event = AsyncMock(return_value=fire_event_return)
    # The listener lookup reads GET /api/events through the client's own
    # request helper, so seeding rows here exercises the real mapping.
    if isinstance(listener_counts, Exception):
        client._request = AsyncMock(side_effect=listener_counts)
    else:
        client._request = AsyncMock(
            return_value=[
                {"event": name, "listener_count": count}
                for name, count in (listener_counts or {}).items()
            ]
        )
    tools = ServiceTools.__new__(ServiceTools)
    tools._client = client
    tools._device_tools = MagicMock()
    return tools


class TestHaCallEvent:
    async def test_fires_event_with_no_data(self):
        tools = _make_tools({"message": "Event my_event fired."})
        result = await tools.ha_call_event("my_event")
        assert result["success"] is True
        assert result["event_type"] == "my_event"
        assert "fired" in result["message"]
        tools._client.fire_event.assert_called_once_with("my_event", None)

    async def test_fires_event_with_dict_data(self):
        tools = _make_tools({"message": "Event custom_event fired."})
        result = await tools.ha_call_event("custom_event", {"key": "value"})
        assert result["success"] is True
        assert result["event_type"] == "custom_event"
        tools._client.fire_event.assert_called_once_with(
            "custom_event", {"key": "value"}
        )

    async def test_fires_event_with_json_string_data(self):
        tools = _make_tools({"message": "Event my_event fired."})
        result = await tools.ha_call_event("my_event", '{"temperature": 22}')
        assert result["success"] is True
        assert result["event_type"] == "my_event"
        tools._client.fire_event.assert_called_once_with(
            "my_event", {"temperature": 22}
        )

    async def test_raises_tool_error_on_list_data(self):
        tools = _make_tools({"message": "ok"})
        with pytest.raises(ToolError):
            await tools.ha_call_event("my_event", "[1, 2, 3]")

    async def test_raises_tool_error_on_invalid_json_string(self):
        """Invalid JSON raises ToolError via the ValueError → invalid_json path."""
        tools = _make_tools({"message": "ok"})
        with pytest.raises(ToolError):
            await tools.ha_call_event("my_event", "{not valid json")

    async def test_returns_fallback_message_when_response_empty(self):
        tools = _make_tools({})
        result = await tools.ha_call_event("my_event")
        assert result["success"] is True
        assert result["event_type"] == "my_event"
        assert "my_event" in result["message"]

    async def test_propagates_connection_error_as_tool_error(self):
        tools = _make_tools(ConnectionError("HA unreachable"))
        with pytest.raises(ToolError):
            await tools.ha_call_event("my_event")

    async def test_non_timeout_connection_error_uses_structured_error(self):
        """A connection failure that is not a timeout takes the structured path."""
        tools = _make_tools(HomeAssistantConnectionError("connection refused"))

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_call_event("my_event")

        payload = json.loads(str(exc_info.value))
        assert "Check Home Assistant connection" in payload["error"]["suggestions"]

    async def test_event_type_passed_to_client(self):
        tools = _make_tools({"message": "Event zone_entered fired."})
        await tools.ha_call_event("zone_entered", {"zone": "home"})
        call_args = tools._client.fire_event.call_args
        assert call_args[0][0] == "zone_entered"
        assert call_args[0][1] == {"zone": "home"}

    async def test_raises_tool_error_on_empty_event_type(self):
        """Empty event_type is rejected before hitting the wire."""
        tools = _make_tools({"message": "ok"})
        with pytest.raises(ToolError):
            await tools.ha_call_event("")

    async def test_raises_tool_error_on_whitespace_event_type(self):
        """Whitespace-only event_type is rejected before hitting the wire."""
        tools = _make_tools({"message": "ok"})
        with pytest.raises(ToolError):
            await tools.ha_call_event("   ")

    async def test_raises_tool_error_on_event_type_with_slash(self):
        """event_type containing a slash produces a malformed URL — rejected early."""
        tools = _make_tools({"message": "ok"})
        with pytest.raises(ToolError):
            await tools.ha_call_event("bad/event")

    async def test_reraises_tool_error_from_client(self):
        """ToolError from fire_event propagates unchanged (not swallowed by except Exception)."""
        tools = _make_tools(ToolError("already structured"))
        with pytest.raises(ToolError, match="already structured"):
            await tools.ha_call_event("my_event")

    async def test_timeout_returns_partial_success_with_warning(self):
        """Connection-timeout from fire_event surfaces as partial-success with a top-level warnings list.

        Pins the post-#1332 contract on the timeout branch (`tools_service.py:495+`):
        the migrated `warnings: list[str]` shape replaces the legacy singular
        `warning` string and must contain the dispatched-may-still-deliver note.
        """
        conn_error = HomeAssistantConnectionError("connection timed out")
        conn_error.__cause__ = httpx.ReadTimeout("Read timed out")
        tools = _make_tools(conn_error)

        result = await tools.ha_call_event("my_event")

        assert result["success"] is True
        assert result["partial"] is True
        assert result["event_type"] == "my_event"
        assert "warning" not in result, "legacy singular 'warning' key must not appear"
        warnings = result.get("warnings")
        assert isinstance(warnings, list) and warnings, (
            f"Expected non-empty warnings list, got: {result!r}"
        )
        assert any("timed out" in w.lower() for w in warnings), (
            f"Expected timeout-related warning content; got: {warnings!r}"
        )

    async def test_reports_exact_listener_count(self):
        tools = _make_tools({"message": "ok"}, {"my_event": 2})

        result = await tools.ha_call_event("my_event")

        assert result["listener_count"] == 2
        assert result["wildcard_listener_count"] == 0
        assert "warnings" not in result

    async def test_wildcard_listener_suppresses_the_no_listener_warning(self):
        tools = _make_tools({"message": "ok"}, {"*": 3})

        result = await tools.ha_call_event("my_event")

        assert result["listener_count"] == 0
        assert result["wildcard_listener_count"] == 3
        assert "warnings" not in result

    async def test_warns_when_no_listener_is_registered(self):
        tools = _make_tools({"message": "ok"}, {})

        result = await tools.ha_call_event("my_event")

        assert result["success"] is True
        assert result["listener_count"] == 0
        assert result["wildcard_listener_count"] == 0
        assert any("no event listeners" in w.lower() for w in result["warnings"])

    async def test_listener_lookup_failure_still_fires(self):
        tools = _make_tools({"message": "ok"}, HomeAssistantAPIError("events down"))

        result = await tools.ha_call_event("my_event")

        assert result["success"] is True
        assert "listener_count" not in result
        assert "wildcard_listener_count" not in result
        tools._client.fire_event.assert_awaited_once_with("my_event", None)

    @pytest.mark.parametrize("event_type", ["bad?event", "bad#event", "bad%3Fevent"])
    async def test_rejects_event_type_with_url_delimiters(self, event_type):
        """URL delimiters would truncate or decode the fired event type."""
        tools = _make_tools({"message": "ok"})

        with pytest.raises(ToolError):
            await tools.ha_call_event(event_type)

        tools._client.fire_event.assert_not_awaited()

    async def test_slow_listener_lookup_does_not_delay_the_fire(self, monkeypatch):
        """A stalled GET /api/events must not hold back the event dispatch."""
        tools = _make_tools({"message": "ok"})
        lookup_cancelled = False

        async def _stalled(*args: object, **kwargs: object) -> list[object]:
            nonlocal lookup_cancelled
            try:
                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                lookup_cancelled = True
                raise
            return []

        tools._client._request = AsyncMock(side_effect=_stalled)
        monkeypatch.setattr(event_call, "EVENT_LISTENER_LOOKUP_BUDGET_SECONDS", 0.05)

        result = await tools.ha_call_event("my_event")

        assert lookup_cancelled, "listener lookup ran past its budget"
        assert result["success"] is True
        assert "listener_count" not in result
        tools._client.fire_event.assert_awaited_once_with("my_event", None)

    async def test_listener_lookup_timeout_is_logged_and_fire_proceeds(self, caplog):
        """A timing-out lookup is optional enrichment, never a fire blocker."""
        tools = _make_tools({"message": "ok"}, TimeoutError("events timed out"))

        with caplog.at_level(logging.DEBUG, logger=event_call.__name__):
            result = await tools.ha_call_event("my_event")

        assert result["success"] is True
        assert "listener lookup failed" in caplog.text.lower()
        assert "listener_count" not in result
        tools._client.fire_event.assert_awaited_once_with("my_event", None)

    async def test_unreadable_row_for_the_fired_event_omits_counts(self):
        """A malformed row for this event must not read as 'no listeners'."""
        tools = _make_tools({"message": "ok"})
        tools._client._request = AsyncMock(
            return_value=[
                {"event": "other_event", "listener_count": 4},
                {"event": "my_event", "listener_count": "3"},
            ]
        )

        result = await tools.ha_call_event("my_event")

        assert result["success"] is True
        assert "listener_count" not in result
        assert "wildcard_listener_count" not in result
        assert "warnings" not in result
        tools._client.fire_event.assert_awaited_once_with("my_event", None)

    async def test_connect_timeout_does_not_claim_dispatch(self):
        """Connect timeouts happen before the POST, so the event was not sent."""
        conn_error = HomeAssistantConnectionError("connect timed out")
        conn_error.__cause__ = httpx.ConnectTimeout("Connect timed out")
        tools = _make_tools(conn_error)

        with pytest.raises(ToolError):
            await tools.ha_call_event("my_event")

    async def test_timeout_preserves_known_listener_counts(self):
        """A dispatch timeout must not discard counts read before the fire."""
        conn_error = HomeAssistantConnectionError("connection timed out")
        conn_error.__cause__ = httpx.ReadTimeout("Read timed out")
        tools = _make_tools(conn_error, {"my_event": 2, "*": 1})

        result = await tools.ha_call_event("my_event")

        assert result["partial"] is True
        assert result["listener_count"] == 2
        assert result["wildcard_listener_count"] == 1
        assert any("timed out" in w.lower() for w in result["warnings"])

    async def test_zero_listener_warning_does_not_claim_delivery(self):
        """A pre-dispatch count cannot establish whether the event was delivered."""
        tools = _make_tools({"message": "ok"}, {})

        result = await tools.ha_call_event("my_event")

        warning = " ".join(result["warnings"]).lower()
        assert "no event listeners" in warning
        assert "received" not in warning
