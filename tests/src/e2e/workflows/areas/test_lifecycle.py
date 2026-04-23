"""
Area and Floor Management E2E Tests

Comprehensive tests for Home Assistant area and floor management via MCP tools.

This test suite validates:
- Area creation, listing, updating, and deletion
- Floor creation, listing, updating, and deletion
- Area-to-floor assignments
- Aliases and icon management
"""

import logging
import uuid
from typing import Any

import pytest

from ...utilities.assertions import parse_mcp_result

logger = logging.getLogger(__name__)

# Delay constants for registry operations (in seconds)
REGISTRY_OPERATION_DELAY = 0.5  # Time to wait after create/delete for registry sync
BATCH_OPERATION_DELAY = 0.2  # Time to wait between batch operations


def generate_unique_name(prefix: str) -> str:
    """Generate a unique name for test entities to avoid conflicts."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@pytest.mark.area
class TestAreaLifecycle:
    """Test complete area management workflows."""

    async def test_area_create_list_delete(self, mcp_client, cleanup_tracker):
        """
        Test: Create area -> List areas -> Delete area

        Validates basic area CRUD operations.
        """
        area_name = generate_unique_name("test_area")
        logger.info(f"Testing area lifecycle: {area_name}")

        # 1. CREATE: Basic area
        create_result = await mcp_client.call_tool(
            "ha_config_set_area",
            {
                "name": area_name,
                "icon": "mdi:sofa",
            },
        )

        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), f"Failed to create area: {create_data}"

        area_id = create_data.get("area_id")
        assert area_id, f"No area_id returned: {create_data}"
        cleanup_tracker.track("area", area_id)
        logger.info(f"Created area: {area_name} (ID: {area_id})")

        # 2. LIST: Verify area exists in list
        list_result = await mcp_client.call_tool("ha_config_list_areas", {})

        list_data = parse_mcp_result(list_result)
        assert list_data.get("success"), f"Failed to list areas: {list_data}"

        areas = list_data.get("areas", [])
        found_area = next(
            (a for a in areas if a.get("area_id") == area_id),
            None,
        )
        assert found_area is not None, f"Created area not found in list: {area_id}"
        assert found_area.get("name") == area_name, (
            f"Area name mismatch: {found_area.get('name')}"
        )
        logger.info(f"Verified area in list: {found_area}")

        # 3. DELETE: Remove the area
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_area",
            {"area_id": area_id},
        )

        delete_data = parse_mcp_result(delete_result)
        assert delete_data.get("success"), f"Failed to delete area: {delete_data}"
        logger.info(f"Deleted area: {area_id}")

        # 4. VERIFY: Area no longer in list
        verify_result = await mcp_client.call_tool("ha_config_list_areas", {})
        verify_data = parse_mcp_result(verify_result)

        areas_after = verify_data.get("areas", [])
        found_after = next(
            (a for a in areas_after if a.get("area_id") == area_id),
            None,
        )
        assert found_after is None, f"Area still exists after deletion: {area_id}"
        logger.info("Area deletion verified")

    async def test_area_update(self, mcp_client, cleanup_tracker):
        """
        Test: Create area -> Update area -> Verify changes

        Validates area update operations.
        """
        area_name = generate_unique_name("test_update_area")
        new_name = generate_unique_name("updated_area")
        logger.info(f"Testing area update: {area_name}")

        # 1. CREATE: Initial area
        create_result = await mcp_client.call_tool(
            "ha_config_set_area",
            {
                "name": area_name,
                "icon": "mdi:sofa",
            },
        )

        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), f"Failed to create area: {create_data}"

        area_id = create_data.get("area_id")
        cleanup_tracker.track("area", area_id)
        logger.info(f"Created area: {area_name} (ID: {area_id})")

        # 2. UPDATE: Change name and icon
        update_result = await mcp_client.call_tool(
            "ha_config_set_area",
            {
                "area_id": area_id,
                "name": new_name,
                "icon": "mdi:bed",
            },
        )

        update_data = parse_mcp_result(update_result)
        assert update_data.get("success"), f"Failed to update area: {update_data}"
        logger.info(f"Updated area: {area_id}")

        # 3. VERIFY: Check changes in list
        list_result = await mcp_client.call_tool("ha_config_list_areas", {})
        list_data = parse_mcp_result(list_result)

        areas = list_data.get("areas", [])
        found_area = next(
            (a for a in areas if a.get("area_id") == area_id),
            None,
        )

        assert found_area is not None, f"Updated area not found: {area_id}"
        assert found_area.get("name") == new_name, (
            f"Name not updated: {found_area.get('name')}"
        )
        assert found_area.get("icon") == "mdi:bed", (
            f"Icon not updated: {found_area.get('icon')}"
        )
        logger.info(f"Verified updated area: {found_area}")

        # 4. CLEANUP
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_area",
            {"area_id": area_id},
        )
        delete_data = parse_mcp_result(delete_result)
        assert delete_data.get("success"), f"Failed to delete area: {delete_data}"
        logger.info("Area cleanup completed")

    async def test_area_with_aliases(self, mcp_client, cleanup_tracker):
        """
        Test: Create area with aliases -> Verify aliases

        Validates alias functionality for voice assistants.
        """
        area_name = generate_unique_name("test_alias_area")
        aliases = ["lounge", "sitting room"]
        logger.info(f"Testing area with aliases: {area_name}")

        # 1. CREATE: Area with aliases
        create_result = await mcp_client.call_tool(
            "ha_config_set_area",
            {
                "name": area_name,
                "aliases": aliases,
                "icon": "mdi:sofa",
            },
        )

        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), f"Failed to create area: {create_data}"

        area_id = create_data.get("area_id")
        cleanup_tracker.track("area", area_id)
        logger.info(f"Created area with aliases: {area_id}")

        # 2. VERIFY: Check aliases in list
        list_result = await mcp_client.call_tool("ha_config_list_areas", {})
        list_data = parse_mcp_result(list_result)

        areas = list_data.get("areas", [])
        found_area = next(
            (a for a in areas if a.get("area_id") == area_id),
            None,
        )

        assert found_area is not None, f"Area not found: {area_id}"
        area_aliases = found_area.get("aliases", [])
        for alias in aliases:
            assert alias in area_aliases, f"Alias '{alias}' not found in {area_aliases}"
        logger.info(f"Verified aliases: {area_aliases}")

        # 3. CLEANUP
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_area",
            {"area_id": area_id},
        )
        delete_data = parse_mcp_result(delete_result)
        assert delete_data.get("success"), f"Failed to delete area: {delete_data}"
        logger.info("Area cleanup completed")


@pytest.mark.floor
class TestFloorLifecycle:
    """Test complete floor management workflows."""

    async def test_floor_create_list_delete(self, mcp_client, cleanup_tracker):
        """
        Test: Create floor -> List floors -> Delete floor

        Validates basic floor CRUD operations.
        """
        floor_name = generate_unique_name("test_floor")
        logger.info(f"Testing floor lifecycle: {floor_name}")

        # 1. CREATE: Basic floor
        create_result = await mcp_client.call_tool(
            "ha_config_set_floor",
            {
                "name": floor_name,
                "level": 1,
                "icon": "mdi:home-floor-1",
            },
        )

        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), f"Failed to create floor: {create_data}"

        floor_id = create_data.get("floor_id")
        assert floor_id, f"No floor_id returned: {create_data}"
        cleanup_tracker.track("floor", floor_id)
        logger.info(f"Created floor: {floor_name} (ID: {floor_id})")

        # 2. LIST: Verify floor exists in list
        list_result = await mcp_client.call_tool("ha_config_list_floors", {})

        list_data = parse_mcp_result(list_result)
        assert list_data.get("success"), f"Failed to list floors: {list_data}"

        floors = list_data.get("floors", [])
        found_floor = next(
            (f for f in floors if f.get("floor_id") == floor_id),
            None,
        )
        assert found_floor is not None, f"Created floor not found in list: {floor_id}"
        assert found_floor.get("name") == floor_name, (
            f"Floor name mismatch: {found_floor.get('name')}"
        )
        assert found_floor.get("level") == 1, (
            f"Floor level mismatch: {found_floor.get('level')}"
        )
        logger.info(f"Verified floor in list: {found_floor}")

        # 3. DELETE: Remove the floor
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_floor",
            {"floor_id": floor_id},
        )

        delete_data = parse_mcp_result(delete_result)
        assert delete_data.get("success"), f"Failed to delete floor: {delete_data}"
        logger.info(f"Deleted floor: {floor_id}")

        # 4. VERIFY: Floor no longer in list
        verify_result = await mcp_client.call_tool("ha_config_list_floors", {})
        verify_data = parse_mcp_result(verify_result)

        floors_after = verify_data.get("floors", [])
        found_after = next(
            (f for f in floors_after if f.get("floor_id") == floor_id),
            None,
        )
        assert found_after is None, f"Floor still exists after deletion: {floor_id}"
        logger.info("Floor deletion verified")

    async def test_floor_update(self, mcp_client, cleanup_tracker):
        """
        Test: Create floor -> Update floor -> Verify changes

        Validates floor update operations.
        """
        floor_name = generate_unique_name("test_update_floor")
        new_name = generate_unique_name("updated_floor")
        logger.info(f"Testing floor update: {floor_name}")

        # 1. CREATE: Initial floor
        create_result = await mcp_client.call_tool(
            "ha_config_set_floor",
            {
                "name": floor_name,
                "level": 0,
                "icon": "mdi:home-floor-g",
            },
        )

        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), f"Failed to create floor: {create_data}"

        floor_id = create_data.get("floor_id")
        cleanup_tracker.track("floor", floor_id)
        logger.info(f"Created floor: {floor_name} (ID: {floor_id})")

        # 2. UPDATE: Change name, level, and icon
        update_result = await mcp_client.call_tool(
            "ha_config_set_floor",
            {
                "floor_id": floor_id,
                "name": new_name,
                "level": 2,
                "icon": "mdi:home-floor-2",
            },
        )

        update_data = parse_mcp_result(update_result)
        assert update_data.get("success"), f"Failed to update floor: {update_data}"
        logger.info(f"Updated floor: {floor_id}")

        # 3. VERIFY: Check changes in list
        list_result = await mcp_client.call_tool("ha_config_list_floors", {})
        list_data = parse_mcp_result(list_result)

        floors = list_data.get("floors", [])
        found_floor = next(
            (f for f in floors if f.get("floor_id") == floor_id),
            None,
        )

        assert found_floor is not None, f"Updated floor not found: {floor_id}"
        assert found_floor.get("name") == new_name, (
            f"Name not updated: {found_floor.get('name')}"
        )
        assert found_floor.get("level") == 2, (
            f"Level not updated: {found_floor.get('level')}"
        )
        assert found_floor.get("icon") == "mdi:home-floor-2", (
            f"Icon not updated: {found_floor.get('icon')}"
        )
        logger.info(f"Verified updated floor: {found_floor}")

        # 4. CLEANUP
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_floor",
            {"floor_id": floor_id},
        )
        delete_data = parse_mcp_result(delete_result)
        assert delete_data.get("success"), f"Failed to delete floor: {delete_data}"
        logger.info("Floor cleanup completed")

    async def test_floor_with_aliases(self, mcp_client, cleanup_tracker):
        """
        Test: Create floor with aliases -> Verify aliases

        Validates alias functionality for voice assistants.
        """
        floor_name = generate_unique_name("test_alias_floor")
        aliases = ["downstairs", "main level"]
        logger.info(f"Testing floor with aliases: {floor_name}")

        # 1. CREATE: Floor with aliases
        create_result = await mcp_client.call_tool(
            "ha_config_set_floor",
            {
                "name": floor_name,
                "level": 0,
                "aliases": aliases,
                "icon": "mdi:home-floor-g",
            },
        )

        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), f"Failed to create floor: {create_data}"

        floor_id = create_data.get("floor_id")
        cleanup_tracker.track("floor", floor_id)
        logger.info(f"Created floor with aliases: {floor_id}")

        # 2. VERIFY: Check aliases in list
        list_result = await mcp_client.call_tool("ha_config_list_floors", {})
        list_data = parse_mcp_result(list_result)

        floors = list_data.get("floors", [])
        found_floor = next(
            (f for f in floors if f.get("floor_id") == floor_id),
            None,
        )

        assert found_floor is not None, f"Floor not found: {floor_id}"
        floor_aliases = found_floor.get("aliases", [])
        for alias in aliases:
            assert alias in floor_aliases, f"Alias '{alias}' not found in {floor_aliases}"
        logger.info(f"Verified aliases: {floor_aliases}")

        # 3. CLEANUP
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_floor",
            {"floor_id": floor_id},
        )
        delete_data = parse_mcp_result(delete_result)
        assert delete_data.get("success"), f"Failed to delete floor: {delete_data}"
        logger.info("Floor cleanup completed")


@pytest.mark.area
@pytest.mark.floor
class TestAreaFloorIntegration:
    """Test area and floor integration scenarios."""

    async def test_area_with_floor_assignment(self, mcp_client, cleanup_tracker):
        """
        Test: Create floor -> Create area on floor -> Verify assignment -> Update assignment

        Validates area-to-floor relationships.
        """
        floor_name = generate_unique_name("test_int_floor")
        area_name = generate_unique_name("test_int_area")
        logger.info(f"Testing area-floor integration: {floor_name} -> {area_name}")

        # 1. CREATE: Floor first
        floor_result = await mcp_client.call_tool(
            "ha_config_set_floor",
            {
                "name": floor_name,
                "level": 1,
            },
        )

        floor_data = parse_mcp_result(floor_result)
        assert floor_data.get("success"), f"Failed to create floor: {floor_data}"

        floor_id = floor_data.get("floor_id")
        cleanup_tracker.track("floor", floor_id)
        logger.info(f"Created floor: {floor_id}")

        # 2. CREATE: Area assigned to floor
        area_result = await mcp_client.call_tool(
            "ha_config_set_area",
            {
                "name": area_name,
                "floor_id": floor_id,
                "icon": "mdi:bed",
            },
        )

        area_data = parse_mcp_result(area_result)
        assert area_data.get("success"), f"Failed to create area: {area_data}"

        area_id = area_data.get("area_id")
        cleanup_tracker.track("area", area_id)
        logger.info(f"Created area on floor: {area_id}")

        # 3. VERIFY: Check floor assignment in list
        list_result = await mcp_client.call_tool("ha_config_list_areas", {})
        list_data = parse_mcp_result(list_result)

        areas = list_data.get("areas", [])
        found_area = next(
            (a for a in areas if a.get("area_id") == area_id),
            None,
        )

        assert found_area is not None, f"Area not found: {area_id}"
        assert found_area.get("floor_id") == floor_id, (
            f"Floor assignment mismatch: expected {floor_id}, got {found_area.get('floor_id')}"
        )
        logger.info(f"Verified floor assignment: {found_area}")

        # 4. UPDATE: Remove floor assignment
        update_result = await mcp_client.call_tool(
            "ha_config_set_area",
            {
                "area_id": area_id,
                "floor_id": "",  # Empty string to remove assignment
            },
        )

        update_data = parse_mcp_result(update_result)
        assert update_data.get("success"), f"Failed to update area: {update_data}"
        logger.info("Removed floor assignment")

        # 5. VERIFY: Floor assignment removed
        verify_result = await mcp_client.call_tool("ha_config_list_areas", {})
        verify_data = parse_mcp_result(verify_result)

        areas_after = verify_data.get("areas", [])
        found_after = next(
            (a for a in areas_after if a.get("area_id") == area_id),
            None,
        )

        assert found_after is not None, f"Area not found after update: {area_id}"
        assert found_after.get("floor_id") is None, (
            f"Floor assignment not removed: {found_after.get('floor_id')}"
        )
        logger.info("Verified floor assignment removed")

        # 6. CLEANUP: Delete area first, then floor
        await mcp_client.call_tool("ha_config_remove_area", {"area_id": area_id})
        await mcp_client.call_tool("ha_config_remove_floor", {"floor_id": floor_id})
        logger.info("Cleanup completed")

    async def test_home_topology_with_assignment(self, mcp_client, cleanup_tracker):
        """
        Test: Create floor -> Create area on floor -> ha_list_floors_areas ->
              Verify area appears nested under floor, not in unassigned_areas.
        """
        floor_name = generate_unique_name("test_topo_floor")
        area_name = generate_unique_name("test_topo_area")
        logger.info(f"Testing topology with assignment: {floor_name} -> {area_name}")

        floor_result = await mcp_client.call_tool(
            "ha_config_set_floor", {"name": floor_name, "level": 2}
        )
        floor_data = parse_mcp_result(floor_result)
        assert floor_data.get("success"), f"Failed to create floor: {floor_data}"
        floor_id = floor_data.get("floor_id")
        cleanup_tracker.track("floor", floor_id)

        area_result = await mcp_client.call_tool(
            "ha_config_set_area",
            {"name": area_name, "floor_id": floor_id, "icon": "mdi:sofa"},
        )
        area_data = parse_mcp_result(area_result)
        assert area_data.get("success"), f"Failed to create area: {area_data}"
        area_id = area_data.get("area_id")
        cleanup_tracker.track("area", area_id)

        topo_result = await mcp_client.call_tool("ha_list_floors_areas", {})
        topo_data = parse_mcp_result(topo_result)

        assert topo_data.get("success"), f"Topology call failed: {topo_data}"
        assert "floors" in topo_data and "unassigned_areas" in topo_data

        target_floor = next(
            (f for f in topo_data["floors"] if f.get("floor_id") == floor_id), None
        )
        assert target_floor is not None, f"Created floor not in topology: {floor_id}"
        assert "areas" in target_floor, "Floor entry missing 'areas' key"

        nested_area_ids = [a.get("area_id") for a in target_floor["areas"]]
        assert area_id in nested_area_ids, (
            f"Area {area_id} not nested under floor {floor_id}: {nested_area_ids}"
        )

        unassigned_ids = [a.get("area_id") for a in topo_data["unassigned_areas"]]
        assert area_id not in unassigned_ids, (
            f"Assigned area leaked into unassigned_areas: {area_id}"
        )
        logger.info(f"Topology verified: {area_id} nested under {floor_id}")

        await mcp_client.call_tool("ha_config_remove_area", {"area_id": area_id})
        await mcp_client.call_tool("ha_config_remove_floor", {"floor_id": floor_id})

    async def test_home_topology_unassigned_area(self, mcp_client, cleanup_tracker):
        """
        Test: Create area without floor_id -> ha_list_floors_areas ->
              Verify area appears in unassigned_areas, not nested under any floor.
        """
        area_name = generate_unique_name("test_topo_unassigned")
        logger.info(f"Testing topology with unassigned area: {area_name}")

        area_result = await mcp_client.call_tool(
            "ha_config_set_area", {"name": area_name, "icon": "mdi:garage"}
        )
        area_data = parse_mcp_result(area_result)
        assert area_data.get("success"), f"Failed to create area: {area_data}"
        area_id = area_data.get("area_id")
        cleanup_tracker.track("area", area_id)

        topo_result = await mcp_client.call_tool("ha_list_floors_areas", {})
        topo_data = parse_mcp_result(topo_result)

        assert topo_data.get("success"), f"Topology call failed: {topo_data}"

        unassigned_ids = [a.get("area_id") for a in topo_data["unassigned_areas"]]
        assert area_id in unassigned_ids, (
            f"Unassigned area {area_id} not in unassigned_areas: {unassigned_ids}"
        )

        for floor in topo_data["floors"]:
            floor_area_ids = [a.get("area_id") for a in floor.get("areas", [])]
            assert area_id not in floor_area_ids, (
                f"Unassigned area leaked into floor {floor.get('floor_id')}: {area_id}"
            )
        logger.info(f"Verified {area_id} is in unassigned_areas only")

        await mcp_client.call_tool("ha_config_remove_area", {"area_id": area_id})

    async def test_home_topology_empty_floor(self, mcp_client, cleanup_tracker):
        """
        Test: Create floor with no areas -> ha_list_floors_areas ->
              Verify floor appears in floors with "areas": [], not filtered out
              and not crashing a downstream iterator.
        """
        floor_name = generate_unique_name("test_topo_empty")
        logger.info(f"Testing topology with empty floor: {floor_name}")

        floor_result = await mcp_client.call_tool(
            "ha_config_set_floor",
            {"name": floor_name, "level": 3, "icon": "mdi:home-floor-3"},
        )
        floor_data = parse_mcp_result(floor_result)
        assert floor_data.get("success"), f"Failed to create floor: {floor_data}"
        floor_id = floor_data.get("floor_id")
        cleanup_tracker.track("floor", floor_id)

        topo_result = await mcp_client.call_tool("ha_list_floors_areas", {})
        topo_data = parse_mcp_result(topo_result)

        assert topo_data.get("success"), f"Topology call failed: {topo_data}"

        floor_ids = [f.get("floor_id") for f in topo_data["floors"]]
        assert floor_id in floor_ids, (
            f"Empty floor {floor_id} not found in floors list: {floor_ids}"
        )

        empty_floor = next(
            (f for f in topo_data["floors"] if f.get("floor_id") == floor_id),
            None,
        )
        assert empty_floor is not None, f"Empty floor entry missing: {floor_id}"
        assert "areas" in empty_floor, (
            f"Empty floor has no 'areas' key — downstream iterators would crash: {empty_floor}"
        )
        assert empty_floor["areas"] == [], (
            f"Empty floor 'areas' should be [], got: {empty_floor['areas']}"
        )

        # Downstream-iterator smoke: must not raise
        for _area in empty_floor["areas"]:
            pass

        logger.info(f"Verified empty floor {floor_id} has areas=[]")

        await mcp_client.call_tool("ha_config_remove_floor", {"floor_id": floor_id})

    async def test_home_topology_sort_order(self, mcp_client, cleanup_tracker):
        """
        Test: Create floors with level=[-1, 0, None, 2] -> ha_list_floors_areas ->
              Verify sort order: -1 first, 2 last, 0 and None adjacent
              (both map to sort key 0 via `level or 0`; stable sort preserves
              insertion order within the tie).
        """
        prefix = generate_unique_name("test_topo_sort")
        logger.info(f"Testing topology sort order with levels [-1, 0, None, 2]: {prefix}")

        levels = [-1, 0, None, 2]
        floor_ids_by_level: dict[str, Any] = {}
        for lvl in levels:
            payload: dict[str, Any] = {"name": f"{prefix}_lvl_{lvl}"}
            if lvl is not None:
                payload["level"] = lvl
            floor_result = await mcp_client.call_tool("ha_config_set_floor", payload)
            floor_data = parse_mcp_result(floor_result)
            assert floor_data.get("success"), (
                f"Failed to create floor level={lvl}: {floor_data}"
            )
            fid = floor_data.get("floor_id")
            cleanup_tracker.track("floor", fid)
            floor_ids_by_level[str(lvl)] = fid

        topo_result = await mcp_client.call_tool("ha_list_floors_areas", {})
        topo_data = parse_mcp_result(topo_result)
        assert topo_data.get("success"), f"Topology call failed: {topo_data}"

        # Extract only our floors in the order returned by the tool
        our_ids = set(floor_ids_by_level.values())
        returned_order = [
            f for f in topo_data["floors"] if f.get("floor_id") in our_ids
        ]
        assert len(returned_order) == 4, (
            f"Expected 4 floors, got {len(returned_order)}: {returned_order}"
        )

        # Sort key in the implementation is `level or 0`, so:
        #   -1 -> -1, 0 -> 0, None -> 0, 2 -> 2
        # Position 0 must be level=-1, position 3 must be level=2.
        # Positions 1 and 2 are the 0/None tie — either order is valid.
        assert returned_order[0].get("floor_id") == floor_ids_by_level["-1"], (
            f"First floor should be level=-1, got: {returned_order[0]}"
        )
        assert returned_order[3].get("floor_id") == floor_ids_by_level["2"], (
            f"Last floor should be level=2, got: {returned_order[3]}"
        )

        middle_ids = {returned_order[1].get("floor_id"), returned_order[2].get("floor_id")}
        expected_middle = {floor_ids_by_level["0"], floor_ids_by_level["None"]}
        assert middle_ids == expected_middle, (
            f"Middle positions should contain level=0 and level=None floors, "
            f"got: {middle_ids} vs expected {expected_middle}"
        )

        logger.info(
            "Verified sort order: -1 first, 2 last, 0/None tied in middle"
        )

        for fid in floor_ids_by_level.values():
            await mcp_client.call_tool("ha_config_remove_floor", {"floor_id": fid})

    @pytest.mark.slow
    async def test_multiple_areas_on_floor(self, mcp_client, cleanup_tracker):
        """
        Test: Create floor -> Create multiple areas on floor -> List and verify

        Validates multi-area floor scenarios.
        """
        floor_name = generate_unique_name("test_multi_floor")
        logger.info(f"Testing multiple areas on floor: {floor_name}")

        # 1. CREATE: Floor
        floor_result = await mcp_client.call_tool(
            "ha_config_set_floor",
            {
                "name": floor_name,
                "level": 0,
            },
        )

        floor_data = parse_mcp_result(floor_result)
        assert floor_data.get("success"), f"Failed to create floor: {floor_data}"

        floor_id = floor_data.get("floor_id")
        cleanup_tracker.track("floor", floor_id)
        logger.info(f"Created floor: {floor_id}")

        # 2. CREATE: Multiple areas on the floor
        area_names = [
            generate_unique_name("room_a"),
            generate_unique_name("room_b"),
            generate_unique_name("room_c"),
        ]
        area_ids = []

        for name in area_names:
            area_result = await mcp_client.call_tool(
                "ha_config_set_area",
                {
                    "name": name,
                    "floor_id": floor_id,
                },
            )

            area_data = parse_mcp_result(area_result)
            assert area_data.get("success"), f"Failed to create area {name}: {area_data}"

            area_id = area_data.get("area_id")
            area_ids.append(area_id)
            cleanup_tracker.track("area", area_id)
            logger.info(f"Created area: {name} (ID: {area_id})")


        # 3. VERIFY: All areas on floor
        list_result = await mcp_client.call_tool("ha_config_list_areas", {})
        list_data = parse_mcp_result(list_result)

        areas = list_data.get("areas", [])
        floor_areas = [a for a in areas if a.get("floor_id") == floor_id]

        assert len(floor_areas) >= len(area_ids), (
            f"Not all areas found on floor: expected {len(area_ids)}, got {len(floor_areas)}"
        )
        logger.info(f"Verified {len(floor_areas)} areas on floor")

        # 4. CLEANUP: Delete areas first, then floor
        for area_id in area_ids:
            delete_result = await mcp_client.call_tool("ha_config_remove_area", {"area_id": area_id})
            delete_data = parse_mcp_result(delete_result)
            if not delete_data.get("success"):
                logger.error(f"Failed to delete area {area_id}: {delete_data}")
            else:
                logger.info(f"Deleted area: {area_id}")

        floor_delete_result = await mcp_client.call_tool("ha_config_remove_floor", {"floor_id": floor_id})
        floor_delete_data = parse_mcp_result(floor_delete_result)
        if not floor_delete_data.get("success"):
            logger.error(f"Failed to delete floor {floor_id}: {floor_delete_data}")
        else:
            logger.info(f"Deleted floor: {floor_id}")
        logger.info("Cleanup completed")


@pytest.mark.area
async def test_area_list_empty_or_populated(mcp_client):
    """
    Test: List areas works correctly (empty or with existing areas)

    Basic validation that the list endpoint works.
    """
    logger.info("Testing ha_list_areas functionality")

    list_result = await mcp_client.call_tool("ha_config_list_areas", {})
    list_data = parse_mcp_result(list_result)

    assert list_data.get("success"), f"Failed to list areas: {list_data}"
    assert "count" in list_data, f"Missing count in response: {list_data}"
    assert "areas" in list_data, f"Missing areas in response: {list_data}"
    assert isinstance(list_data["areas"], list), (
        f"Areas should be a list: {type(list_data['areas'])}"
    )

    logger.info(f"Found {list_data['count']} existing area(s)")


@pytest.mark.floor
async def test_floor_list_empty_or_populated(mcp_client):
    """
    Test: List floors works correctly (empty or with existing floors)

    Basic validation that the list endpoint works.
    """
    logger.info("Testing ha_list_floors functionality")

    list_result = await mcp_client.call_tool("ha_config_list_floors", {})
    list_data = parse_mcp_result(list_result)

    assert list_data.get("success"), f"Failed to list floors: {list_data}"
    assert "count" in list_data, f"Missing count in response: {list_data}"
    assert "floors" in list_data, f"Missing floors in response: {list_data}"
    assert isinstance(list_data["floors"], list), (
        f"Floors should be a list: {type(list_data['floors'])}"
    )

    logger.info(f"Found {list_data['count']} existing floor(s)")


@pytest.mark.area
@pytest.mark.floor
async def test_home_topology_schema(mcp_client):
    """
    Test: ha_list_floors_areas returns a well-formed response on any HA instance
    (populated or empty). Validates schema only, not content.
    """
    logger.info("Testing ha_list_floors_areas schema")

    topo_result = await mcp_client.call_tool("ha_list_floors_areas", {})
    topo_data = parse_mcp_result(topo_result)

    assert topo_data.get("success"), f"Topology call failed: {topo_data}"
    assert "floor_count" in topo_data, f"Missing floor_count: {topo_data}"
    assert "area_count" in topo_data, f"Missing area_count: {topo_data}"
    assert "unassigned_count" in topo_data, f"Missing unassigned_count: {topo_data}"
    assert "floors" in topo_data, f"Missing floors: {topo_data}"
    assert "unassigned_areas" in topo_data, f"Missing unassigned_areas: {topo_data}"
    assert isinstance(topo_data["floors"], list), (
        f"floors should be list: {type(topo_data['floors'])}"
    )
    assert isinstance(topo_data["unassigned_areas"], list), (
        f"unassigned_areas should be list: {type(topo_data['unassigned_areas'])}"
    )
    assert isinstance(topo_data["floor_count"], int)
    assert isinstance(topo_data["area_count"], int)
    assert isinstance(topo_data["unassigned_count"], int)
    assert "orphaned_count" in topo_data, f"Missing orphaned_count: {topo_data}"
    assert "orphaned_areas" in topo_data, f"Missing orphaned_areas: {topo_data}"
    assert isinstance(topo_data["orphaned_areas"], list), (
        f"orphaned_areas should be list: {type(topo_data['orphaned_areas'])}"
    )
    assert isinstance(topo_data["orphaned_count"], int)
    # Each floor entry must carry its floor-registry fields plus the areas list
    for floor in topo_data["floors"]:
        assert "floor_id" in floor, f"Floor entry missing floor_id: {floor}"
        assert "areas" in floor, f"Floor entry missing areas key: {floor}"
        assert isinstance(floor["areas"], list)

    logger.info(
        f"Schema ok: {topo_data['floor_count']} floor(s), "
        f"{topo_data['area_count']} area(s), "
        f"{topo_data['unassigned_count']} unassigned, "
        f"{topo_data['orphaned_count']} orphaned"
    )


@pytest.mark.asyncio
class TestAreaFloorDestructiveNegativeInputs:
    """
    A7 negative-input tests for ha_config_remove_area and ha_config_remove_floor.

    Covers the nonexistent-identifier failure path, which is not exercised by
    the existing lifecycle tests (which only call remove on identifiers they
    just created).

    Methodology: source-verified against tools_areas.py. Both tools call the
    respective registry delete WebSocket (config/area_registry/delete,
    config/floor_registry/delete). When the registry returns a failure result,
    raise_tool_error is invoked with ErrorCode.SERVICE_CALL_FAILED. Live-probed
    response shape against a fresh HA testcontainer before test authoring.
    """

    async def test_remove_area_nonexistent(self, mcp_client):
        """
        Test: ha_config_remove_area with a nonexistent area_id returns a
        structured error, not success=True.

        Source path: WebSocket result.success=False →
        raise_tool_error(SERVICE_CALL_FAILED, "Failed to delete area: ...").
        """
        from ...utilities.assertions import safe_call_tool

        data = await safe_call_tool(
            mcp_client,
            "ha_config_remove_area",
            {"area_id": "nonexistent_area_a7_e2e_xyz_404"},
        )

        assert not data.get("success"), (
            f"Expected failure for nonexistent area, got success=True: {data}"
        )
        assert data["error"]["code"] == "SERVICE_CALL_FAILED", (
            f"Expected error code SERVICE_CALL_FAILED, got: {data.get('error')}"
        )
        error_msg = str(data.get("error", "")).lower()
        assert "doesn't exist" in error_msg or "not found" in error_msg, (
            f"Expected 'doesn't exist'/'not found' in error message, got: {data.get('error')}"
        )

    async def test_remove_floor_nonexistent(self, mcp_client):
        """
        Test: ha_config_remove_floor with a nonexistent floor_id returns a
        structured error, not success=True.

        Source path: WebSocket result.success=False →
        raise_tool_error(SERVICE_CALL_FAILED, "Failed to delete floor: ...").
        """
        from ...utilities.assertions import safe_call_tool

        data = await safe_call_tool(
            mcp_client,
            "ha_config_remove_floor",
            {"floor_id": "nonexistent_floor_a7_e2e_xyz_404"},
        )

        assert not data.get("success"), (
            f"Expected failure for nonexistent floor, got success=True: {data}"
        )
        assert data["error"]["code"] == "SERVICE_CALL_FAILED", (
            f"Expected error code SERVICE_CALL_FAILED, got: {data.get('error')}"
        )
        error_msg = str(data.get("error", "")).lower()
        assert "doesn't exist" in error_msg or "not found" in error_msg, (
            f"Expected 'doesn't exist'/'not found' in error message, got: {data.get('error')}"
        )
