"""
Device Registry E2E Tests

Tests for the device registry management tools:
- ha_get_device: List all devices with optional filtering
- ha_get_device: Get device details including entities
- ha_update_device: Update device properties (name, area, disabled, labels)
- ha_remove_device: Remove orphaned devices

Key test scenarios:
- List devices and verify structure
- Get device details with entities
- Update device name (note: does NOT cascade to entities)
- Filter devices by area and manufacturer
- Handle non-existent devices
"""

import logging

import pytest

from ...utilities.assertions import parse_mcp_result

logger = logging.getLogger(__name__)


@pytest.mark.registry
class TestDeviceList:
    """Test ha_get_device functionality."""

    async def test_list_all_devices(self, mcp_client):
        """
        Test: List all devices in the registry

        Verifies the basic list functionality returns expected structure.
        """
        logger.info("Testing device list - all devices")

        list_result = await mcp_client.call_tool("ha_get_device", {})
        list_data = parse_mcp_result(list_result)

        assert list_data.get("success"), f"Failed to list devices: {list_data}"
        assert "devices" in list_data, "Response should contain devices list"
        assert "count" in list_data, "Response should contain count"
        assert "total_devices" in list_data, "Response should contain total_devices"

        devices = list_data["devices"]
        count = list_data["count"]

        assert isinstance(devices, list), "devices should be a list"
        assert count == len(devices), f"Count mismatch: {count} vs {len(devices)}"

        logger.info(f"Listed {count} devices successfully")

        # If devices exist, verify structure
        if devices:
            device = devices[0]
            expected_fields = ["device_id", "name", "manufacturer", "model"]
            for field in expected_fields:
                assert field in device, f"Device missing field: {field}"
            logger.info(f"Sample device: {device.get('name')} ({device.get('device_id')[:8]}...)")

    async def test_list_devices_filter_by_area(self, mcp_client):
        """
        Test: Filter devices by area_id

        Note: This test may not find devices if no devices are assigned to areas
        in the test environment.
        """
        logger.info("Testing device list - filter by area")

        # First, get all devices to find an area
        all_result = await mcp_client.call_tool("ha_get_device", {})
        all_data = parse_mcp_result(all_result)
        assert all_data.get("success"), f"Failed to list all devices: {all_data}"

        # Find a device with an area
        devices_with_area = [
            d for d in all_data.get("devices", [])
            if d.get("area_id")
        ]

        if not devices_with_area:
            logger.info("No devices with areas found, skipping area filter test")
            pytest.skip("No devices with areas in test environment")

        area_id = devices_with_area[0]["area_id"]
        logger.info(f"Testing filter with area_id: {area_id}")

        # Filter by area
        filter_result = await mcp_client.call_tool(
            "ha_get_device",
            {"area_id": area_id},
        )
        filter_data = parse_mcp_result(filter_result)

        assert filter_data.get("success"), f"Failed to filter devices: {filter_data}"
        assert filter_data.get("filters"), "Response should indicate filters applied"

        # Verify all returned devices have the specified area
        for device in filter_data.get("devices", []):
            assert device.get("area_id") == area_id, (
                f"Device {device.get('name')} has area {device.get('area_id')}, expected {area_id}"
            )

        logger.info(f"Area filter returned {filter_data['count']} devices")

    async def test_list_devices_filter_by_manufacturer(self, mcp_client):
        """
        Test: Filter devices by manufacturer name
        """
        logger.info("Testing device list - filter by manufacturer")

        # First, get all devices to find a manufacturer
        all_result = await mcp_client.call_tool("ha_get_device", {})
        all_data = parse_mcp_result(all_result)
        assert all_data.get("success"), f"Failed to list all devices: {all_data}"

        # Find a device with a manufacturer
        devices_with_mfr = [
            d for d in all_data.get("devices", [])
            if d.get("manufacturer")
        ]

        if not devices_with_mfr:
            logger.info("No devices with manufacturers found, skipping manufacturer filter test")
            pytest.skip("No devices with manufacturers in test environment")

        manufacturer = devices_with_mfr[0]["manufacturer"]
        logger.info(f"Testing filter with manufacturer: {manufacturer}")

        # Filter by manufacturer (partial match)
        filter_result = await mcp_client.call_tool(
            "ha_get_device",
            {"manufacturer": manufacturer[:5]},  # Partial match
        )
        filter_data = parse_mcp_result(filter_result)

        assert filter_data.get("success"), f"Failed to filter devices: {filter_data}"

        # Verify all returned devices contain the manufacturer substring
        for device in filter_data.get("devices", []):
            device_mfr = device.get("manufacturer", "").lower()
            assert manufacturer[:5].lower() in device_mfr, (
                f"Device {device.get('name')} manufacturer '{device_mfr}' "
                f"doesn't match filter '{manufacturer[:5]}'"
            )

        logger.info(f"Manufacturer filter returned {filter_data['count']} devices")


@pytest.mark.registry
class TestDeviceGet:
    """Test ha_get_device functionality."""

    async def test_get_device_details(self, mcp_client):
        """
        Test: Get detailed information about a specific device
        """
        logger.info("Testing get device details")

        # First, get a device ID
        list_result = await mcp_client.call_tool("ha_get_device", {})
        list_data = parse_mcp_result(list_result)
        assert list_data.get("success"), f"Failed to list devices: {list_data}"

        if not list_data.get("devices"):
            logger.info("No devices found, skipping get device test")
            pytest.skip("No devices in test environment")

        device_id = list_data["devices"][0]["device_id"]
        logger.info(f"Getting details for device: {device_id}")

        # Get device details
        get_result = await mcp_client.call_tool(
            "ha_get_device",
            {"device_id": device_id},
        )
        get_data = parse_mcp_result(get_result)

        assert get_data.get("success"), f"Failed to get device: {get_data}"
        assert "device" in get_data, "Response should contain device details"
        assert "entities" in get_data, "Response should contain entities list"
        assert "entity_count" in get_data, "Response should contain entity_count"

        device = get_data["device"]
        assert device.get("device_id") == device_id, "Device ID mismatch"

        # Verify device structure
        expected_fields = [
            "device_id", "name", "manufacturer", "model",
            "area_id", "disabled_by", "labels",
        ]
        for field in expected_fields:
            assert field in device, f"Device missing field: {field}"

        logger.info(
            f"Got device: {device.get('name')} with {get_data['entity_count']} entities"
        )

        # Log entities if present
        if get_data.get("entities"):
            for entity in get_data["entities"][:3]:
                logger.info(f"  - Entity: {entity.get('entity_id')}")

    async def test_get_device_nonexistent(self, mcp_client):
        """
        Test: Getting a non-existent device should fail gracefully
        """
        logger.info("Testing get non-existent device")

        get_result = await mcp_client.call_tool(
            "ha_get_device",
            {"device_id": "definitely_not_a_real_device_id_12345"},
        )
        get_data = parse_mcp_result(get_result)

        assert not get_data.get("success"), "Getting non-existent device should fail"
        assert "not found" in get_data.get("error", "").lower(), (
            f"Error should indicate device not found: {get_data}"
        )
        logger.info("Non-existent device correctly rejected")


@pytest.mark.registry
class TestDeviceUpdate:
    """Test ha_update_device functionality."""

    async def test_update_device_name(self, mcp_client):
        """
        Test: Update device display name (name_by_user)

        IMPORTANT: This does NOT cascade to entities - they keep their original entity_ids.
        """
        logger.info("Testing device name update")

        # First, get a device ID
        list_result = await mcp_client.call_tool("ha_get_device", {})
        list_data = parse_mcp_result(list_result)
        assert list_data.get("success"), f"Failed to list devices: {list_data}"

        if not list_data.get("devices"):
            logger.info("No devices found, skipping update test")
            pytest.skip("No devices in test environment")

        device = list_data["devices"][0]
        device_id = device["device_id"]
        original_name = device.get("name")
        test_name = "Test Device Name E2E"
        logger.info(f"Updating device {device_id} name: {original_name} -> {test_name}")

        # Update device name
        update_result = await mcp_client.call_tool(
            "ha_update_device",
            {
                "device_id": device_id,
                "name": test_name,
            },
        )
        update_data = parse_mcp_result(update_result)

        assert update_data.get("success"), f"Failed to update device: {update_data}"
        assert "note" in update_data, "Response should include note about entity rename"
        logger.info(f"Device name updated. Note: {update_data.get('note')}")

        # Verify update was applied
        assert "device_entry" in update_data, "Response should contain device_entry"
        updated_entry = update_data["device_entry"]
        # Check name_by_user (the user-defined name) or fallback to name
        actual_name = updated_entry.get("name_by_user") or updated_entry.get("name")
        assert actual_name == test_name, (
            f"Name not updated: expected '{test_name}', got '{actual_name}'"
        )

        # Restore original name (or clear custom name)
        logger.info("Restoring original device name")
        restore_result = await mcp_client.call_tool(
            "ha_update_device",
            {
                "device_id": device_id,
                "name": "",  # Clear custom name
            },
        )
        restore_data = parse_mcp_result(restore_result)
        assert restore_data.get("success"), f"Failed to restore name: {restore_data}"
        logger.info("Device name restored")

    async def test_update_device_labels(self, mcp_client):
        """
        Test: Update device labels

        Note: Labels must exist in Home Assistant's label registry before they
        can be assigned to devices. This test verifies the update mechanism
        works, even if labels don't exist (they'll be empty in that case).
        """
        logger.info("Testing device labels update")

        # First, get a device ID
        list_result = await mcp_client.call_tool("ha_get_device", {})
        list_data = parse_mcp_result(list_result)
        assert list_data.get("success"), f"Failed to list devices: {list_data}"

        if not list_data.get("devices"):
            logger.info("No devices found, skipping labels test")
            pytest.skip("No devices in test environment")

        device_id = list_data["devices"][0]["device_id"]
        test_labels = ["test_label", "e2e_test"]
        logger.info(f"Updating device {device_id} labels: {test_labels}")

        # Update device labels
        # Note: If labels don't exist in label registry, they won't be applied
        update_result = await mcp_client.call_tool(
            "ha_update_device",
            {
                "device_id": device_id,
                "labels": test_labels,
            },
        )
        update_data = parse_mcp_result(update_result)

        assert update_data.get("success"), f"Failed to update labels: {update_data}"
        logger.info("Labels update command succeeded")

        # Verify the response structure contains labels field
        updated_entry = update_data.get("device_entry", {})
        assert "labels" in updated_entry, "Response should contain labels field"
        updated_labels = updated_entry.get("labels", [])
        logger.info(f"Labels in response: {updated_labels}")

        # Note: Labels may be empty if the labels don't exist in label_registry
        # The important thing is that the API accepted the request
        if updated_labels:
            logger.info(f"Labels applied: {updated_labels}")
        else:
            logger.info(
                "Labels were not applied (likely labels don't exist in label registry)"
            )

        # Clear labels (set to empty)
        logger.info("Clearing device labels")
        clear_result = await mcp_client.call_tool(
            "ha_update_device",
            {
                "device_id": device_id,
                "labels": [],
            },
        )
        clear_data = parse_mcp_result(clear_result)
        assert clear_data.get("success"), f"Failed to clear labels: {clear_data}"
        logger.info("Labels cleared")

    async def test_update_device_no_changes(self, mcp_client):
        """
        Test: Calling update with no parameters should fail
        """
        logger.info("Testing device update with no changes")

        # First, get a device ID
        list_result = await mcp_client.call_tool("ha_get_device", {})
        list_data = parse_mcp_result(list_result)
        assert list_data.get("success"), f"Failed to list devices: {list_data}"

        if not list_data.get("devices"):
            pytest.skip("No devices in test environment")

        device_id = list_data["devices"][0]["device_id"]

        # Update with no parameters
        update_result = await mcp_client.call_tool(
            "ha_update_device",
            {"device_id": device_id},
        )
        update_data = parse_mcp_result(update_result)

        assert not update_data.get("success"), "Update with no changes should fail"
        assert "no updates" in update_data.get("error", "").lower(), (
            f"Error should mention no updates: {update_data}"
        )
        logger.info("No-changes update correctly rejected")

    async def test_update_device_nonexistent(self, mcp_client):
        """
        Test: Updating a non-existent device should fail gracefully
        """
        logger.info("Testing update non-existent device")

        update_result = await mcp_client.call_tool(
            "ha_update_device",
            {
                "device_id": "definitely_not_a_real_device_id_12345",
                "name": "Test Name",
            },
        )
        update_data = parse_mcp_result(update_result)

        assert not update_data.get("success"), "Updating non-existent device should fail"
        logger.info(f"Non-existent device update correctly rejected: {update_data.get('error')}")


@pytest.mark.registry
class TestDeviceRemove:
    """Test ha_remove_device functionality."""

    async def test_remove_device_nonexistent(self, mcp_client):
        """
        Test: Removing a non-existent device should fail gracefully
        """
        logger.info("Testing remove non-existent device")

        remove_result = await mcp_client.call_tool(
            "ha_remove_device",
            {"device_id": "definitely_not_a_real_device_id_12345"},
        )
        remove_data = parse_mcp_result(remove_result)

        assert not remove_data.get("success"), "Removing non-existent device should fail"
        assert "not found" in remove_data.get("error", "").lower(), (
            f"Error should indicate device not found: {remove_data}"
        )
        logger.info("Non-existent device removal correctly rejected")

    # Note: We don't test actual device removal in E2E tests
    # because we don't want to remove real devices from the test environment.
    # The ha_remove_device tool is primarily for orphaned devices.


@pytest.mark.registry
async def test_device_registry_workflow(mcp_client):
    """
    Quick test: Basic device registry workflow

    Tests the basic flow of listing and inspecting devices.
    """
    logger.info("Running basic device registry workflow test")

    # 1. List devices
    list_result = await mcp_client.call_tool("ha_get_device", {})
    list_data = parse_mcp_result(list_result)
    assert list_data.get("success"), f"Failed to list devices: {list_data}"
    logger.info(f"Listed {list_data['count']} devices")

    # 2. If devices exist, get details for first one
    if list_data.get("devices"):
        device_id = list_data["devices"][0]["device_id"]
        device_name = list_data["devices"][0]["name"]

        get_result = await mcp_client.call_tool(
            "ha_get_device",
            {"device_id": device_id},
        )
        get_data = parse_mcp_result(get_result)
        assert get_data.get("success"), f"Failed to get device: {get_data}"
        logger.info(
            f"Got device '{device_name}' with {get_data['entity_count']} entities"
        )
    else:
        logger.info("No devices in test environment, workflow test partial")

    logger.info("Basic device registry workflow test completed")


@pytest.mark.registry
async def test_device_entity_independence(mcp_client):
    """
    Test: Verify device and entity naming are independent

    This test documents the important behavior that renaming a device
    does NOT rename its entities.
    """
    logger.info("Testing device/entity naming independence")

    # Get a device with entities
    list_result = await mcp_client.call_tool("ha_get_device", {})
    list_data = parse_mcp_result(list_result)
    assert list_data.get("success"), f"Failed to list devices: {list_data}"

    if not list_data.get("devices"):
        pytest.skip("No devices in test environment")

    # Find a device with at least one entity
    device_with_entities = None
    for device in list_data["devices"]:
        get_result = await mcp_client.call_tool(
            "ha_get_device",
            {"device_id": device["device_id"]},
        )
        get_data = parse_mcp_result(get_result)
        if get_data.get("success") and get_data.get("entity_count", 0) > 0:
            device_with_entities = get_data
            break

    if not device_with_entities:
        pytest.skip("No devices with entities in test environment")

    device = device_with_entities["device"]
    entities = device_with_entities["entities"]
    device_id = device["device_id"]
    original_entity_ids = [e["entity_id"] for e in entities]

    logger.info(f"Testing with device: {device['name']} ({len(entities)} entities)")

    # Rename the device
    test_name = "Independence Test Device"
    update_result = await mcp_client.call_tool(
        "ha_update_device",
        {
            "device_id": device_id,
            "name": test_name,
        },
    )
    update_data = parse_mcp_result(update_result)
    assert update_data.get("success"), f"Failed to rename device: {update_data}"


    # Verify entities still have original entity_ids
    get_result = await mcp_client.call_tool(
        "ha_get_device",
        {"device_id": device_id},
    )
    get_data = parse_mcp_result(get_result)
    assert get_data.get("success"), f"Failed to get device after rename: {get_data}"

    new_entity_ids = [e["entity_id"] for e in get_data.get("entities", [])]
    assert set(new_entity_ids) == set(original_entity_ids), (
        f"Entity IDs should NOT change when device is renamed. "
        f"Original: {original_entity_ids}, New: {new_entity_ids}"
    )
    logger.info("Verified: Entity IDs unchanged after device rename")

    # Restore device name
    restore_result = await mcp_client.call_tool(
        "ha_update_device",
        {
            "device_id": device_id,
            "name": "",  # Clear custom name
        },
    )
    restore_data = parse_mcp_result(restore_result)
    assert restore_data.get("success"), f"Failed to restore device name: {restore_data}"

    logger.info("Device/entity naming independence test completed")
