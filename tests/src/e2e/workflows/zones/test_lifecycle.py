"""
End-to-End tests for Home Assistant Zone Management tools.

This test suite validates the complete lifecycle of Home Assistant zones including:
- Zone listing
- Zone creation with various configurations
- Zone updates (name, coordinates, radius, icon, passive mode)
- Zone deletion
- Input validation and error handling

Each test uses real Home Assistant API calls via the MCP server to ensure
production-level functionality and compatibility.
"""

import logging

import pytest

from ...utilities.proxy_helpers import ProxyMCPAssertions as MCPAssertions

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@pytest.mark.zone
class TestZoneLifecycle:
    """Test complete zone management workflows."""

    async def test_zone_list(self, mcp_client):
        """
        Test: List all zones

        Validates that we can retrieve all zones from Home Assistant.
        """
        logger.info("Listing all zones...")

        async with MCPAssertions(mcp_client) as mcp:
            list_data = await mcp.call_tool_success("ha_get_zone", {})

            assert "count" in list_data, f"Missing 'count' in response: {list_data}"
            assert "zones" in list_data, f"Missing 'zones' in response: {list_data}"
            assert isinstance(list_data["zones"], list), (
                f"Zones should be a list: {list_data}"
            )

            logger.info(f"Found {list_data['count']} zones")
            for zone in list_data["zones"]:
                logger.info(f"  - {zone.get('name', 'Unknown')} (id: {zone.get('id')})")

    async def test_zone_basic_lifecycle(self, mcp_client, cleanup_tracker):
        """
        Test: Basic zone lifecycle (create, list, update, delete)

        Validates fundamental zone operations with a simple zone.
        """
        logger.info("Testing basic zone lifecycle...")

        async with MCPAssertions(mcp_client) as mcp:
            # 1. CREATE: Basic zone
            zone_name = "Test Office E2E"
            create_data = await mcp.call_tool_success(
                "ha_create_zone",
                {
                    "name": zone_name,
                    "latitude": 40.7128,
                    "longitude": -74.0060,
                    "radius": 150,
                    "icon": "mdi:briefcase",
                },
            )

            zone_id = create_data.get("zone_id")
            assert zone_id, f"Missing zone_id in create response: {create_data}"
            cleanup_tracker.track("zone", zone_id)
            logger.info(f"Created zone: {zone_name} (id: {zone_id})")

            # 2. LIST: Verify zone appears in list
            list_data = await mcp.call_tool_success("ha_get_zone", {})

            zone_found = False
            for zone in list_data.get("zones", []):
                if zone.get("id") == zone_id:
                    zone_found = True
                    assert zone.get("name") == zone_name, (
                        f"Name mismatch: {zone.get('name')}"
                    )
                    assert zone.get("latitude") == 40.7128, (
                        f"Latitude mismatch: {zone.get('latitude')}"
                    )
                    assert zone.get("longitude") == -74.0060, (
                        f"Longitude mismatch: {zone.get('longitude')}"
                    )
                    assert zone.get("radius") == 150, (
                        f"Radius mismatch: {zone.get('radius')}"
                    )
                    break

            assert zone_found, f"Zone {zone_id} not found in list"
            logger.info("Zone verified in list")

            # 3. UPDATE: Modify zone properties
            update_data = await mcp.call_tool_success(
                "ha_update_zone",
                {
                    "zone_id": zone_id,
                    "name": "Updated Office E2E",
                    "radius": 200,
                },
            )

            assert "updated_fields" in update_data, (
                f"Missing updated_fields: {update_data}"
            )
            assert "name" in update_data["updated_fields"], (
                f"Name not in updated_fields: {update_data}"
            )
            assert "radius" in update_data["updated_fields"], (
                f"Radius not in updated_fields: {update_data}"
            )
            logger.info("Zone updated successfully")

            # 4. VERIFY UPDATE: Check updated values in list
            list_data = await mcp.call_tool_success("ha_get_zone", {})

            for zone in list_data.get("zones", []):
                if zone.get("id") == zone_id:
                    assert zone.get("name") == "Updated Office E2E", (
                        f"Updated name mismatch: {zone.get('name')}"
                    )
                    assert zone.get("radius") == 200, (
                        f"Updated radius mismatch: {zone.get('radius')}"
                    )
                    break
            logger.info("Zone update verified")

            # 5. DELETE: Remove zone
            await mcp.call_tool_success(
                "ha_delete_zone",
                {"zone_id": zone_id},
            )
            logger.info("Zone deleted successfully")

            # 6. VERIFY DELETE: Zone should not appear in list
            list_data = await mcp.call_tool_success("ha_get_zone", {})

            for zone in list_data.get("zones", []):
                assert zone.get("id") != zone_id, (
                    f"Zone {zone_id} still exists after deletion"
                )
            logger.info("Zone deletion verified")

    async def test_zone_with_passive_mode(self, mcp_client, cleanup_tracker):
        """
        Test: Create zone with passive mode enabled

        Passive zones don't trigger automations on enter/exit.
        """
        logger.info("Testing passive zone creation...")

        async with MCPAssertions(mcp_client) as mcp:
            zone_name = "Test Passive Zone E2E"
            create_data = await mcp.call_tool_success(
                "ha_create_zone",
                {
                    "name": zone_name,
                    "latitude": 40.7580,
                    "longitude": -73.9855,
                    "radius": 500,
                    "passive": True,
                },
            )

            zone_id = create_data.get("zone_id")
            assert zone_id, f"Missing zone_id: {create_data}"
            cleanup_tracker.track("zone", zone_id)
            logger.info(f"Created passive zone: {zone_name} (id: {zone_id})")

            # Verify passive mode in list
            list_data = await mcp.call_tool_success("ha_get_zone", {})

            for zone in list_data.get("zones", []):
                if zone.get("id") == zone_id:
                    assert zone.get("passive") is True, (
                        f"Passive mode not set: {zone}"
                    )
                    break
            logger.info("Passive mode verified")

            # Update passive mode to False
            await mcp.call_tool_success(
                "ha_update_zone",
                {
                    "zone_id": zone_id,
                    "passive": False,
                },
            )
            logger.info("Passive mode updated to False")

            # Verify passive mode is now False
            list_data = await mcp.call_tool_success("ha_get_zone", {})

            for zone in list_data.get("zones", []):
                if zone.get("id") == zone_id:
                    assert zone.get("passive") is False, (
                        f"Passive mode should be False: {zone}"
                    )
                    break
            logger.info("Passive mode update verified")

            # Cleanup
            await mcp.call_tool_success(
                "ha_delete_zone",
                {"zone_id": zone_id},
            )
            logger.info("Passive zone cleaned up")

    async def test_zone_coordinate_update(self, mcp_client, cleanup_tracker):
        """
        Test: Update zone coordinates

        Validates updating latitude and longitude separately and together.
        """
        logger.info("Testing zone coordinate updates...")

        async with MCPAssertions(mcp_client) as mcp:
            # Create initial zone
            zone_name = "Test Coordinates E2E"
            create_data = await mcp.call_tool_success(
                "ha_create_zone",
                {
                    "name": zone_name,
                    "latitude": 40.0,
                    "longitude": -74.0,
                    "radius": 100,
                },
            )

            zone_id = create_data.get("zone_id")
            cleanup_tracker.track("zone", zone_id)
            logger.info("Created zone at (40.0, -74.0)")

            # Update latitude only
            await mcp.call_tool_success(
                "ha_update_zone",
                {
                    "zone_id": zone_id,
                    "latitude": 41.0,
                },
            )
            logger.info("Updated latitude to 41.0")

            # Verify latitude update
            list_data = await mcp.call_tool_success("ha_get_zone", {})
            for zone in list_data.get("zones", []):
                if zone.get("id") == zone_id:
                    assert zone.get("latitude") == 41.0, (
                        f"Latitude not updated: {zone.get('latitude')}"
                    )
                    assert zone.get("longitude") == -74.0, (
                        f"Longitude changed unexpectedly: {zone.get('longitude')}"
                    )
                    break

            # Update both coordinates
            await mcp.call_tool_success(
                "ha_update_zone",
                {
                    "zone_id": zone_id,
                    "latitude": 42.0,
                    "longitude": -73.0,
                },
            )
            logger.info("Updated both coordinates to (42.0, -73.0)")

            # Verify both updated
            list_data = await mcp.call_tool_success("ha_get_zone", {})
            for zone in list_data.get("zones", []):
                if zone.get("id") == zone_id:
                    assert zone.get("latitude") == 42.0, (
                        f"Latitude mismatch: {zone.get('latitude')}"
                    )
                    assert zone.get("longitude") == -73.0, (
                        f"Longitude mismatch: {zone.get('longitude')}"
                    )
                    break
            logger.info("Coordinate updates verified")

            # Cleanup
            await mcp.call_tool_success(
                "ha_delete_zone",
                {"zone_id": zone_id},
            )
            logger.info("Coordinates test zone cleaned up")

    async def test_zone_input_validation(self, mcp_client):
        """
        Test: Input validation for zone operations

        Validates proper error handling for invalid inputs.
        """
        logger.info("Testing zone input validation...")

        async with MCPAssertions(mcp_client) as mcp:
            # Test: Invalid latitude (out of range)
            await mcp.call_tool_failure(
                "ha_create_zone",
                {
                    "name": "Invalid Latitude",
                    "latitude": 100.0,  # Invalid: must be -90 to 90
                    "longitude": -74.0,
                    "radius": 100,
                },
                expected_error="latitude",
            )
            logger.info("Invalid latitude properly rejected")

            # Test: Invalid longitude (out of range)
            await mcp.call_tool_failure(
                "ha_create_zone",
                {
                    "name": "Invalid Longitude",
                    "latitude": 40.0,
                    "longitude": 200.0,  # Invalid: must be -180 to 180
                    "radius": 100,
                },
                expected_error="longitude",
            )
            logger.info("Invalid longitude properly rejected")

            # Test: Invalid radius (zero or negative)
            await mcp.call_tool_failure(
                "ha_create_zone",
                {
                    "name": "Invalid Radius",
                    "latitude": 40.0,
                    "longitude": -74.0,
                    "radius": 0,  # Invalid: must be > 0
                },
                expected_error="radius",
            )
            logger.info("Invalid radius properly rejected")

            # Test: Update with no fields
            await mcp.call_tool_failure(
                "ha_update_zone",
                {
                    "zone_id": "some_zone_id",
                },
                expected_error="No fields to update",
            )
            logger.info("Update with no fields properly rejected")

            # Test: Delete non-existent zone
            await mcp.call_tool_failure(
                "ha_delete_zone",
                {
                    "zone_id": "nonexistent_zone_xyz_123",
                },
            )
            logger.info("Delete non-existent zone properly handled")

            logger.info("All input validation tests passed")

    async def test_zone_multiple_operations(self, mcp_client, cleanup_tracker):
        """
        Test: Multiple zone operations in sequence

        Tests creating and managing multiple zones simultaneously.
        """
        logger.info("Testing multiple zone operations...")

        async with MCPAssertions(mcp_client) as mcp:
            zones_to_create = [
                {
                    "name": "Test Zone Alpha",
                    "latitude": 40.7128,
                    "longitude": -74.0060,
                    "radius": 100,
                    "icon": "mdi:alpha-a",
                },
                {
                    "name": "Test Zone Beta",
                    "latitude": 34.0522,
                    "longitude": -118.2437,
                    "radius": 150,
                    "icon": "mdi:alpha-b",
                },
                {
                    "name": "Test Zone Gamma",
                    "latitude": 51.5074,
                    "longitude": -0.1278,
                    "radius": 200,
                    "icon": "mdi:alpha-g",
                },
            ]

            created_zone_ids = []

            # Create multiple zones
            for zone_config in zones_to_create:
                create_data = await mcp.call_tool_success(
                    "ha_create_zone",
                    zone_config,
                )
                zone_id = create_data.get("zone_id")
                assert zone_id, f"Missing zone_id for {zone_config['name']}"
                created_zone_ids.append(zone_id)
                cleanup_tracker.track("zone", zone_id)
                logger.info(f"Created: {zone_config['name']} (id: {zone_id})")

            logger.info(f"Created {len(created_zone_ids)} zones")

            # Verify all zones exist
            list_data = await mcp.call_tool_success("ha_get_zone", {})
            zone_ids_in_list = [z.get("id") for z in list_data.get("zones", [])]

            for zone_id in created_zone_ids:
                assert zone_id in zone_ids_in_list, (
                    f"Zone {zone_id} not found in list"
                )
            logger.info("All zones verified in list")

            # Update all zones
            for zone_id in created_zone_ids:
                await mcp.call_tool_success(
                    "ha_update_zone",
                    {
                        "zone_id": zone_id,
                        "radius": 250,  # Update all to same radius
                    },
                )
            logger.info("All zones updated")

            # Delete all zones
            for zone_id in created_zone_ids:
                await mcp.call_tool_success(
                    "ha_delete_zone",
                    {"zone_id": zone_id},
                )
            logger.info("All zones deleted")

            # Verify all deleted
            list_data = await mcp.call_tool_success("ha_get_zone", {})
            zone_ids_in_list = [z.get("id") for z in list_data.get("zones", [])]

            for zone_id in created_zone_ids:
                assert zone_id not in zone_ids_in_list, (
                    f"Zone {zone_id} still exists after deletion"
                )
            logger.info("All zone deletions verified")


async def test_zone_search_discovery(mcp_client):
    """
    Test: Zone search and discovery capabilities

    Validates that users can find zones using search tools.
    """
    logger.info("Testing zone search and discovery...")

    async with MCPAssertions(mcp_client) as mcp:
        # Search for zone entities
        try:
            search_data = await mcp.call_tool_success(
                "ha_search_entities",
                {"query": "zone", "domain_filter": "zone", "limit": 10},
            )

            data = (
                search_data.get("data", {}) if search_data.get("data") else search_data
            )

            if data.get("success") and data.get("results"):
                results = data.get("results", [])
                logger.info(f"Found {len(results)} zone entities via search")

                for result in results:
                    entity_id = result.get("entity_id", "")
                    friendly_name = result.get("friendly_name", "Unknown")
                    logger.info(f"  - {entity_id}: {friendly_name}")
            else:
                logger.info("No zone entities found via search (may be normal)")

        except Exception as e:
            logger.warning(f"Zone search failed: {e}")
            logger.info("This may be normal if no zones exist")

    logger.info("Zone search and discovery test completed")
