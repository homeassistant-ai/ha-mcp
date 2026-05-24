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
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": None}
        )
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
            return_value={
                "success": True,
                "result": {"area_id": "x", "floor_id": "x", "name": "X"},
            }
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
            await tools.ha_set_area_or_floor(
                kind="floor", name="Ground", floor_id="ground"
            )
        error_data = json.loads(str(exc_info.value))
        assert error_data["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "floor_id" in error_data["error"]["message"]
        tools._client.send_websocket_message.assert_not_called()

    async def test_picture_rejected_for_floor(self, tools):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_set_area_or_floor(
                kind="floor", name="Ground", picture="http://x"
            )
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


class TestListFloorsAreasFieldsProjection:
    """Unit tests for fields= top-level projection in ha_list_floors_areas."""

    @pytest.fixture
    def tools(self):
        client = MagicMock()

        def _route(msg):
            return {
                "config/area_registry/list": {
                    "success": True,
                    "result": [
                        {"area_id": "kitchen", "name": "Kitchen", "floor_id": "ground"},
                        {"area_id": "garage", "name": "Garage", "floor_id": None},
                    ],
                },
                "config/floor_registry/list": {
                    "success": True,
                    "result": [{"floor_id": "ground", "name": "Ground", "level": 0}],
                },
            }[msg["type"]]

        client.send_websocket_message = AsyncMock(side_effect=_route)
        return AreaTools(client)

    async def test_no_fields_returns_full_response(self, tools):
        result = await tools.ha_list_floors_areas()
        assert set(result.keys()) == {
            "success",
            "floor_count",
            "area_count",
            "unassigned_count",
            "orphaned_count",
            "floors",
            "unassigned_areas",
            "orphaned_areas",
            "message",
        }

    async def test_fields_projects_top_level(self, tools):
        result = await tools.ha_list_floors_areas(fields=["floors"])
        assert set(result.keys()) == {"success", "floors"}

    async def test_multiple_fields_projects_correctly(self, tools):
        result = await tools.ha_list_floors_areas(fields=["floor_count", "area_count"])
        assert set(result.keys()) == {"success", "floor_count", "area_count"}

    async def test_success_always_retained(self, tools):
        result = await tools.ha_list_floors_areas(fields=["floor_count"])
        assert result["success"] is True

    async def test_unknown_field_emits_warning(self, tools):
        result = await tools.ha_list_floors_areas(fields=["nonexistent_key"])
        assert result["success"] is True
        assert "warnings" in result
        assert any("nonexistent_key" in w for w in result["warnings"])

    async def test_bad_fields_integer_raises_tool_error(self, tools):
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_list_floors_areas(fields=123)
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert error.get("parameter") == "fields"

    async def test_bad_json_fields_raises_tool_error(self, tools):
        with pytest.raises(ToolError):
            await tools.ha_list_floors_areas(fields='["')


class TestListFloorsAreasAreaFieldsProjection:
    """Unit tests for area_fields= per-record projection across all 3 area buckets."""

    @pytest.fixture
    def tools(self):
        client = MagicMock()

        def _route(msg):
            return {
                "config/area_registry/list": {
                    "success": True,
                    "result": [
                        {
                            "area_id": "kitchen",
                            "name": "Kitchen",
                            "icon": "mdi:silverware",
                            "floor_id": "ground",
                        },
                        {
                            "area_id": "garage",
                            "name": "Garage",
                            "icon": None,
                            "floor_id": None,
                        },
                        {
                            "area_id": "ghost",
                            "name": "Ghost",
                            "icon": None,
                            "floor_id": "deleted_floor_id",
                        },
                    ],
                },
                "config/floor_registry/list": {
                    "success": True,
                    "result": [{"floor_id": "ground", "name": "Ground", "level": 0}],
                },
            }[msg["type"]]

        client.send_websocket_message = AsyncMock(side_effect=_route)
        return AreaTools(client)

    async def test_area_fields_projects_nested_areas(self, tools):
        """Areas nested under floors are projected to only the requested keys."""
        result = await tools.ha_list_floors_areas(area_fields=["area_id"])
        ground = next(f for f in result["floors"] if f["floor_id"] == "ground")
        assert all(set(a.keys()) == {"area_id"} for a in ground["areas"])

    async def test_area_fields_projects_unassigned_areas(self, tools):
        result = await tools.ha_list_floors_areas(area_fields=["area_id"])
        assert all(set(a.keys()) == {"area_id"} for a in result["unassigned_areas"])

    async def test_area_fields_projects_orphaned_areas(self, tools):
        result = await tools.ha_list_floors_areas(area_fields=["area_id"])
        assert all(set(a.keys()) == {"area_id"} for a in result["orphaned_areas"])

    async def test_area_fields_does_not_affect_floor_records(self, tools):
        """Floor metadata (name, level, etc.) is untouched — only area records project."""
        result = await tools.ha_list_floors_areas(area_fields=["area_id"])
        ground = next(f for f in result["floors"] if f["floor_id"] == "ground")
        assert "name" in ground
        assert "level" in ground

    async def test_area_fields_multiple_keys(self, tools):
        result = await tools.ha_list_floors_areas(area_fields=["area_id", "name"])
        ground = next(f for f in result["floors"] if f["floor_id"] == "ground")
        for area in ground["areas"]:
            assert set(area.keys()) == {"area_id", "name"}

    async def test_area_fields_unknown_key_emits_warning(self, tools):
        """area_fields with only unknown keys emits the typo-guard warning."""
        result = await tools.ha_list_floors_areas(area_fields=["are_id"])
        assert "warnings" in result, (
            "Expected warnings key when all projected records are empty"
        )
        assert any("are_id" in w for w in result["warnings"]), (
            f"Expected typo field name in warning, got: {result['warnings']}"
        )

    async def test_area_fields_does_not_affect_outer_response_keys(self, tools):
        """area_fields only projects inside area records; top-level keys are unchanged."""
        result = await tools.ha_list_floors_areas(area_fields=["area_id"])
        assert "success" in result
        assert "floor_count" in result
        assert "area_count" in result
        assert "floors" in result
        assert "unassigned_areas" in result
        assert "orphaned_areas" in result
