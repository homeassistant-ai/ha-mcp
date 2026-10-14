"""Unit tests for bounded Home Assistant event capture."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_events import EventTools


def _make_tools(queue: asyncio.Queue) -> tuple[EventTools, MagicMock, MagicMock]:
    client = MagicMock()
    client.base_url = "http://homeassistant.local:8123"
    client.token = "test-token"
    client.verify_ssl = None
    tools = EventTools(client)

    websocket = MagicMock()
    websocket.subscribe_command = AsyncMock(return_value=(17, queue))
    websocket.release_subscription = AsyncMock()
    return tools, websocket, client


class TestHaCaptureEvents:
    @pytest.mark.asyncio
    async def test_captures_filtered_events_and_releases_subscription(
        self, monkeypatch
    ):
        queue = asyncio.Queue()
        queue.put_nowait(
            {
                "event": {
                    "event_type": "zha_event",
                    "data": {"device_ieee": "unrelated"},
                }
            }
        )
        matching_event = {
            "event_type": "zha_event",
            "data": {"device_ieee": "00:11:22:33:44:55:66:77", "command": "on"},
            "time_fired": "2026-10-14T12:00:00+00:00",
        }
        queue.put_nowait({"event": matching_event})
        tools, websocket, client = _make_tools(queue)
        get_websocket = AsyncMock(return_value=websocket)
        monkeypatch.setattr(
            "ha_mcp.tools.tools_events.get_websocket_client", get_websocket
        )

        result = await tools.ha_capture_events(
            event_type="zha_event",
            duration_seconds=10,
            max_events=2,
            event_data_filter={"device_ieee": "00:11:22:33:44:55:66:77"},
        )

        assert result["success"] is True
        assert result["events_seen"] == 2
        assert result["events"] == [matching_event]
        assert result["stop_reason"] == "event_limit"
        get_websocket.assert_awaited_once_with(
            url=client.base_url,
            token=client.token,
            verify_ssl=client.verify_ssl,
        )
        websocket.subscribe_command.assert_awaited_once_with(
            "subscribe_events",
            event_type="zha_event",
            queue_maxsize=1,
        )
        websocket.release_subscription.assert_awaited_once_with(17)

    @pytest.mark.asyncio
    async def test_stops_after_maximum_observed_events(self, monkeypatch):
        queue = asyncio.Queue()
        queue.put_nowait({"event": {"event_type": "test_event", "data": {"n": 1}}})
        queue.put_nowait({"event": {"event_type": "test_event", "data": {"n": 2}}})
        tools, websocket, _client = _make_tools(queue)
        monkeypatch.setattr(
            "ha_mcp.tools.tools_events.get_websocket_client",
            AsyncMock(return_value=websocket),
        )

        result = await tools.ha_capture_events(
            event_type="test_event", duration_seconds=10, max_events=1
        )

        assert result["events_seen"] == 1
        assert result["events"] == [{"event_type": "test_event", "data": {"n": 1}}]
        assert result["stop_reason"] == "event_limit"
        websocket.release_subscription.assert_awaited_once_with(17)

    @pytest.mark.asyncio
    async def test_time_limit_returns_empty_capture_and_releases(self, monkeypatch):
        queue = asyncio.Queue()
        tools, websocket, _client = _make_tools(queue)
        monkeypatch.setattr(
            "ha_mcp.tools.tools_events.get_websocket_client",
            AsyncMock(return_value=websocket),
        )

        async def timeout_immediately(awaitable, timeout):
            awaitable.close()
            raise TimeoutError

        monkeypatch.setattr(
            "ha_mcp.tools.tools_events.asyncio.wait_for", timeout_immediately
        )

        result = await tools.ha_capture_events(
            event_type="test_event", duration_seconds=1, max_events=5
        )

        assert result["events"] == []
        assert result["stop_reason"] == "time_limit"
        websocket.release_subscription.assert_awaited_once_with(17)

    @pytest.mark.asyncio
    async def test_stops_before_captured_payload_exceeds_size_limit(self, monkeypatch):
        queue = asyncio.Queue()
        first_event = {"event_type": "test_event", "data": {"n": 1}}
        queue.put_nowait({"event": first_event})
        queue.put_nowait(
            {"event": {"event_type": "test_event", "data": {"payload": "x" * 100}}}
        )
        tools, websocket, _client = _make_tools(queue)
        monkeypatch.setattr(
            "ha_mcp.tools.tools_events.get_websocket_client",
            AsyncMock(return_value=websocket),
        )
        monkeypatch.setattr("ha_mcp.tools.tools_events.MAX_CAPTURED_EVENT_BYTES", 50)

        result = await tools.ha_capture_events(
            event_type="test_event", duration_seconds=10, max_events=5
        )

        assert result["events"] == [first_event]
        assert result["events_seen"] == 2
        assert result["events_omitted_due_to_size"] == 1
        assert result["stop_reason"] == "response_size_limit"
        websocket.release_subscription.assert_awaited_once_with(17)

    @pytest.mark.asyncio
    async def test_cancellation_releases_subscription(self, monkeypatch):
        queue = asyncio.Queue()
        tools, websocket, _client = _make_tools(queue)
        subscribed = asyncio.Event()

        async def subscribe(*args, **kwargs):
            subscribed.set()
            return 17, queue

        websocket.subscribe_command = AsyncMock(side_effect=subscribe)
        monkeypatch.setattr(
            "ha_mcp.tools.tools_events.get_websocket_client",
            AsyncMock(return_value=websocket),
        )

        task = asyncio.create_task(
            tools.ha_capture_events(
                event_type="test_event", duration_seconds=30, max_events=5
            )
        )
        await subscribed.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        websocket.release_subscription.assert_awaited_once_with(17)

    @pytest.mark.asyncio
    async def test_rejects_blank_event_type_without_connecting(self, monkeypatch):
        queue = asyncio.Queue()
        tools, _websocket, _client = _make_tools(queue)
        get_websocket = AsyncMock()
        monkeypatch.setattr(
            "ha_mcp.tools.tools_events.get_websocket_client", get_websocket
        )

        with pytest.raises(ToolError):
            await tools.ha_capture_events(
                event_type=" ", duration_seconds=10, max_events=5
            )

        get_websocket.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "event_type",
        ["ha_mcp_approval_response", "HA_MCP_APPROVAL_RESPONSE"],
    )
    async def test_rejects_approval_response_event_without_connecting(
        self, monkeypatch, event_type
    ):
        queue = asyncio.Queue()
        tools, _websocket, _client = _make_tools(queue)
        get_websocket = AsyncMock()
        monkeypatch.setattr(
            "ha_mcp.tools.tools_events.get_websocket_client", get_websocket
        )

        with pytest.raises(ToolError, match="sensitive approval PIN"):
            await tools.ha_capture_events(
                event_type=event_type, duration_seconds=10, max_events=5
            )

        get_websocket.assert_not_awaited()
