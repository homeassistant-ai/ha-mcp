"""
Device Registry E2E Tests

Tests for the device registry management tools:
- ha_get_device: List all devices with optional filtering
- ha_get_device: Get device details including entities
- ha_set_device: Set device properties (name, area, disabled, labels)
- ha_remove_device: Remove orphaned devices

Key test scenarios:
- List devices and verify structure
- Get device details with entities
- Set device name (note: does NOT cascade to entities)
- Filter devices by area and manufacturer
- Handle non-existent devices
"""

import logging

import pytest

from ...utilities.assertions import parse_mcp_result, safe_call_tool

logger = logging.getLogger(__name__)


async def _core_2026_9_child_fixture(ha_client):
    """Return one real child fixture and whether the component is available."""
    info = await ha_client.send_websocket_message({"type": "ha_mcp_tools/info"})
    component_available = info.get("success") is True
    if component_available:
        assert "device_registry_child_semantics" in info["result"]["capabilities"]

    device_reply = await ha_client.send_websocket_message(
        {"type": "config/device_registry/list"}
    )
    entity_reply = await ha_client.send_websocket_message(
        {"type": "config/entity_registry/list"}
    )
    assert device_reply.get("success") is True, device_reply
    assert entity_reply.get("success") is True, entity_reply
    device_rows = device_reply.get("result")
    entity_rows = entity_reply.get("result")
    assert isinstance(device_rows, list)
    assert isinstance(entity_rows, list)

    devices_by_id = {
        row["id"]: row
        for row in device_rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    switch_entities_by_device: dict[str, list[str]] = {}
    for row in entity_rows:
        if not isinstance(row, dict):
            continue
        device_id = row.get("device_id")
        entity_id = row.get("entity_id")
        if (
            isinstance(device_id, str)
            and isinstance(entity_id, str)
            and entity_id.startswith("switch.")
        ):
            switch_entities_by_device.setdefault(device_id, []).append(entity_id)

    children_by_parent: dict[str, list[dict]] = {}
    for row in device_rows:
        if not isinstance(row, dict):
            continue
        parent_id = row.get("parent_device_id")
        child_id = row.get("id")
        if (
            isinstance(parent_id, str)
            and parent_id
            and isinstance(child_id, str)
            and switch_entities_by_device.get(child_id)
        ):
            children_by_parent.setdefault(parent_id, []).append(row)

    candidates = [
        (parent_id, sorted(children, key=lambda row: row["id"]))
        for parent_id, children in children_by_parent.items()
        if parent_id in devices_by_id and len(children) >= 2
    ]
    assert candidates, (
        "Core 2026.9 kitchen_sink did not create the expected power-strip "
        "parent with at least two child switch devices"
    )
    parent_id, children = sorted(candidates, key=lambda item: item[0])[0]
    child_id = children[0]["id"]
    child_entity_id = sorted(switch_entities_by_device[child_id])[0]
    return (
        parent_id,
        child_id,
        child_entity_id,
        devices_by_id[parent_id].get("area_id"),
        component_available,
    )


async def _error_log_total(mcp_client) -> int:
    """Return the authoritative current error-log line count."""
    result = parse_mcp_result(
        await mcp_client.call_tool("ha_get_logs", {"source": "error_log", "limit": 1})
    )
    assert result.get("success") is True, result
    return result["total_lines"]


async def _error_log_lines_since(mcp_client, baseline_total: int) -> list[str]:
    """Read only the bounded newest window added since a line-count boundary."""
    current_total = await _error_log_total(mcp_client)
    assert current_total >= baseline_total, (
        "Error-log history rotated during the child-device read assertion"
    )
    new_line_count = current_total - baseline_total
    new_log_lines: list[str] = []
    offset = 0
    while offset < new_line_count:
        page = parse_mcp_result(
            await mcp_client.call_tool(
                "ha_get_logs",
                {
                    "source": "error_log",
                    "limit": min(1000, new_line_count - offset),
                    "offset": offset,
                },
            )
        )
        assert page.get("success") is True, page
        assert page["offset"] == offset, page
        assert page["window_lines"] > 0, page
        new_log_lines.extend(page["log"].splitlines())
        offset += page["window_lines"]
    return new_log_lines


@pytest.mark.registry
class TestDeviceList:
    """Test ha_get_device functionality."""

    async def test_list_all_devices(self, mcp_client):
        """
        Test: List all devices in the registry (paginated, summary mode).

        Verifies the basic list functionality returns expected structure
        with pagination metadata.
        """
        logger.info("Testing device list - all devices")

        list_result = await mcp_client.call_tool("ha_get_device", {})
        list_data = parse_mcp_result(list_result)

        assert list_data.get("success"), f"Failed to list devices: {list_data}"
        assert "devices" in list_data, "Response should contain devices list"
        assert "count" in list_data, "Response should contain count"
        assert "total_devices" in list_data, "Response should contain total_devices"

        # Pagination metadata
        assert "has_more" in list_data, "Response should contain has_more"
        assert "offset" in list_data, "Response should contain offset"
        assert "limit" in list_data, "Response should contain limit"
        assert "total_count" in list_data, "Response should contain total_count"

        devices = list_data["devices"]
        count = list_data["count"]

        assert isinstance(devices, list), "devices should be a list"
        assert count == len(devices), f"Count mismatch: {count} vs {len(devices)}"
        assert count <= 50, f"Default page should have at most 50 devices, got {count}"

        logger.info(f"Listed {count} devices successfully")

        # If devices exist, verify structure
        if devices:
            device = devices[0]
            expected_fields = ["device_id", "name", "manufacturer", "model"]
            for field in expected_fields:
                assert field in device, f"Device missing field: {field}"
            # Summary mode should not include entities
            assert "entities" not in device, "Summary mode should omit entities"
            logger.info(
                f"Sample device: {device.get('name')} ({device.get('device_id')[:8]}...)"
            )

    async def test_list_devices_filter_by_area(self, mcp_client):
        """Filter devices by area_id (positive, multi-match, and negative cases).

        Seed (tests/initial_test_state/.storage/core.device_registry) assigns
        ``living_room`` to 2+ demo devices so this test reliably exercises the
        area-filter code path without relying on environmental luck.
        """
        logger.info("Testing device list - filter by area")

        all_result = await mcp_client.call_tool("ha_get_device", {})
        all_data = parse_mcp_result(all_result)
        assert all_data.get("success"), f"Failed to list all devices: {all_data}"

        filter_result = await mcp_client.call_tool(
            "ha_get_device",
            {"area_id": "living_room"},
        )
        filter_data = parse_mcp_result(filter_result)

        assert filter_data.get("success"), f"Failed to filter devices: {filter_data}"
        assert filter_data.get("filters"), "Response should indicate filters applied"
        assert filter_data["count"] >= 2, (
            f"Seed assigns living_room to 2+ devices but filter returned {filter_data['count']} — "
            "check tests/initial_test_state/.storage/core.device_registry"
        )
        assert filter_data["count"] < all_data["count"], (
            f"Filter returned {filter_data['count']} of {all_data['count']} devices — "
            "area filter appears to be ignored"
        )
        for device in filter_data["devices"]:
            assert device.get("area_id") == "living_room", (
                f"Device {device.get('name')} has area {device.get('area_id')}, expected living_room"
            )

        empty_result = await mcp_client.call_tool(
            "ha_get_device",
            {"area_id": "no_such_area_xyz123"},
        )
        empty_data = parse_mcp_result(empty_result)
        assert empty_data.get("success"), (
            f"Filter by nonexistent area should succeed, not raise: {empty_data}"
        )
        assert empty_data["count"] == 0, (
            f"Filter by nonexistent area returned {empty_data['count']} devices, expected 0"
        )

        logger.info(
            f"Area filter: {filter_data['count']} living_room devices, "
            f"negative case returned 0"
        )

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
            d for d in all_data.get("devices", []) if d.get("manufacturer")
        ]

        if not devices_with_mfr:
            logger.info(
                "No devices with manufacturers found, skipping manufacturer filter test"
            )
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

    async def test_list_devices_pagination(self, mcp_client):
        """Test that limit/offset pagination works for device listing."""
        logger.info("Testing device list pagination")

        # Get first page with small limit
        page1 = await mcp_client.call_tool(
            "ha_get_device",
            {"limit": 2, "offset": 0},
        )
        data1 = parse_mcp_result(page1)
        assert data1.get("success"), f"Page 1 failed: {data1}"

        if data1["total_count"] < 3:
            pytest.skip("Not enough devices to test pagination")

        assert data1["count"] == 2
        assert data1["offset"] == 0
        assert data1["has_more"] is True

        # Get second page
        page2 = await mcp_client.call_tool(
            "ha_get_device",
            {"limit": 2, "offset": 2},
        )
        data2 = parse_mcp_result(page2)
        assert data2.get("success"), f"Page 2 failed: {data2}"
        assert data2["offset"] == 2

        # Pages should not overlap
        ids1 = {d["device_id"] for d in data1["devices"]}
        ids2 = {d["device_id"] for d in data2["devices"]}
        assert ids1.isdisjoint(ids2), "Pages should not overlap"

        logger.info("Device pagination test passed")

    async def test_list_devices_full_detail(self, mcp_client):
        """Test that detail_level='full' includes entities in list mode."""
        logger.info("Testing device list with full detail")

        result = await mcp_client.call_tool(
            "ha_get_device",
            {"detail_level": "full", "limit": 5},
        )
        data = parse_mcp_result(result)
        assert data.get("success"), f"Full detail failed: {data}"
        assert data.get("detail_level") == "full"

        # Full mode should include entities per device
        for device in data.get("devices", []):
            assert "entities" in device, "Full mode should include entities"

        logger.info("Device full detail test passed")


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
            "device_id",
            "name",
            "manufacturer",
            "model",
            "area_id",
            "disabled_by",
            "labels",
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

        get_data = await safe_call_tool(
            mcp_client,
            "ha_get_device",
            {"device_id": "definitely_not_a_real_device_id_12345"},
        )

        assert not get_data.get("success"), "Getting non-existent device should fail"
        error = get_data.get("error", {})
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        assert "not found" in error_msg.lower(), (
            f"Error should indicate device not found: {get_data}"
        )
        logger.info("Non-existent device correctly rejected")


@pytest.mark.registry
async def test_core_2026_9_child_devices_inherit_parent_area(mcp_client, ha_client):
    """Exercise the real Core 2026.9 kitchen-sink child-device topology.

    The fixture creates one power-strip parent with multiple child outlets. The
    test assigns only the parent to ``living_room`` and proves the same inherited
    placement through search, single-device lookup, and area-filtered device list.
    The mutation is always restored, and the component must not touch Core's
    deprecated mapping methods while serving the reads.
    """
    (
        parent_id,
        child_id,
        child_entity_id,
        original_parent_area,
        component_available,
    ) = await _core_2026_9_child_fixture(ha_client)

    body_error: Exception | None = None
    restore_error: Exception | None = None
    try:
        update = parse_mcp_result(
            await mcp_client.call_tool(
                "ha_set_device", {"device_id": parent_id, "area_id": "living_room"}
            )
        )
        assert update.get("success") is True, update

        baseline_total_lines = (
            await _error_log_total(mcp_client) if component_available else None
        )

        search = parse_mcp_result(
            await mcp_client.call_tool(
                "ha_search",
                {
                    "query": child_entity_id,
                    "domain_filter": "switch",
                    "exact_match": True,
                    "result_fields": ["area"],
                },
            )
        )
        assert search.get("success") is True, search
        assert any(row.get("area") == "Living Room" for row in search["entities"]), (
            search
        )

        single = parse_mcp_result(
            await mcp_client.call_tool("ha_get_device", {"device_id": child_id})
        )
        assert single.get("success") is True, single
        assert single["device"]["device_id"] == child_id
        assert single["device"]["area_id"] == "living_room"
        assert single["device"]["config_entries"]

        area_list = parse_mcp_result(
            await mcp_client.call_tool(
                "ha_get_device", {"area_id": "living_room", "limit": 200}
            )
        )
        assert area_list.get("success") is True, area_list
        assert child_id in {row["device_id"] for row in area_list["devices"]}

        if baseline_total_lines is not None:
            deprecated_component_lines = [
                line
                for line in await _error_log_lines_since(
                    mcp_client, baseline_total_lines
                )
                if "helpers.frame" in line and "ha_mcp_tools" in line
            ]
            assert deprecated_component_lines == []
    except Exception as err:
        body_error = err
    finally:
        try:
            restore = parse_mcp_result(
                await mcp_client.call_tool(
                    "ha_set_device",
                    {
                        "device_id": parent_id,
                        "area_id": original_parent_area or "",
                    },
                )
            )
            assert restore.get("success") is True, restore
        except Exception as err:
            restore_error = err

    if body_error is not None and restore_error is not None:
        raise ExceptionGroup(
            "Core 2026.9 child-device read assertions and cleanup both failed",
            [body_error, restore_error],
        )
    if body_error is not None:
        raise body_error
    if restore_error is not None:
        raise restore_error


@pytest.mark.registry
class TestDeviceSet:
    """Test ha_set_device functionality."""

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
            "ha_set_device",
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
            "ha_set_device",
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

        Labels must exist in Home Assistant's label registry before they can
        be assigned — unknown label IDs are rejected instead of being stored
        as dangling references (issue #2159) — so the test creates its labels
        first and removes them afterwards.
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

        # Capture the original labels so cleanup restores the device exactly.
        detail_result = await mcp_client.call_tool(
            "ha_get_device", {"device_id": device_id}
        )
        detail_data = parse_mcp_result(detail_result)
        assert detail_data.get("success"), f"Failed to get device: {detail_data}"
        original_labels = detail_data.get("device", {}).get("labels", [])

        # Create the labels this test assigns. Creation happens inside the
        # cleanup guard so a failed second create still removes the first.
        test_labels = []
        try:
            for label_name in ("Device E2E Label A", "Device E2E Label B"):
                create_result = await mcp_client.call_tool(
                    "ha_config_set_label", {"name": label_name}
                )
                create_data = parse_mcp_result(create_result)
                assert create_data.get("success"), (
                    f"Failed to create label: {create_data}"
                )
                test_labels.append(create_data["label_id"])
            logger.info(f"Updating device {device_id} labels: {test_labels}")

            # Update device labels
            update_result = await mcp_client.call_tool(
                "ha_set_device",
                {
                    "device_id": device_id,
                    "labels": test_labels,
                },
            )
            update_data = parse_mcp_result(update_result)

            assert update_data.get("success"), f"Failed to update labels: {update_data}"
            updated_labels = update_data.get("device_entry", {}).get("labels", [])
            assert sorted(updated_labels) == sorted(test_labels), (
                f"Labels not applied: {update_data}"
            )
            logger.info(f"Labels applied: {updated_labels}")

            # Clear labels (set to empty)
            logger.info("Clearing device labels")
            clear_result = await mcp_client.call_tool(
                "ha_set_device",
                {
                    "device_id": device_id,
                    "labels": [],
                },
            )
            clear_data = parse_mcp_result(clear_result)
            assert clear_data.get("success"), f"Failed to clear labels: {clear_data}"
            logger.info("Labels cleared")
        finally:
            # Restore the device's original labels even when an assertion
            # above fails, then drop the labels this test created.
            await mcp_client.call_tool(
                "ha_set_device",
                {"device_id": device_id, "labels": original_labels},
            )
            for label_id in test_labels:
                await mcp_client.call_tool(
                    "ha_config_remove_label", {"label_id": label_id}
                )

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
        update_data = await safe_call_tool(
            mcp_client,
            "ha_set_device",
            {"device_id": device_id},
        )

        assert not update_data.get("success"), "Update with no changes should fail"
        error = update_data.get("error", {})
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        assert "no updates" in error_msg.lower(), (
            f"Error should mention no updates: {update_data}"
        )
        logger.info("No-changes update correctly rejected")

    async def test_update_device_nonexistent(self, mcp_client):
        """
        Test: Updating a non-existent device should fail gracefully
        """
        logger.info("Testing update non-existent device")

        update_data = await safe_call_tool(
            mcp_client,
            "ha_set_device",
            {
                "device_id": "definitely_not_a_real_device_id_12345",
                "name": "Test Name",
            },
        )

        assert not update_data.get("success"), (
            "Updating non-existent device should fail"
        )
        logger.info(
            f"Non-existent device update correctly rejected: {update_data.get('error')}"
        )


@pytest.mark.registry
class TestDeviceRemove:
    """Test ha_remove_device functionality."""

    async def test_remove_device_nonexistent(self, mcp_client):
        """
        Test: Removing a non-existent device should fail gracefully
        """
        logger.info("Testing remove non-existent device")

        remove_data = await safe_call_tool(
            mcp_client,
            "ha_remove_device",
            {"device_id": "definitely_not_a_real_device_id_12345"},
        )

        assert not remove_data.get("success"), (
            "Removing non-existent device should fail"
        )
        error = remove_data.get("error", {})
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        assert "not found" in error_msg.lower(), (
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
        "ha_set_device",
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
        "ha_set_device",
        {
            "device_id": device_id,
            "name": "",  # Clear custom name
        },
    )
    restore_data = parse_mcp_result(restore_result)
    assert restore_data.get("success"), f"Failed to restore device name: {restore_data}"

    logger.info("Device/entity naming independence test completed")


@pytest.mark.registry
class TestDeviceGetNegativeInputs:
    """
    A2 negative-input tests for ha_get_device's single-device lookup mode.

    Covers the nonexistent-device_id failure path. Existing tests in this
    file exercise the list mode, area/manufacturer filters, and the
    update/remove flows, but never call ha_get_device with a device_id
    that is absent from the device registry.

    Methodology: source-verified against tools_registry.py. When the
    requested device_id is not present in the device registry list,
    raise_tool_error is invoked with ErrorCode.RESOURCE_NOT_FOUND and the
    message "Device not found: ..." (devices live in the device registry,
    addressed by device_id UUID — not entities, so RESOURCE_NOT_FOUND is
    the correct category per #1297).
    """

    async def test_get_device_nonexistent_device_id(self, mcp_client):
        """
        Test: ha_get_device(device_id="<nonexistent>") returns a structured
        error with code RESOURCE_NOT_FOUND, not success=True.

        Source path: tools_registry.py — single-device lookup branch returns
        RESOURCE_NOT_FOUND when the device_id is absent from
        config/device_registry/list.
        """
        data = await safe_call_tool(
            mcp_client,
            "ha_get_device",
            {"device_id": "nonexistent_device_a2_e2e_xyz_404"},
        )

        assert not data.get("success"), (
            f"Expected failure for nonexistent device_id, got success=True: {data}"
        )
        assert data["error"]["code"] == "RESOURCE_NOT_FOUND", (
            f"Expected error code RESOURCE_NOT_FOUND, got: {data['error']}"
        )
        assert "suggestion" in data["error"], (
            "Error response should include a suggestion"
        )
        error_msg = data["error"]["message"].lower()
        assert "not found" in error_msg, (
            f"Expected 'not found' in error message, got: {data['error']}"
        )
