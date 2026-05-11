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


_HISTORY_RESULT = {
    "success": True,
    "source": "history",
    "entities": [{"entity_id": "sensor.temp", "states": []}],
    "period": {"start": "2025-01-01T00:00:00+00:00", "end": "2025-01-02T00:00:00+00:00"},
    "query_params": {"minimal_response": True, "significant_changes_only": True, "limit": 100, "offset": 0},
    "time_zone": "UTC",
}


class TestHaGetHistoryFieldsProjection:
    """Unit tests for fields= projection in ha_get_history."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.base_url = "http://homeassistant.local"
        client.token = "test_token"
        client.verify_ssl = True
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
            return_value=dict(_HISTORY_RESULT),
        ):
            result = await history_tool(entity_ids="sensor.temp")
        assert set(result.keys()) == {"success", "source", "entities", "period", "query_params", "time_zone"}

    @pytest.mark.asyncio
    async def test_single_field_projects_to_that_key_plus_success(self, history_tool):
        with self._ws_patch(), patch(
            "ha_mcp.tools.tools_history._fetch_history",
            new_callable=AsyncMock,
            return_value=dict(_HISTORY_RESULT),
        ):
            result = await history_tool(entity_ids="sensor.temp", fields=["entities"])
        assert set(result.keys()) == {"success", "entities"}
        assert result["entities"][0]["entity_id"] == "sensor.temp"

    @pytest.mark.asyncio
    async def test_multiple_fields_projects_correctly(self, history_tool):
        with self._ws_patch(), patch(
            "ha_mcp.tools.tools_history._fetch_history",
            new_callable=AsyncMock,
            return_value=dict(_HISTORY_RESULT),
        ):
            result = await history_tool(entity_ids="sensor.temp", fields=["source", "period"])
        assert set(result.keys()) == {"success", "source", "period"}

    @pytest.mark.asyncio
    async def test_success_always_present_regardless_of_fields(self, history_tool):
        with self._ws_patch(), patch(
            "ha_mcp.tools.tools_history._fetch_history",
            new_callable=AsyncMock,
            return_value=dict(_HISTORY_RESULT),
        ):
            result = await history_tool(entity_ids="sensor.temp", fields=["time_zone"])
        assert "success" in result
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_unknown_field_silently_omitted(self, history_tool):
        with self._ws_patch(), patch(
            "ha_mcp.tools.tools_history._fetch_history",
            new_callable=AsyncMock,
            return_value=dict(_HISTORY_RESULT),
        ):
            result = await history_tool(entity_ids="sensor.temp", fields=["nonexistent"])
        assert set(result.keys()) == {"success"}
