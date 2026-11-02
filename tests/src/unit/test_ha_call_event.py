"""Unit tests for ha_call_event tool."""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
    HomeAssistantCommandTimeout,
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
    client.send_websocket_message = AsyncMock(
        side_effect=HomeAssistantCommandNotSent("no WebSocket in tests")
    )
    tools = ServiceTools.__new__(ServiceTools)
    tools._client = client
    tools._device_tools = MagicMock()
    return tools


def _ws_success(payload: object) -> dict:
    return {"success": True, "result": payload}


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
        assert any("did not confirm" in w.lower() for w in warnings), (
            f"Expected ambiguous-dispatch warning content; got: {warnings!r}"
        )

    async def test_reports_exact_listener_count(self):
        tools = _make_tools({"message": "ok"}, {"my_event": 2})

        result = await tools.ha_call_event("my_event")

        assert result["listener_count"] == 2
        assert result["wildcard_listener_count"] == 0
        assert any(
            "WebSocket fire_event was unavailable" in w for w in result["warnings"]
        )

    async def test_wildcard_only_warns_with_match_all_context(self):
        """Recorder and other match-all listeners must not hide a zero count."""
        tools = _make_tools({"message": "ok"}, {"*": 3})

        result = await tools.ha_call_event("my_event")

        assert result["listener_count"] == 0
        assert result["wildcard_listener_count"] == 3
        warning = " ".join(result["warnings"]).lower()
        assert "no event listeners" in warning
        assert "match-all" in warning
        assert "recorder" in warning

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
        tools._client.fire_event.assert_awaited_once_with("my_event", None)

    async def test_connect_timeout_does_not_claim_dispatch(self):
        """Connect timeouts happen before the POST, so the event was not sent."""
        conn_error = HomeAssistantConnectionError("connect timed out")
        conn_error.__cause__ = httpx.ConnectTimeout("Connect timed out")
        tools = _make_tools(conn_error)

        with pytest.raises(ToolError):
            await tools.ha_call_event("my_event")

    async def test_component_route_returns_context_and_counts(self, monkeypatch):
        """A capable component fulfils the fire, counts, and context in one call."""
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            return_value=_ws_success(
                {
                    "event_type": "my_event",
                    "context": {"id": "component-context"},
                    "listener_count": 2,
                    "wildcard_listener_count": 1,
                }
            )
        )
        monkeypatch.setattr(
            event_call,
            "get_component_caps",
            AsyncMock(return_value=SimpleNamespace(capabilities=["call_event"])),
        )

        result = await tools.ha_call_event("my_event")

        assert result["context_id"] == "component-context"
        assert result["listener_count"] == 2
        assert result["wildcard_listener_count"] == 1
        tools._client.fire_event.assert_not_awaited()

    @pytest.mark.parametrize(
        ("exact", "wildcard"),
        [(None, None), (True, 0), (-1, 0), ("3", 0), (0, True), (0, -1), (0, "2")],
    )
    async def test_component_invalid_counts_are_omitted(
        self, monkeypatch, exact, wildcard
    ):
        """A malformed component count must read as unknown, never as zero."""
        payload: dict = {"context": {"id": "ctx"}}
        if exact is not None:
            payload["listener_count"] = exact
            payload["wildcard_listener_count"] = wildcard
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            return_value=_ws_success(payload)
        )
        monkeypatch.setattr(
            event_call,
            "get_component_caps",
            AsyncMock(return_value=SimpleNamespace(capabilities=["call_event"])),
        )

        result = await tools.ha_call_event("my_event")

        assert result["context_id"] == "ctx"
        assert "listener_count" not in result
        assert "wildcard_listener_count" not in result
        assert "warnings" not in result

    async def test_component_generic_error_is_structured(self, monkeypatch):
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            side_effect=RuntimeError("component bug")
        )
        monkeypatch.setattr(
            event_call,
            "get_component_caps",
            AsyncMock(return_value=SimpleNamespace(capabilities=["call_event"])),
        )

        with pytest.raises(ToolError):
            await tools.ha_call_event("my_event")
        assert tools._client.send_websocket_message.await_count == 1

    @pytest.mark.parametrize("component_capable", [True, False])
    async def test_unauthorized_token_maps_to_admin_required(
        self, monkeypatch, component_capable
    ):
        """A non-admin token gets the admin-required error, not a generic failure."""
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            side_effect=HomeAssistantCommandError(
                "Command failed: Unauthorized", "unauthorized"
            )
        )
        caps = (
            SimpleNamespace(capabilities=["call_event"]) if component_capable else None
        )
        monkeypatch.setattr(
            event_call, "get_component_caps", AsyncMock(return_value=caps)
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_call_event("my_event")

        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "AUTH_INSUFFICIENT_PERMISSIONS"
        assert any(
            "administrator" in suggestion.lower()
            for suggestion in payload["error"]["suggestions"]
        )

    async def test_event_rejection_uses_event_specific_suggestions(self, monkeypatch):
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": "Event type has a maximum length of 64 characters",
            }
        )
        monkeypatch.setattr(
            event_call, "get_component_caps", AsyncMock(return_value=None)
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_call_event("my_event")

        payload = json.loads(str(exc_info.value))
        assert any(
            "64 characters" in suggestion
            for suggestion in payload["error"]["suggestions"]
        )

    async def test_component_probe_token_rejection_uses_a_token_error(
        self, monkeypatch
    ):
        """A rejected token must not read as a permissions problem."""
        from ha_mcp.tools import component_api

        tools = _make_tools({"message": "rest"})
        ws = AsyncMock()
        ws.send_command = AsyncMock(side_effect=HomeAssistantAuthError("invalid auth"))
        monkeypatch.setattr(
            component_api, "get_websocket_client", AsyncMock(return_value=ws)
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_call_event("my_event")

        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] != "AUTH_INSUFFICIENT_PERMISSIONS"
        assert any(
            "Check the Home Assistant token" in suggestion
            for suggestion in payload["error"]["suggestions"]
        )
        assert component_api.component_probe_auth_failed(tools._client)
        ws.send_command.assert_awaited_once_with("ha_mcp_tools/info")
        tools._client._request.assert_not_awaited()
        tools._client.send_websocket_message.assert_not_awaited()

    @pytest.mark.parametrize("component_capable", [True, False])
    async def test_unauthorized_envelope_maps_to_admin_required(
        self, monkeypatch, component_capable
    ):
        """Core can return the refusal as an envelope, not a raised error."""
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": "Command failed: Unauthorized",
                "error_code": "unauthorized",
            }
        )
        caps = (
            SimpleNamespace(capabilities=["call_event"]) if component_capable else None
        )
        monkeypatch.setattr(
            event_call, "get_component_caps", AsyncMock(return_value=caps)
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_call_event("my_event")

        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "AUTH_INSUFFICIENT_PERMISSIONS"
        tools._client.fire_event.assert_not_awaited()

    async def test_component_probe_auth_failure_stops_before_the_fire(
        self, monkeypatch
    ):
        """A rejected probe must not be followed by a listener read or fire."""
        from ha_mcp.tools import component_api

        tools = _make_tools({"message": "rest"})
        ws = AsyncMock()
        ws.send_command = AsyncMock(
            side_effect=HomeAssistantCommandError(
                "Command failed: Unauthorized", "unauthorized"
            )
        )
        monkeypatch.setattr(
            component_api, "get_websocket_client", AsyncMock(return_value=ws)
        )

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_call_event("my_event")

        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "AUTH_INSUFFICIENT_PERMISSIONS"
        ws.send_command.assert_awaited_once_with("ha_mcp_tools/info")
        tools._client._request.assert_not_awaited()
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_ws_not_sent_falls_back_with_context_warning(self):
        """The REST fallback says why context_id is missing."""
        tools = _make_tools({"message": "rest"})

        result = await tools.ha_call_event("my_event")

        assert result["context_id"] is None
        assert any(
            "WebSocket fire_event was unavailable" in warning
            for warning in result["warnings"]
        )

    async def test_ws_fallback_refusal_names_the_websocket_failure(self):
        """The delimiter refusal must name the fallback, not the event type."""
        tools = _make_tools({"message": "rest"})

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_call_event("bad/event")

        message = str(exc_info.value)
        assert "REST fallback" in message
        assert "WebSocket fire_event command was not sent" in message
        assert "cannot contain path separators" in message

    async def test_component_post_send_loss_is_partial_and_not_retried(
        self, monkeypatch
    ):
        """A connection loss after the component send may have fired the event."""
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            side_effect=HomeAssistantConnectionError("component down")
        )
        monkeypatch.setattr(
            event_call,
            "get_component_caps",
            AsyncMock(return_value=SimpleNamespace(capabilities=["call_event"])),
        )

        result = await tools.ha_call_event("my_event")

        assert result["success"] is True
        assert result["partial"] is True
        assert any("may have been dispatched" in w for w in result["warnings"])
        assert tools._client.send_websocket_message.await_count == 1
        tools._client.fire_event.assert_not_awaited()

    async def test_component_not_sent_falls_back_to_ws(self, monkeypatch):
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            side_effect=[
                HomeAssistantCommandNotSent("no component socket"),
                _ws_success({"context": {"id": "ws-context"}}),
            ]
        )
        monkeypatch.setattr(
            event_call,
            "get_component_caps",
            AsyncMock(return_value=SimpleNamespace(capabilities=["call_event"])),
        )

        result = await tools.ha_call_event("my_event")

        assert result["context_id"] == "ws-context"
        assert [
            call.args[0]["type"]
            for call in tools._client.send_websocket_message.await_args_list
        ] == [event_call.WS_CALL_EVENT, event_call.CORE_FIRE_EVENT]

    async def test_component_invalid_result_is_an_error(self, monkeypatch):
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            return_value=_ws_success("not a dict")
        )
        monkeypatch.setattr(
            event_call,
            "get_component_caps",
            AsyncMock(return_value=SimpleNamespace(capabilities=["call_event"])),
        )

        with pytest.raises(ToolError):
            await tools.ha_call_event("my_event")
        assert tools._client.send_websocket_message.await_count == 1

    async def test_component_non_dict_reply_is_an_error(self, monkeypatch):
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            return_value=["not", "a", "dict"]
        )
        monkeypatch.setattr(
            event_call,
            "get_component_caps",
            AsyncMock(return_value=SimpleNamespace(capabilities=["call_event"])),
        )

        with pytest.raises(ToolError):
            await tools.ha_call_event("my_event")
        assert tools._client.send_websocket_message.await_count == 1

    async def test_component_unknown_command_falls_back_to_ws(self, monkeypatch):
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            side_effect=[
                {"success": False, "error_code": "unknown_command"},
                _ws_success({"context": {"id": "ws-context"}}),
            ]
        )
        monkeypatch.setattr(
            event_call,
            "get_component_caps",
            AsyncMock(return_value=SimpleNamespace(capabilities=["call_event"])),
        )

        result = await tools.ha_call_event("my_event")

        assert result["context_id"] == "ws-context"

    async def test_component_rejection_is_an_error(self, monkeypatch):
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": "bad event"}
        )
        monkeypatch.setattr(
            event_call,
            "get_component_caps",
            AsyncMock(return_value=SimpleNamespace(capabilities=["call_event"])),
        )

        with pytest.raises(ToolError):
            await tools.ha_call_event("my_event")
        assert tools._client.send_websocket_message.await_count == 1

    async def test_component_non_dict_context_is_omitted(self, monkeypatch):
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            return_value=_ws_success({"context": "not-a-dict", "listener_count": 0})
        )
        monkeypatch.setattr(
            event_call,
            "get_component_caps",
            AsyncMock(return_value=SimpleNamespace(capabilities=["call_event"])),
        )

        result = await tools.ha_call_event("my_event")

        assert result["context"] == {}
        assert result["context_id"] is None

    async def test_ws_route_returns_context_when_component_is_absent(self, monkeypatch):
        """Without the component, Core's fire_event still returns the context."""
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            return_value=_ws_success({"context": {"id": "ws-context"}})
        )
        monkeypatch.setattr(
            event_call, "get_component_caps", AsyncMock(return_value=None)
        )

        result = await tools.ha_call_event("my_event")

        assert result["context_id"] == "ws-context"
        assert result["listener_count"] == 0
        tools._client.fire_event.assert_not_awaited()

    async def test_ws_route_accepts_url_delimiters(self, monkeypatch):
        """The WS route carries the event type in JSON, not a URL path."""
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            return_value=_ws_success({"context": {"id": "ws-context"}})
        )
        monkeypatch.setattr(
            event_call, "get_component_caps", AsyncMock(return_value=None)
        )

        result = await tools.ha_call_event("mcp_probe?key=go")

        assert result["success"] is True
        assert result["context_id"] == "ws-context"
        sent = tools._client.send_websocket_message.await_args.args[0]
        assert sent["event_type"] == "mcp_probe?key=go"

    async def test_ws_post_send_loss_is_partial_and_not_retried(self, monkeypatch):
        tools = _make_tools({"message": "rest"}, {"my_event": 2})
        timeout = HomeAssistantConnectionError("ws down")
        timeout.__cause__ = HomeAssistantCommandTimeout("Command timeout")
        tools._client.send_websocket_message = AsyncMock(side_effect=timeout)
        monkeypatch.setattr(
            event_call, "get_component_caps", AsyncMock(return_value=None)
        )

        result = await tools.ha_call_event("my_event")

        assert result["success"] is True
        assert result["partial"] is True
        assert result["listener_count"] == 2
        assert any("may have been dispatched" in w for w in result["warnings"])
        assert tools._client.send_websocket_message.await_count == 1
        tools._client.fire_event.assert_not_awaited()

    async def test_ws_invalid_result_is_an_error(self, monkeypatch):
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            return_value=_ws_success("nope")
        )
        monkeypatch.setattr(
            event_call, "get_component_caps", AsyncMock(return_value=None)
        )

        with pytest.raises(ToolError):
            await tools.ha_call_event("my_event")

    async def test_ws_non_dict_reply_is_an_error(self, monkeypatch):
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(return_value="nope")
        monkeypatch.setattr(
            event_call, "get_component_caps", AsyncMock(return_value=None)
        )

        with pytest.raises(ToolError):
            await tools.ha_call_event("my_event")

    async def test_ws_unknown_command_is_an_error_not_a_fallback(self, monkeypatch):
        """Core registers fire_event on every supported version."""
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error_code": "unknown_command"}
        )
        monkeypatch.setattr(
            event_call, "get_component_caps", AsyncMock(return_value=None)
        )
        invalidated = MagicMock()
        monkeypatch.setattr(event_call, "invalidate_caps", invalidated)

        with pytest.raises(ToolError):
            await tools.ha_call_event("my_event")

        tools._client.fire_event.assert_not_awaited()
        invalidated.assert_not_called()

    async def test_ws_rejection_is_an_error(self, monkeypatch):
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": "bad event"}
        )
        monkeypatch.setattr(
            event_call, "get_component_caps", AsyncMock(return_value=None)
        )

        with pytest.raises(ToolError):
            await tools.ha_call_event("my_event")

    async def test_ws_non_dict_context_is_omitted(self, monkeypatch):
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            return_value=_ws_success({"context": "bad"})
        )
        monkeypatch.setattr(
            event_call, "get_component_caps", AsyncMock(return_value=None)
        )

        result = await tools.ha_call_event("my_event")

        assert result["context"] == {}
        assert result["context_id"] is None

    async def test_state_changed_stays_on_the_rest_view(self, monkeypatch):
        """state_changed needs APIEventView's State rebuilding, not WS."""
        tools = _make_tools({"message": "Event state_changed fired."})
        monkeypatch.setattr(
            event_call,
            "get_component_caps",
            AsyncMock(return_value=SimpleNamespace(capabilities=["call_event"])),
        )

        result = await tools.ha_call_event("state_changed", {"entity_id": "light.x"})

        assert result["success"] is True
        tools._client.fire_event.assert_awaited_once_with(
            "state_changed", {"entity_id": "light.x"}
        )
        tools._client.send_websocket_message.assert_not_awaited()

    async def test_timeout_preserves_known_listener_counts(self):
        """A dispatch timeout must not discard counts read before the fire."""
        conn_error = HomeAssistantConnectionError("connection timed out")
        conn_error.__cause__ = httpx.ReadTimeout("Read timed out")
        tools = _make_tools(conn_error, {"my_event": 2, "*": 1})

        result = await tools.ha_call_event("my_event")

        assert result["partial"] is True
        assert result["listener_count"] == 2
        assert result["wildcard_listener_count"] == 1
        assert result["context"] == {}
        assert result["context_id"] is None
        assert any("did not confirm" in w.lower() for w in result["warnings"])

    async def test_zero_listener_warning_does_not_claim_delivery(self):
        """A pre-dispatch count cannot establish whether the event was delivered."""
        tools = _make_tools({"message": "ok"}, {})

        result = await tools.ha_call_event("my_event")

        warning = " ".join(result["warnings"]).lower()
        assert "no event listeners" in warning
        assert "received" not in warning

    @pytest.mark.parametrize("component_capable", [True, False])
    async def test_absent_payload_is_omitted_from_the_fire_message(
        self, monkeypatch, component_capable
    ):
        """Core types event_data as a dict, so an explicit null is refused."""
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            return_value=_ws_success({"context": {"id": "ctx"}})
        )
        caps = (
            SimpleNamespace(capabilities=["call_event"]) if component_capable else None
        )
        monkeypatch.setattr(
            event_call, "get_component_caps", AsyncMock(return_value=caps)
        )

        await tools.ha_call_event("my_event")

        message = tools._client.send_websocket_message.await_args.args[0]
        assert "event_data" not in message

    async def test_payload_is_sent_when_given(self, monkeypatch):
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            return_value=_ws_success({"context": {"id": "ctx"}})
        )
        monkeypatch.setattr(
            event_call,
            "get_component_caps",
            AsyncMock(return_value=SimpleNamespace(capabilities=["call_event"])),
        )

        await tools.ha_call_event("my_event", {"key": "value"})

        message = tools._client.send_websocket_message.await_args.args[0]
        assert message["event_data"] == {"key": "value"}

    @pytest.mark.parametrize("component_capable", [True, False])
    async def test_rejected_token_is_not_retried_over_rest(
        self, monkeypatch, component_capable
    ):
        """Re-sending a rejected token adds a 401 toward Home Assistant's ban."""
        tools = _make_tools({"message": "rest"})
        not_sent = HomeAssistantCommandNotSent("no answer")
        not_sent.__cause__ = HomeAssistantAuthError("invalid auth")
        tools._client.send_websocket_message = AsyncMock(side_effect=not_sent)
        caps = (
            SimpleNamespace(capabilities=["call_event"]) if component_capable else None
        )
        monkeypatch.setattr(
            event_call, "get_component_caps", AsyncMock(return_value=caps)
        )

        with pytest.raises(ToolError):
            await tools.ha_call_event("my_event")

        assert tools._client.send_websocket_message.await_count == 1
        tools._client.fire_event.assert_not_awaited()

    async def test_command_not_sent_falls_back_when_not_an_auth_failure(
        self, monkeypatch
    ):
        """A command refused before dispatch still uses the REST fallback."""
        tools = _make_tools({"message": "rest"})
        tools._client.send_websocket_message = AsyncMock(
            side_effect=HomeAssistantCommandNotSent("not connected")
        )
        monkeypatch.setattr(
            event_call,
            "get_component_caps",
            AsyncMock(return_value=SimpleNamespace(capabilities=["call_event"])),
        )

        result = await tools.ha_call_event("my_event")

        assert result["success"] is True
        tools._client.fire_event.assert_awaited_once_with("my_event", None)

    async def test_ws_fallback_reuses_the_prefetched_listener_fields(self):
        """The fallback must not repeat the lookup's up-to-5s wait."""
        tools = _make_tools({"message": "rest"}, {"my_event": 2, "*": 1})

        result = await tools.ha_call_event("my_event")

        assert result["listener_count"] == 2
        assert result["wildcard_listener_count"] == 1
        tools._client._request.assert_awaited_once()
        tools._client.fire_event.assert_awaited_once_with("my_event", None)

    async def test_rest_returns_context_fallbacks(self):
        """REST reports no context, but every route keeps the same keys."""
        tools = _make_tools({"message": "ok"})

        result = await tools.ha_call_event("my_event")

        assert result["context"] == {}
        assert result["context_id"] is None
