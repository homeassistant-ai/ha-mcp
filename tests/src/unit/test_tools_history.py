"""Unit tests for ha_get_history tool exception handling."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_history import HistoryTools


class TestHaGetHistoryExceptionSuggestions:
    """Test that except Exception provides source-specific error suggestions."""

    @pytest.fixture
    def mock_client(self):
        """Create a minimal mock HA client."""
        client = MagicMock()
        client.base_url = "http://homeassistant.local"
        client.token = "test_token"
        return client

    @pytest.fixture
    def history_tool(self, mock_client):
        """Create HistoryTools instance and return ha_get_history."""
        tools = HistoryTools(mock_client)
        return tools.ha_get_history

    @pytest.mark.asyncio
    async def test_statistics_exception_includes_state_class_hint(self, history_tool):
        """Unexpected exception with source=statistics surfaces state_class suggestion."""
        with (
            patch(
                "ha_mcp.tools.tools_history.get_connected_ws_client",
                side_effect=RuntimeError("unexpected"),
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await history_tool(entity_ids="sensor.test", source="statistics")

        suggestions = json.loads(str(exc_info.value))["error"]["suggestions"]
        assert any("state_class" in s for s in suggestions)

    @pytest.mark.asyncio
    async def test_history_exception_does_not_include_state_class_hint(
        self, history_tool
    ):
        """Unexpected exception with source=history does not surface state_class suggestion."""
        with (
            patch(
                "ha_mcp.tools.tools_history.get_connected_ws_client",
                side_effect=RuntimeError("unexpected"),
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await history_tool(entity_ids="sensor.test", source="history")

        suggestions = json.loads(str(exc_info.value))["error"]["suggestions"]
        assert not any("state_class" in s for s in suggestions)
        assert any("entity" in s.lower() for s in suggestions)


# _fetch_history returns the unwrapped inner payload; ha_get_history then runs
# project_fields and wraps with add_timezone_metadata at the call site.
_HISTORY_INNER = {
    "success": True,
    "source": "history",
    "entities": [{"entity_id": "sensor.temp", "states": []}],
    "period": {"start": "2025-01-01T00:00:00+00:00", "end": "2025-01-02T00:00:00+00:00"},
    "query_params": {"minimal_response": True, "significant_changes_only": True, "limit": 100, "offset": 0},
}


class TestHaGetHistoryFieldsProjection:
    """Unit tests for fields= projection in ha_get_history."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.base_url = "http://homeassistant.local"
        client.token = "test_token"
        client.verify_ssl = True
        # add_timezone_metadata is invoked at the projection call site and reads
        # client.get_config(); mock it so the wrapper returns deterministically.
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        return client

    @pytest.fixture
    def history_tool(self, mock_client):
        return HistoryTools(mock_client).ha_get_history

    def _ws_patch(self):
        ws = AsyncMock()
        ws.disconnect = AsyncMock()
        return patch(
            "ha_mcp.tools.tools_history.get_connected_ws_client",
            new_callable=AsyncMock,
            return_value=(ws, None),
        )

    @pytest.mark.asyncio
    async def test_no_fields_returns_full_response(self, history_tool):
        with self._ws_patch(), patch(
            "ha_mcp.tools.tools_history._fetch_history",
            new_callable=AsyncMock,
            return_value=dict(_HISTORY_INNER),
        ):
            result = await history_tool(entity_ids="sensor.temp")
        assert "data" in result
        assert "metadata" in result
        assert set(result["data"].keys()) == {"success", "source", "entities", "period", "query_params"}

    @pytest.mark.asyncio
    async def test_single_field_projects_to_that_key_plus_success(self, history_tool):
        with self._ws_patch(), patch(
            "ha_mcp.tools.tools_history._fetch_history",
            new_callable=AsyncMock,
            return_value=dict(_HISTORY_INNER),
        ):
            result = await history_tool(entity_ids="sensor.temp", fields=["entities"])
        assert set(result["data"].keys()) == {"success", "entities"}
        assert result["data"]["entities"][0]["entity_id"] == "sensor.temp"
        assert "metadata" in result

    @pytest.mark.asyncio
    async def test_multiple_fields_projects_correctly(self, history_tool):
        with self._ws_patch(), patch(
            "ha_mcp.tools.tools_history._fetch_history",
            new_callable=AsyncMock,
            return_value=dict(_HISTORY_INNER),
        ):
            result = await history_tool(entity_ids="sensor.temp", fields=["source", "period"])
        assert set(result["data"].keys()) == {"success", "source", "period"}
        assert "metadata" in result

    @pytest.mark.asyncio
    async def test_success_always_present_regardless_of_fields(self, history_tool):
        with self._ws_patch(), patch(
            "ha_mcp.tools.tools_history._fetch_history",
            new_callable=AsyncMock,
            return_value=dict(_HISTORY_INNER),
        ):
            result = await history_tool(entity_ids="sensor.temp", fields=["source"])
        assert "success" in result["data"]
        assert result["data"]["success"] is True

    @pytest.mark.asyncio
    async def test_unknown_field_silently_omitted(self, history_tool):
        with self._ws_patch(), patch(
            "ha_mcp.tools.tools_history._fetch_history",
            new_callable=AsyncMock,
            return_value=dict(_HISTORY_INNER),
        ):
            result = await history_tool(entity_ids="sensor.temp", fields=["nonexistent"])
        assert set(result["data"].keys()) == {"success"}

    @pytest.mark.asyncio
    async def test_malformed_fields_raises_tool_error(self, history_tool):
        with pytest.raises(ToolError):
            await history_tool(entity_ids="sensor.temp", fields=123)

    @pytest.mark.asyncio
    async def test_bad_json_fields_raises_tool_error(self, history_tool):
        with pytest.raises(ToolError):
            await history_tool(entity_ids="sensor.temp", fields='["')


_ORDER_INNER_STUB = {
    "success": True,
    "source": "history",
    "entities": [{"entity_id": "sensor.temp", "states": []}],
    "period": {"start": "2025-01-01T00:00:00+00:00", "end": "2025-01-02T00:00:00+00:00"},
    "query_params": {
        "minimal_response": True,
        "significant_changes_only": True,
        "limit": 100,
        "offset": 0,
        "order": "desc",
    },
}

_STATISTICS_INNER = {
    "success": True,
    "source": "statistics",
    "entities": [{"entity_id": "sensor.energy", "statistics": []}],
    "period_type": "hour",
    "time_range": {"start": "2025-01-01T00:00:00+00:00", "end": "2025-01-02T00:00:00+00:00"},
    "statistic_types": ["mean"],
    "query_params": {"limit": 100, "offset": 0},
}


class TestHaGetHistoryStatisticsFieldsProjection:
    """Unit tests for fields= projection in ha_get_history with source='statistics'."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.base_url = "http://homeassistant.local"
        client.token = "test_token"
        client.verify_ssl = True
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        return client

    @pytest.fixture
    def history_tool(self, mock_client):
        return HistoryTools(mock_client).ha_get_history

    def _ws_patch(self):
        ws = AsyncMock()
        ws.disconnect = AsyncMock()
        return patch(
            "ha_mcp.tools.tools_history.get_connected_ws_client",
            new_callable=AsyncMock,
            return_value=(ws, None),
        )

    @pytest.mark.asyncio
    async def test_no_fields_returns_full_response(self, history_tool):
        with self._ws_patch(), patch(
            "ha_mcp.tools.tools_history._fetch_statistics",
            new_callable=AsyncMock,
            return_value=dict(_STATISTICS_INNER),
        ):
            result = await history_tool(entity_ids="sensor.energy", source="statistics")
        assert "data" in result
        assert "metadata" in result
        assert set(result["data"].keys()) == {
            "success", "source", "entities", "period_type",
            "time_range", "statistic_types", "query_params",
        }

    @pytest.mark.asyncio
    async def test_single_field_projection(self, history_tool):
        with self._ws_patch(), patch(
            "ha_mcp.tools.tools_history._fetch_statistics",
            new_callable=AsyncMock,
            return_value=dict(_STATISTICS_INNER),
        ):
            result = await history_tool(
                entity_ids="sensor.energy", source="statistics", fields=["entities"]
            )
        assert set(result["data"].keys()) == {"success", "entities"}
        assert result["data"]["entities"][0]["entity_id"] == "sensor.energy"

    @pytest.mark.asyncio
    async def test_stats_specific_key_period_type(self, history_tool):
        with self._ws_patch(), patch(
            "ha_mcp.tools.tools_history._fetch_statistics",
            new_callable=AsyncMock,
            return_value=dict(_STATISTICS_INNER),
        ):
            result = await history_tool(
                entity_ids="sensor.energy", source="statistics", fields=["period_type"]
            )
        assert set(result["data"].keys()) == {"success", "period_type"}
        assert result["data"]["period_type"] == "hour"

    @pytest.mark.asyncio
    async def test_success_always_present(self, history_tool):
        with self._ws_patch(), patch(
            "ha_mcp.tools.tools_history._fetch_statistics",
            new_callable=AsyncMock,
            return_value=dict(_STATISTICS_INNER),
        ):
            result = await history_tool(
                entity_ids="sensor.energy", source="statistics", fields=["entities"]
            )
        assert result["data"]["success"] is True

    @pytest.mark.asyncio
    async def test_unknown_field_omitted(self, history_tool):
        with self._ws_patch(), patch(
            "ha_mcp.tools.tools_history._fetch_statistics",
            new_callable=AsyncMock,
            return_value=dict(_STATISTICS_INNER),
        ):
            result = await history_tool(
                entity_ids="sensor.energy", source="statistics", fields=["nonexistent"]
            )
        assert set(result["data"].keys()) == {"success"}

    @pytest.mark.asyncio
    async def test_malformed_fields_raises_tool_error(self, history_tool):
        with pytest.raises(ToolError):
            await history_tool(entity_ids="sensor.energy", source="statistics", fields=123)

    @pytest.mark.asyncio
    async def test_bad_json_fields_raises_tool_error(self, history_tool):
        with pytest.raises(ToolError):
            await history_tool(entity_ids="sensor.energy", source="statistics", fields='["')


class TestHaGetHistoryOrder:
    """Tests for the order= parameter (issue #1199).

    Verifies that the order parameter is threaded through to _fetch_history
    (which is responsible for the actual reversal).
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.base_url = "http://homeassistant.local"
        client.token = "test_token"
        client.verify_ssl = True
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        return client

    @pytest.fixture
    def history_tool(self, mock_client):
        return HistoryTools(mock_client).ha_get_history

    def _ws_patch(self):
        ws = AsyncMock()
        ws.disconnect = AsyncMock()
        return patch(
            "ha_mcp.tools.tools_history.get_connected_ws_client",
            new_callable=AsyncMock,
            return_value=(ws, None),
        )

    @pytest.mark.asyncio
    async def test_order_desc_default_passed_to_fetch_history(self, history_tool):
        """Default order='desc' is threaded through to _fetch_history."""
        with self._ws_patch(), patch(
            "ha_mcp.tools.tools_history._fetch_history",
            new_callable=AsyncMock,
            return_value=dict(_ORDER_INNER_STUB),
        ) as mock_fetch:
            await history_tool(entity_ids="sensor.temp")
        _args, _kwargs = mock_fetch.call_args
        assert _kwargs.get("order") == "desc" or "desc" in _args

    @pytest.mark.asyncio
    async def test_order_asc_passed_to_fetch_history(self, history_tool):
        """order='asc' is passed through to _fetch_history unchanged."""
        with self._ws_patch(), patch(
            "ha_mcp.tools.tools_history._fetch_history",
            new_callable=AsyncMock,
            return_value=dict(_ORDER_INNER_STUB),
        ) as mock_fetch:
            await history_tool(entity_ids="sensor.temp", order="asc")
        _args, _kwargs = mock_fetch.call_args
        assert _kwargs.get("order") == "asc" or "asc" in _args

    @pytest.mark.asyncio
    async def test_order_ignored_for_statistics_source(self, history_tool):
        """order= is not passed to _fetch_statistics (statistics has no ordering param)."""
        _stats_stub = {
            "success": True,
            "source": "statistics",
            "entities": [],
            "period_type": "day",
            "time_range": {"start": "2025-01-01T00:00:00+00:00", "end": "2025-01-02T00:00:00+00:00"},
            "statistic_types": ["mean"],
            "query_params": {"limit": 100, "offset": 0},
        }
        with self._ws_patch(), patch(
            "ha_mcp.tools.tools_history._fetch_statistics",
            new_callable=AsyncMock,
            return_value=_stats_stub,
        ) as mock_stats:
            await history_tool(entity_ids="sensor.energy", source="statistics", order="asc")
        # _fetch_statistics should be called, not _fetch_history
        mock_stats.assert_called_once()
