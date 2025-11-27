"""
Area and Floor Management E2E Tests

Comprehensive tests for Home Assistant area and floor management via MCP tools.

This test suite validates:
- Area creation, listing, updating, and deletion
- Floor creation, listing, updating, and deletion
- Area-to-floor assignments
- Aliases and icon management
"""

import asyncio
import logging
import uuid

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
        await asyncio.sleep(REGISTRY_OPERATION_DELAY)  # Allow time for registration
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
        await asyncio.sleep(REGISTRY_OPERATION_DELAY)
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
        await asyncio.sleep(REGISTRY_OPERATION_DELAY)
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
        await asyncio.sleep(REGISTRY_OPERATION_DELAY)
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
        await asyncio.sleep(REGISTRY_OPERATION_DELAY)
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
        await asyncio.sleep(REGISTRY_OPERATION_DELAY)
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
        await asyncio.sleep(REGISTRY_OPERATION_DELAY)
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
        await asyncio.sleep(REGISTRY_OPERATION_DELAY)
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
        await asyncio.sleep(REGISTRY_OPERATION_DELAY)
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
        await asyncio.sleep(REGISTRY_OPERATION_DELAY)
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

            await asyncio.sleep(BATCH_OPERATION_DELAY)

        # 3. VERIFY: All areas on floor
        await asyncio.sleep(REGISTRY_OPERATION_DELAY)
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
            await asyncio.sleep(BATCH_OPERATION_DELAY)

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
