"""
Edge Case Tests for Consolidated Area/Floor Tools

Tests behavior when cross-type parameters are provided, ensuring
the consolidated ha_config_set_area/ha_config_remove_area tools
handle type-specific params correctly.
"""

import logging
import uuid

import pytest

from ...utilities.assertions import parse_mcp_result, safe_call_tool

logger = logging.getLogger(__name__)


def generate_unique_name(prefix: str) -> str:
    """Generate a unique name for test entities to avoid conflicts."""
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@pytest.mark.area
@pytest.mark.floor
class TestConsolidatedAreaFloorEdgeCases:
    """Test edge cases in consolidated area/floor tools."""

    async def test_floor_create_ignores_area_only_params(
        self, mcp_client, cleanup_tracker
    ):
        """
        Test: Creating a floor with area-only params (picture) silently ignores them.

        The `picture` param only applies when type='area'. When type='floor',
        it should be ignored without error.
        """
        floor_name = generate_unique_name("test_edge_floor")
        logger.info(f"Testing floor create with area-only params: {floor_name}")

        create_result = await mcp_client.call_tool(
            "ha_config_set_area",
            {
                "type": "floor",
                "name": floor_name,
                "level": 1,
                "picture": "http://example.com/image.png",  # area-only param
            },
        )

        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), f"Floor create should succeed: {create_data}"

        floor_id = create_data.get("floor_id")
        assert floor_id, f"No floor_id returned: {create_data}"
        cleanup_tracker.track("floor", floor_id)
        logger.info(f"Floor created successfully despite area-only params: {floor_id}")

        # Cleanup
        await mcp_client.call_tool(
            "ha_config_remove_area",
            {"type": "floor", "floor_id": floor_id},
        )

    async def test_area_create_ignores_floor_only_params(
        self, mcp_client, cleanup_tracker
    ):
        """
        Test: Creating an area with floor-only param (level) silently ignores it.

        The `level` param only applies when type='floor'. When type='area',
        it should be ignored without error.
        """
        area_name = generate_unique_name("test_edge_area")
        logger.info(f"Testing area create with floor-only params: {area_name}")

        create_result = await mcp_client.call_tool(
            "ha_config_set_area",
            {
                "name": area_name,
                "level": 5,  # floor-only param
                "icon": "mdi:sofa",
            },
        )

        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), f"Area create should succeed: {create_data}"

        area_id = create_data.get("area_id")
        assert area_id, f"No area_id returned: {create_data}"
        cleanup_tracker.track("area", area_id)
        logger.info(f"Area created successfully despite floor-only params: {area_id}")

        # Cleanup
        await mcp_client.call_tool(
            "ha_config_remove_area",
            {"area_id": area_id},
        )

    async def test_remove_floor_without_floor_id_fails(self, mcp_client):
        """
        Test: Removing a floor without providing floor_id returns a validation error.
        """
        logger.info("Testing remove floor without floor_id")

        result = await safe_call_tool(
            mcp_client,
            "ha_config_remove_area",
            {"type": "floor"},
        )

        # Should fail with validation error
        assert not result.get("success"), (
            f"Should have failed without floor_id: {result}"
        )
        logger.info(f"Correctly failed: {result}")

    async def test_remove_area_without_area_id_fails(self, mcp_client):
        """
        Test: Removing an area without providing area_id returns a validation error.
        """
        logger.info("Testing remove area without area_id")

        result = await safe_call_tool(
            mcp_client,
            "ha_config_remove_area",
            {},
        )

        # Should fail with validation error
        assert not result.get("success"), (
            f"Should have failed without area_id: {result}"
        )
        logger.info(f"Correctly failed: {result}")

    async def test_create_floor_without_name_fails(self, mcp_client):
        """
        Test: Creating a floor without a name returns a validation error.
        """
        logger.info("Testing floor create without name")

        result = await safe_call_tool(
            mcp_client,
            "ha_config_set_area",
            {"type": "floor", "level": 1},
        )

        assert not result.get("success"), f"Should have failed without name: {result}"
        logger.info(f"Correctly failed: {result}")

    async def test_create_area_without_name_fails(self, mcp_client):
        """
        Test: Creating an area without a name returns a validation error.
        """
        logger.info("Testing area create without name")

        result = await safe_call_tool(
            mcp_client,
            "ha_config_set_area",
            {"icon": "mdi:sofa"},
        )

        assert not result.get("success"), f"Should have failed without name: {result}"
        logger.info(f"Correctly failed: {result}")

    async def test_floor_create_without_level(self, mcp_client, cleanup_tracker):
        """
        Test: Creating a floor without specifying level uses HA's default.
        """
        floor_name = generate_unique_name("test_no_level")
        logger.info(f"Testing floor create without level: {floor_name}")

        create_result = await mcp_client.call_tool(
            "ha_config_set_area",
            {
                "type": "floor",
                "name": floor_name,
            },
        )

        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), f"Floor create should succeed: {create_data}"

        floor_id = create_data.get("floor_id")
        assert floor_id, f"No floor_id returned: {create_data}"
        cleanup_tracker.track("floor", floor_id)
        logger.info(f"Floor created without level: {floor_id}")

        # Cleanup
        await mcp_client.call_tool(
            "ha_config_remove_area",
            {"type": "floor", "floor_id": floor_id},
        )
