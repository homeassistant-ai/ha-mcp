"""Unit tests for statistic_types parameter validation in ha_get_history (statistics source).

All tests use the public tool layer (HistoryTools.ha_get_history) following the pattern
established in test_history_pagination.py, which exercises param coercion, error
formatting, and source dispatch.
"""

import contextlib
import json
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_history import HistoryTools


def _make_mock_client() -> MagicMock:
    client = MagicMock()
    client.base_url = "http://localhost:8123"
    client.token = "test-token"
    return client


def _make_ws_dispatcher(stat_result: dict | None = None):
    """Async ``send_websocket_message`` stand-in for the pooled transport (#1813).

    The statistics tool now issues its single WS command through the shared
    pooled ``client.send_websocket_message`` rather than a per-call dedicated
    connection.
    """

    async def send_websocket_message(message):
        if message["type"] == "recorder/statistics_during_period":
            return {"success": True, "result": stat_result or {}}
        return {"success": False, "error": f"unknown command: {message['type']}"}

    return send_websocket_message


class TestStatisticTypesValidation:
    """Tests for statistic_types parameter edge cases via the public tool layer.

    Uses the same fixture pattern as TestStatisticsPagination in
    test_history_pagination.py: patch get_connected_ws_client, call
    HistoryTools(client).ha_get_history with source='statistics'.
    """

    @pytest.fixture
    def mock_client(self):
        return _make_mock_client()

    @pytest.fixture
    def history_tool(self, mock_client):
        self._mock_client = mock_client
        return HistoryTools(mock_client).ha_get_history

    def _patch_ws(self, rows: list[dict] | None = None):
        self._mock_client.send_websocket_message = _make_ws_dispatcher(
            stat_result={"sensor.test": rows or []}
        )
        return contextlib.nullcontext()

    @pytest.mark.asyncio
    async def test_empty_list_raises_tool_error(self, history_tool):
        """statistic_types=[] must raise ToolError with VALIDATION_INVALID_PARAMETER.

        Verified live: types=[] causes HA to return rows with only start/end keys,
        discarding all value fields (sum, mean, state, etc.). This is never useful.
        """
        with self._patch_ws(), pytest.raises(ToolError) as exc_info:
            await history_tool(
                entity_ids="sensor.test",
                source="statistics",
                start_time="7d",
                statistic_types=[],
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "statistic_types" in error["message"]

    @pytest.mark.asyncio
    async def test_none_does_not_set_types_in_command(self, history_tool):
        """statistic_types=None must not include 'types' in the WS command (HA returns all)."""
        sent_message = {}

        async def capturing_send(message):
            sent_message.update(message)
            return {"success": True, "result": {"sensor.test": []}}

        self._mock_client.send_websocket_message = capturing_send
        with patch(
            "ha_mcp.tools.tools_history.add_timezone_metadata",
            side_effect=lambda _c, d, **_kw: d,
        ):
            await history_tool(
                entity_ids="sensor.test",
                source="statistics",
                start_time="7d",
            )

        assert "types" not in sent_message, (
            "statistic_types=None must not send 'types' to HA — "
            "omitting 'types' lets HA return all available types."
        )

    @pytest.mark.asyncio
    async def test_valid_list_sets_types_in_command(self, history_tool):
        """statistic_types=['mean', 'sum'] must include types in the WS command."""
        sent_message = {}

        async def capturing_send(message):
            sent_message.update(message)
            return {"success": True, "result": {"sensor.test": []}}

        self._mock_client.send_websocket_message = capturing_send
        with patch(
            "ha_mcp.tools.tools_history.add_timezone_metadata",
            side_effect=lambda _c, d, **_kw: d,
        ):
            await history_tool(
                entity_ids="sensor.test",
                source="statistics",
                start_time="7d",
                statistic_types=["mean", "sum"],
            )

        assert "types" in sent_message
        assert set(sent_message["types"]) == {"mean", "sum"}

    @pytest.mark.asyncio
    async def test_comma_separated_string_sets_types_in_command(self, history_tool):
        """statistic_types='mean,sum' (comma-separated string) must send correct types."""
        sent_message = {}

        async def capturing_send(message):
            sent_message.update(message)
            return {"success": True, "result": {"sensor.test": []}}

        self._mock_client.send_websocket_message = capturing_send
        with patch(
            "ha_mcp.tools.tools_history.add_timezone_metadata",
            side_effect=lambda _c, d, **_kw: d,
        ):
            await history_tool(
                entity_ids="sensor.test",
                source="statistics",
                start_time="7d",
                statistic_types="mean,sum",
            )

        assert "types" in sent_message
        assert set(sent_message["types"]) == {"mean", "sum"}

    @pytest.mark.asyncio
    async def test_empty_string_list_notation_raises(self, history_tool):
        """statistic_types='[]' (string notation for empty list) must raise ToolError."""
        with self._patch_ws(), pytest.raises(ToolError) as exc_info:
            await history_tool(
                entity_ids="sensor.test",
                source="statistics",
                start_time="7d",
                statistic_types="[]",
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "statistic_types" in error["message"]

    @pytest.mark.asyncio
    async def test_invalid_type_raises(self, history_tool):
        """Invalid type name must raise ToolError with VALIDATION_INVALID_PARAMETER."""
        with self._patch_ws(), pytest.raises(ToolError) as exc_info:
            await history_tool(
                entity_ids="sensor.test",
                source="statistics",
                start_time="7d",
                statistic_types=["invalid_type"],
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "invalid_type" in error["message"]
