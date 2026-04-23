"""Unit tests for AreaTools — orphaned partition branch + malformed-WS-response guard.

Both paths are untriggerable from E2E: the orphaned branch needs .storage drift
between the two sequential WS reads, and the SERVICE_CALL_FAILED guard needs a
malformed WS response with success=True but no "result" key. Mocking the client
covers both cheaply.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_areas import AreaTools


class TestHomeTopologyPartition:
    """Covers the orphaned partition branch that E2E cannot reach."""

    @pytest.fixture
    def tools(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return AreaTools(client)

    async def test_orphaned_area_partition(self, tools):
        """An area whose floor_id points to a non-existent floor lands in orphaned_areas."""
        tools._client.send_websocket_message.side_effect = [
            # areas: one nested, one orphaned, one unassigned
            {
                "success": True,
                "result": [
                    {"area_id": "kitchen", "floor_id": "ground"},
                    {"area_id": "ghost", "floor_id": "deleted_floor_id"},
                    {"area_id": "loose", "floor_id": None},
                ],
            },
            # floors: only "ground" exists — "deleted_floor_id" is not present
            {
                "success": True,
                "result": [
                    {"floor_id": "ground", "name": "Ground", "level": 0},
                ],
            },
        ]

        result = await tools.ha_list_floors_areas()

        assert result["success"] is True
        assert result["orphaned_count"] == 1
        assert result["unassigned_count"] == 1
        assert [a["area_id"] for a in result["orphaned_areas"]] == ["ghost"]
        assert [a["area_id"] for a in result["unassigned_areas"]] == ["loose"]

        ground = next(f for f in result["floors"] if f["floor_id"] == "ground")
        assert [a["area_id"] for a in ground["areas"]] == ["kitchen"]


class TestHomeTopologyMalformedResponseGuard:
    """Covers the SERVICE_CALL_FAILED guard for malformed WS responses."""

    @pytest.fixture
    def tools(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return AreaTools(client)

    async def test_malformed_ws_response_triggers_guard(self, tools):
        """success=True without a "result" key must raise SERVICE_CALL_FAILED, not silently return empty counts."""
        tools._client.send_websocket_message.side_effect = [
            {"success": True},  # malformed — no "result" key
            {"success": True, "result": []},
        ]

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_list_floors_areas()

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert error_data["error"]["code"] == "SERVICE_CALL_FAILED"
