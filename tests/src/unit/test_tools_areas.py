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


class TestRemoveAreaOrFloorRouting:
    """Confirms ha_remove_area_or_floor routes to the correct registry by kind.

    E2E covers both kinds via testcontainer roundtrips, but a swapped ternary
    (e.g., area → floor_registry) would only surface as a generic 'Failed to
    delete' message in E2E. Mocking the WS call here pins the message shape.
    """

    @pytest.fixture
    def tools(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock(return_value={"success": True, "result": None})
        return AreaTools(client)

    async def test_area_kind_routes_to_area_registry(self, tools):
        result = await tools.ha_remove_area_or_floor(kind="area", id="garage")
        sent = tools._client.send_websocket_message.call_args.args[0]
        assert sent == {"type": "config/area_registry/delete", "area_id": "garage"}
        assert result["success"] is True
        assert result["area_id"] == "garage"
        assert result["kind"] == "area"

    async def test_floor_kind_routes_to_floor_registry(self, tools):
        result = await tools.ha_remove_area_or_floor(kind="floor", id="ground")
        sent = tools._client.send_websocket_message.call_args.args[0]
        assert sent == {"type": "config/floor_registry/delete", "floor_id": "ground"}
        assert result["success"] is True
        assert result["floor_id"] == "ground"
        assert result["kind"] == "floor"


class TestSetAreaOrFloorRouting:
    """Confirms ha_set_area_or_floor routes to the correct WS message type by kind.

    Asserts the high-level routing (area_registry vs floor_registry, create vs
    update) without re-testing the message-builder helpers themselves.
    """

    @pytest.fixture
    def tools(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": {"area_id": "x", "floor_id": "x", "name": "X"}}
        )
        return AreaTools(client)

    async def test_area_create_routes_to_area_registry_create(self, tools):
        await tools.ha_set_area_or_floor(kind="area", name="Kitchen")
        sent = tools._client.send_websocket_message.call_args.args[0]
        assert sent["type"] == "config/area_registry/create"
        assert sent["name"] == "Kitchen"

    async def test_area_update_routes_to_area_registry_update(self, tools):
        await tools.ha_set_area_or_floor(kind="area", id="kitchen", name="K2")
        sent = tools._client.send_websocket_message.call_args.args[0]
        assert sent["type"] == "config/area_registry/update"
        assert sent["area_id"] == "kitchen"

    async def test_floor_create_routes_to_floor_registry_create(self, tools):
        await tools.ha_set_area_or_floor(kind="floor", name="Ground", level=0)
        sent = tools._client.send_websocket_message.call_args.args[0]
        assert sent["type"] == "config/floor_registry/create"
        assert sent["name"] == "Ground"
        assert sent["level"] == 0

    async def test_floor_update_routes_to_floor_registry_update(self, tools):
        await tools.ha_set_area_or_floor(kind="floor", id="ground", level=2)
        sent = tools._client.send_websocket_message.call_args.args[0]
        assert sent["type"] == "config/floor_registry/update"
        assert sent["floor_id"] == "ground"
        assert sent["level"] == 2


class TestSetAreaOrFloorCrossKindRejection:
    """Cross-kind parameters (e.g., picture under kind='floor') must be rejected
    with VALIDATION_INVALID_PARAMETER rather than silently dropped."""

    @pytest.fixture
    def tools(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return AreaTools(client)

    async def test_level_rejected_for_area(self, tools):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_area_or_floor(kind="area", name="Kitchen", level=1)
        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "level" in error_data["error"]["message"]
        # No WS call should have been made
        tools._client.send_websocket_message.assert_not_called()

    async def test_floor_id_rejected_for_floor(self, tools):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_area_or_floor(kind="floor", name="Ground", floor_id="ground")
        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "floor_id" in error_data["error"]["message"]
        tools._client.send_websocket_message.assert_not_called()

    async def test_picture_rejected_for_floor(self, tools):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_area_or_floor(kind="floor", name="Ground", picture="http://x")
        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "picture" in error_data["error"]["message"]
        tools._client.send_websocket_message.assert_not_called()

    async def test_empty_id_rejected(self, tools):
        """id='' must not silently route to create — it indicates a malformed call."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_area_or_floor(kind="area", id="", name="K")
        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "non-empty" in error_data["error"]["message"]
        tools._client.send_websocket_message.assert_not_called()


class TestHaConfigListAreasFieldsProjection:
    """Unit tests for fields= projection in ha_config_list_areas."""

    @pytest.fixture
    def tools(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock(return_value={
            "success": True,
            "result": [
                {"area_id": "kitchen", "name": "Kitchen"},
                {"area_id": "bedroom", "name": "Bedroom"},
            ],
        })
        return AreaTools(client)

    async def test_no_fields_returns_full_response(self, tools):
        result = await tools.ha_config_list_areas()
        assert set(result.keys()) == {"success", "count", "areas", "message"}

    async def test_single_field_projects_to_that_key_plus_success(self, tools):
        result = await tools.ha_config_list_areas(fields=["areas"])
        assert set(result.keys()) == {"success", "areas"}
        assert len(result["areas"]) == 2

    async def test_multiple_fields_projects_correctly(self, tools):
        result = await tools.ha_config_list_areas(fields=["count", "areas"])
        assert set(result.keys()) == {"success", "count", "areas"}
        assert result["count"] == 2

    async def test_success_always_included_regardless_of_fields(self, tools):
        result = await tools.ha_config_list_areas(fields=["count"])
        assert "success" in result
        assert result["success"] is True

    async def test_unknown_field_silently_omitted(self, tools):
        result = await tools.ha_config_list_areas(fields=["nonexistent_key"])
        assert set(result.keys()) == {"success"}

    async def test_bad_fields_integer_raises_tool_error(self, tools):
        """fields=123 raises ToolError with VALIDATION_FAILED + parameter='fields'.

        Covers the early-validate raise path in ``ha_config_list_areas`` so a
        regression dropping the try/except still surfaces.
        """
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_list_areas(fields=123)
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert error.get("parameter") == "fields"

    async def test_bad_json_fields_raises_tool_error(self, tools):
        """fields='[\"' (malformed JSON) raises ToolError."""
        with pytest.raises(ToolError):
            await tools.ha_config_list_areas(fields='["')
