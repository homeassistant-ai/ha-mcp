"""Native logbook lookup tests for trace debugging."""

from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantCommandTimeout
from ha_mcp.tools.trace_logbook import LOGBOOK_ROW_LIMIT, fetch_trace_logbook

from .test_tools_traces import _make_pooled_client


def _trace() -> dict:
    return {
        "run_id": "r1",
        "timestamp": {"start": "2026-11-08T16:00:00+00:00"},
        "context": {"id": "ctx-1"},
    }


def _component(entries):
    client = _make_pooled_client(lambda mtype, message: {})
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": entries}
    )
    client.get_logbook = AsyncMock()
    return client


class TestFetchTraceLogbook:
    @pytest.mark.asyncio
    async def test_component_uses_start_and_context_without_end_time(self) -> None:
        client = _component([{"name": "Light"}])

        with (
            patch(
                "ha_mcp.tools.log_sources_logbook.get_component_caps",
                AsyncMock(return_value={"logbook_events": True}),
            ),
            patch(
                "ha_mcp.tools.log_sources_logbook.component_supports",
                return_value=True,
            ),
        ):
            logbook = await fetch_trace_logbook(client, _trace())

        assert logbook.transport == "component"
        assert logbook.entries == [{"name": "Light"}]
        assert logbook.warnings == []
        client.send_websocket_message.assert_awaited_once_with(
            {
                "type": "ha_mcp_tools/logbook_events",
                "start_time": "2026-11-08T16:00:00+00:00",
                "context_id": "ctx-1",
            }
        )
        client.get_logbook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_component_absent_uses_rest(self, freezer) -> None:
        freezer.move_to("2026-11-08T16:05:00+00:00")
        client = _make_pooled_client(lambda mtype, message: {})
        client.get_logbook = AsyncMock(return_value=[{"name": "REST"}])

        with (
            patch(
                "ha_mcp.tools.log_sources_logbook.get_component_caps",
                AsyncMock(return_value={}),
            ),
            patch(
                "ha_mcp.tools.log_sources_logbook.component_supports",
                return_value=False,
            ),
        ):
            logbook = await fetch_trace_logbook(client, _trace())

        assert logbook.transport == "rest"
        assert logbook.entries == [{"name": "REST"}]
        client.get_logbook.assert_awaited_once_with(
            start_time="2026-11-08T16:00:00+00:00",
            end_time="2026-11-08T16:05:00+00:00",
            context_id="ctx-1",
        )

    @pytest.mark.asyncio
    async def test_component_timeout_is_not_replayed_over_rest(self) -> None:
        client = _component([])
        client.send_websocket_message = AsyncMock(
            side_effect=HomeAssistantCommandTimeout("timed out")
        )
        client.get_logbook = AsyncMock()

        with (
            patch(
                "ha_mcp.tools.log_sources_logbook.get_component_caps",
                AsyncMock(return_value={"logbook_events": True}),
            ),
            patch(
                "ha_mcp.tools.log_sources_logbook.component_supports",
                return_value=True,
            ),
            pytest.raises(ToolError),
        ):
            await fetch_trace_logbook(client, _trace())

        client.get_logbook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_component_rejection_surfaces(self) -> None:
        client = _component([])
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error_code": "invalid_format",
                "error": "bad context",
            }
        )

        with (
            patch(
                "ha_mcp.tools.log_sources_logbook.get_component_caps",
                AsyncMock(return_value={"logbook_events": True}),
            ),
            patch(
                "ha_mcp.tools.log_sources_logbook.component_supports",
                return_value=True,
            ),
            pytest.raises(ToolError),
        ):
            await fetch_trace_logbook(client, _trace())

    @pytest.mark.asyncio
    async def test_rest_non_list_reply_is_a_warning(self) -> None:
        client = _make_pooled_client(lambda mtype, message: {})
        client.get_logbook = AsyncMock(return_value=None)

        with (
            patch(
                "ha_mcp.tools.log_sources_logbook.get_component_caps",
                AsyncMock(return_value={}),
            ),
            patch(
                "ha_mcp.tools.log_sources_logbook.component_supports",
                return_value=False,
            ),
        ):
            logbook = await fetch_trace_logbook(client, _trace())

        assert logbook.entries == []
        assert logbook.transport == "rest"
        assert logbook.warnings == ["The logbook reply was not a row list."]

    @pytest.mark.asyncio
    async def test_rows_are_capped(self) -> None:
        rows = [{"i": i} for i in range(LOGBOOK_ROW_LIMIT + 5)]
        rows.insert(1, {"i": 0})
        client = _component(rows)

        with (
            patch(
                "ha_mcp.tools.log_sources_logbook.get_component_caps",
                AsyncMock(return_value={"logbook_events": True}),
            ),
            patch(
                "ha_mcp.tools.log_sources_logbook.component_supports",
                return_value=True,
            ),
        ):
            logbook = await fetch_trace_logbook(client, _trace())

        assert len(logbook.entries) == LOGBOOK_ROW_LIMIT
        assert logbook.entries[0] == {"i": 0}
        assert logbook.entries[1] == {"i": 0}
        assert logbook.total == len(rows)
        assert logbook.truncated is True

    @pytest.mark.asyncio
    async def test_missing_context_does_not_read(self) -> None:
        client = _make_pooled_client(lambda mtype, message: {})
        client.get_logbook = AsyncMock()

        logbook = await fetch_trace_logbook(
            client, {"timestamp": {"start": "2026-11-08T16:00:00+00:00"}}
        )

        assert logbook.entries == []
        client.get_logbook.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_start_does_not_read(self) -> None:
        client = _make_pooled_client(lambda mtype, message: {})
        client.get_logbook = AsyncMock()

        logbook = await fetch_trace_logbook(client, {"context": {"id": "ctx-1"}})

        assert logbook.entries == []
        client.get_logbook.assert_not_awaited()
