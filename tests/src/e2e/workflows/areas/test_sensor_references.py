"""Area temperature/humidity sensor references through ha_set_area_or_floor.

Issue #2619: neither MCP path could change these references. The raw
``config/area_registry/update`` command is guarded, and the guarded setter had
no parameter for either field.
"""

import json
import uuid
from typing import Any

import pytest

from ...utilities.assertions import MCPAssertions, parse_mcp_result, safe_call_tool
from .test_lifecycle import _flatten_areas, generate_unique_name


async def _read_area(mcp_client, area_id: str) -> dict[str, Any]:
    list_data = parse_mcp_result(await mcp_client.call_tool("ha_list_floors_areas", {}))
    assert list_data.get("success"), f"List failed: {list_data}"
    area = next(
        (a for a in _flatten_areas(list_data) if a.get("area_id") == area_id),
        None,
    )
    assert area is not None, f"Area {area_id} not found: {list_data}"
    return area


@pytest.mark.area
class TestAreaSensorReferences:
    async def test_set_replace_and_clear(self, mcp_client, cleanup_tracker):
        """Set both references on create, replace one, then clear both.

        A partial update must leave the omitted reference and the name untouched;
        an empty string clears, matching the icon/picture/floor_id convention.
        """
        area_name = generate_unique_name("test_sensor_area")
        area_id = None
        try:
            create_data = parse_mcp_result(
                await mcp_client.call_tool(
                    "ha_set_area_or_floor",
                    {
                        "kind": "area",
                        "name": area_name,
                        "temperature_entity_id": "sensor.demo_temperature",
                        "humidity_entity_id": "sensor.demo_humidity",
                    },
                )
            )
            assert create_data.get("success"), f"Create failed: {create_data}"
            area_id = create_data["area_id"]
            cleanup_tracker.track("area", area_id)

            area = await _read_area(mcp_client, area_id)
            assert area.get("temperature_entity_id") == "sensor.demo_temperature", area
            assert area.get("humidity_entity_id") == "sensor.demo_humidity", area

            update_data = parse_mcp_result(
                await mcp_client.call_tool(
                    "ha_set_area_or_floor",
                    {
                        "kind": "area",
                        "id": area_id,
                        "temperature_entity_id": "sensor.demo_outside_temperature",
                    },
                )
            )
            assert update_data.get("success"), f"Update failed: {update_data}"
            area = await _read_area(mcp_client, area_id)
            assert (
                area.get("temperature_entity_id") == "sensor.demo_outside_temperature"
            )
            assert area.get("humidity_entity_id") == "sensor.demo_humidity", area
            assert area.get("name") == area_name, area

            clear_data = parse_mcp_result(
                await mcp_client.call_tool(
                    "ha_set_area_or_floor",
                    {
                        "kind": "area",
                        "id": area_id,
                        "temperature_entity_id": "",
                        "humidity_entity_id": "",
                    },
                )
            )
            assert clear_data.get("success"), f"Clear failed: {clear_data}"
            area = await _read_area(mcp_client, area_id)
            assert area.get("temperature_entity_id") is None, area
            assert area.get("humidity_entity_id") is None, area
        finally:
            if area_id:
                await safe_call_tool(
                    mcp_client,
                    "ha_remove_area_or_floor",
                    {"kind": "area", "id": area_id},
                )

    async def test_wrong_device_class_is_rejected(self, mcp_client):
        """A temperature sensor offered as the humidity reference surfaces HA's
        rejection as a failed call that names the entity, not a success envelope."""
        mcp = MCPAssertions(mcp_client)
        data = await mcp.call_tool_failure(
            "ha_set_area_or_floor",
            {
                "kind": "area",
                "name": f"Sensor Ref Area {uuid.uuid4().hex[:8]}",
                "humidity_entity_id": "sensor.demo_temperature",
            },
        )

        assert data["error"]["code"] == "SERVICE_CALL_FAILED", (
            f"Expected SERVICE_CALL_FAILED, got: {data.get('error')}"
        )
        assert "sensor.demo_temperature" in json.dumps(data), (
            f"Error must name the offending entity: {data}"
        )
