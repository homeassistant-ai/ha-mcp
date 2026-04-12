"""Unit tests for ha_get_history offset/limit pagination (issue #930).

Tests that history and statistics sources correctly support offset-based
pagination with standardized metadata: total_count, offset, limit, count,
has_more, next_offset.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_history import HistoryTools

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PAGINATION_FIELDS = {"total_count", "offset", "limit", "count", "has_more", "next_offset"}


def _make_mock_client() -> MagicMock:
    client = MagicMock()
    client.base_url = "http://homeassistant.local"
    client.token = "test_token"
    return client


def _make_history_states(n: int) -> list[dict]:
    """Generate n minimal state-change dicts (short-form HA format)."""
    return [{"s": str(i), "lu": 1700000000.0 + i, "lc": 1700000000.0 + i} for i in range(n)]


def _make_stat_rows(n: int) -> list[dict]:
    """Generate n minimal statistics rows."""
    return [{"start": 1700000000 + i * 300, "mean": float(i)} for i in range(n)]


def _make_ws_client_mock(history_result: dict | None = None, stat_result: dict | None = None) -> MagicMock:
    ws = MagicMock()
    ws.disconnect = AsyncMock()

    async def send_command(cmd, **kwargs):
        if cmd == "history/history_during_period":
            return {"success": True, "result": history_result or {}}
        if cmd == "recorder/statistics_during_period":
            return {"success": True, "result": stat_result or {}}
        return {"success": False, "error": f"unknown command: {cmd}"}

    ws.send_command = send_command
    return ws


# ---------------------------------------------------------------------------
# Tests: history source
# ---------------------------------------------------------------------------


class TestHistoryPagination:
    """Pagination for source='history'."""

    @pytest.fixture
    def mock_client(self):
        return _make_mock_client()

    @pytest.fixture
    def history_tool(self, mock_client):
        return HistoryTools(mock_client).ha_get_history

    def _patch_ws(self, states: list[dict]):
        ws = _make_ws_client_mock(history_result={"sensor.test": states})
        return patch(
            "ha_mcp.tools.tools_history.get_connected_ws_client",
            return_value=(ws, None),
        )

    @pytest.mark.asyncio
    async def test_default_offset_returns_first_page(self, history_tool):
        """offset=0 (default) returns the first limit entries."""
        states = _make_history_states(20)
        with self._patch_ws(states), patch("ha_mcp.tools.tools_history.add_timezone_metadata", side_effect=lambda _c, d: d):
            result = await history_tool(entity_ids="sensor.test", limit=5)

        entity = result["entities"][0]
        assert len(entity["states"]) == 5
        assert entity["total_count"] == 20
        assert entity["offset"] == 0
        assert entity["has_more"] is True
        assert entity["next_offset"] == 5

    @pytest.mark.asyncio
    async def test_offset_skips_entries(self, history_tool):
        """offset=5 skips the first 5 entries."""
        states = _make_history_states(20)
        with self._patch_ws(states), patch("ha_mcp.tools.tools_history.add_timezone_metadata", side_effect=lambda _c, d: d):
            result = await history_tool(entity_ids="sensor.test", limit=5, offset=5)

        entity = result["entities"][0]
        assert len(entity["states"]) == 5
        assert entity["offset"] == 5
        assert entity["states"][0]["state"] == "5"

    @pytest.mark.asyncio
    async def test_offset_beyond_total_returns_empty(self, history_tool):
        """offset beyond total_count returns empty states, has_more=False."""
        states = _make_history_states(10)
        with self._patch_ws(states), patch("ha_mcp.tools.tools_history.add_timezone_metadata", side_effect=lambda _c, d: d):
            result = await history_tool(entity_ids="sensor.test", limit=5, offset=100)

        entity = result["entities"][0]
        assert entity["states"] == []
        assert entity["has_more"] is False
        assert entity["next_offset"] is None
        assert entity["total_count"] == 10

    @pytest.mark.asyncio
    async def test_last_page_has_more_false(self, history_tool):
        """Final page returns has_more=False and next_offset=None."""
        states = _make_history_states(7)
        with self._patch_ws(states), patch("ha_mcp.tools.tools_history.add_timezone_metadata", side_effect=lambda _c, d: d):
            result = await history_tool(entity_ids="sensor.test", limit=5, offset=5)

        entity = result["entities"][0]
        assert len(entity["states"]) == 2
        assert entity["has_more"] is False
        assert entity["next_offset"] is None

    @pytest.mark.asyncio
    async def test_pagination_fields_present(self, history_tool):
        """All standardized pagination fields are present in each entity."""
        states = _make_history_states(3)
        with self._patch_ws(states), patch("ha_mcp.tools.tools_history.add_timezone_metadata", side_effect=lambda _c, d: d):
            result = await history_tool(entity_ids="sensor.test", limit=2)

        entity = result["entities"][0]
        assert PAGINATION_FIELDS.issubset(entity.keys())

    @pytest.mark.asyncio
    async def test_negative_offset_raises_tool_error(self, history_tool):
        """Negative offset raises ToolError with VALIDATION_INVALID_PARAMETER."""
        states = _make_history_states(5)
        with self._patch_ws(states), pytest.raises(ToolError) as exc_info:
            await history_tool(entity_ids="sensor.test", offset="-1")

        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"

    @pytest.mark.asyncio
    async def test_invalid_limit_raises_tool_error(self, history_tool):
        """Non-numeric limit raises ToolError with VALIDATION_INVALID_PARAMETER."""
        states = _make_history_states(5)
        with self._patch_ws(states), pytest.raises(ToolError) as exc_info:
            await history_tool(entity_ids="sensor.test", limit="not_a_number")

        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"


# ---------------------------------------------------------------------------
# Tests: statistics source
# ---------------------------------------------------------------------------


class TestStatisticsPagination:
    """Pagination for source='statistics'."""

    @pytest.fixture
    def mock_client(self):
        return _make_mock_client()

    @pytest.fixture
    def history_tool(self, mock_client):
        return HistoryTools(mock_client).ha_get_history

    def _patch_ws(self, rows: list[dict]):
        ws = _make_ws_client_mock(stat_result={"sensor.energy": rows})
        return patch(
            "ha_mcp.tools.tools_history.get_connected_ws_client",
            return_value=(ws, None),
        )

    @pytest.mark.asyncio
    async def test_default_limit_applied(self, history_tool):
        """Without explicit limit, default (100) is applied."""
        rows = _make_stat_rows(150)
        with self._patch_ws(rows), patch("ha_mcp.tools.tools_history.add_timezone_metadata", side_effect=lambda _c, d: d):
            result = await history_tool(
                entity_ids="sensor.energy", source="statistics", start_time="30d"
            )

        entity = result["entities"][0]
        assert entity["count"] == 100
        assert entity["total_count"] == 150
        assert entity["has_more"] is True
        assert entity["next_offset"] == 100

    @pytest.mark.asyncio
    async def test_offset_skips_rows(self, history_tool):
        """offset=10 skips the first 10 statistics rows."""
        rows = _make_stat_rows(20)
        with self._patch_ws(rows), patch("ha_mcp.tools.tools_history.add_timezone_metadata", side_effect=lambda _c, d: d):
            result = await history_tool(
                entity_ids="sensor.energy", source="statistics",
                start_time="30d", limit=5, offset=10,
            )

        entity = result["entities"][0]
        assert entity["count"] == 5
        assert entity["offset"] == 10
        assert entity["statistics"][0]["mean"] == 10.0

    @pytest.mark.asyncio
    async def test_offset_beyond_total_returns_empty(self, history_tool):
        """offset beyond available rows returns empty statistics."""
        rows = _make_stat_rows(5)
        with self._patch_ws(rows), patch("ha_mcp.tools.tools_history.add_timezone_metadata", side_effect=lambda _c, d: d):
            result = await history_tool(
                entity_ids="sensor.energy", source="statistics",
                start_time="30d", limit=5, offset=50,
            )

        entity = result["entities"][0]
        assert entity["statistics"] == []
        assert entity["has_more"] is False
        assert entity["next_offset"] is None

    @pytest.mark.asyncio
    async def test_pagination_fields_present(self, history_tool):
        """All standardized pagination fields present for statistics source."""
        rows = _make_stat_rows(3)
        with self._patch_ws(rows), patch("ha_mcp.tools.tools_history.add_timezone_metadata", side_effect=lambda _c, d: d):
            result = await history_tool(
                entity_ids="sensor.energy", source="statistics", start_time="30d", limit=2
            )

        entity = result["entities"][0]
        assert PAGINATION_FIELDS.issubset(entity.keys())

    @pytest.mark.asyncio
    async def test_negative_offset_raises_tool_error(self, history_tool):
        """Negative offset raises ToolError for statistics source."""
        rows = _make_stat_rows(5)
        with self._patch_ws(rows), pytest.raises(ToolError) as exc_info:
            await history_tool(
                entity_ids="sensor.energy", source="statistics",
                start_time="30d", offset="-5",
            )

        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"

    @pytest.mark.asyncio
    async def test_invalid_limit_raises_tool_error(self, history_tool):
        """Non-numeric limit raises ToolError for statistics source."""
        rows = _make_stat_rows(5)
        with self._patch_ws(rows), pytest.raises(ToolError) as exc_info:
            await history_tool(
                entity_ids="sensor.energy", source="statistics",
                start_time="30d", limit="bad",
            )

        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
