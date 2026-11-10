"""
Device Registry E2E Tests

ha_get_device (list, filters, single lookup with entities, unknown ids) and
ha_remove_device (unknown id, Core's refusal). ha_set_device's own tests live
in test_device_update.py; the rename/entity-id independence test and the
child-area test here call it as a step.
"""

import logging

import pytest

from ...utilities.assertions import (
    MCPAssertions,
    parse_mcp_result,
    safe_call_tool,
)

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
        "Error-log history rotated while the test read it"
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
            expected_fields = ["id", "name", "manufacturer", "model"]
            for field in expected_fields:
                assert field in device, f"Device missing field: {field}"
            # Summary mode should not include entities
            assert "entities" not in device, "Summary mode should omit entities"
            logger.info(
                f"Sample device: {device.get('name')} ({device.get('id')[:8]}...)"
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
            assert device.get("effective_area_id") == "living_room", (
                f"Device {device.get('name')} is in {device.get('effective_area_id')}, expected living_room"
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
        ids1 = {d["id"] for d in data1["devices"]}
        ids2 = {d["id"] for d in data2["devices"]}
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

    async def test_integration_filter_follows_the_owning_config_entry(self, mcp_client):
        """demo's Notifier carries a ``notify`` identifier but belongs to demo's
        config entry: ``integration="demo"`` lists it as ``integration_type``
        demo, and ``integration="notify"`` does not list it."""
        async with MCPAssertions(mcp_client) as mcp:
            demo = await mcp.call_tool_success(
                "ha_get_device", {"integration": "demo", "limit": 200}
            )
            notify = await mcp.call_tool_success(
                "ha_get_device", {"integration": "notify", "limit": 200}
            )
        notifiers = [d for d in demo["devices"] if d["name"] == "Notifier"]
        assert len(notifiers) == 1, demo
        assert notifiers[0]["integration_type"] == "demo", notifiers
        assert notifiers[0]["id"] not in {d["id"] for d in notify["devices"]}, notify


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

        device_id = list_data["devices"][0]["id"]
        logger.info(f"Getting details for device: {device_id}")

        # Get device details
        get_result = await mcp_client.call_tool(
            "ha_get_device",
            {"device_id": device_id},
        )
        get_data = parse_mcp_result(get_result)

        assert get_data.get("success"), f"Failed to get device: {get_data}"
        assert "device" in get_data, "Response should contain device details"
        assert "entity_count" in get_data, "Response should contain entity_count"

        device = get_data["device"]
        assert "entities" in device, "Device should contain its entities"
        assert "linked_devices" in device, "Device should list its linked devices"
        assert device.get("id") == device_id, "Device ID mismatch"

        # Verify device structure
        expected_fields = [
            "id",
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
        if device.get("entities"):
            for entity in device["entities"][:3]:
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
        assert single["device"]["id"] == child_id
        # The child keeps no area of its own and sits in its parent's.
        assert single["device"]["area_id"] is None
        assert single["device"]["effective_area_id"] == "living_room"
        assert single["device"]["config_entry_id"]
        assert single["device"]["integration_type"] == "kitchen_sink"

        area_list = parse_mcp_result(
            await mcp_client.call_tool(
                "ha_get_device", {"area_id": "living_room", "limit": 200}
            )
        )
        assert area_list.get("success") is True, area_list
        assert child_id in {row["id"] for row in area_list["devices"]}

        if baseline_total_lines is not None:
            deprecated_component_lines = [
                line
                for line in await _error_log_lines_since(
                    mcp_client, baseline_total_lines
                )
                if "helpers.frame" in line and "ha_mcp_tools" in line
            ]
            assert deprecated_component_lines == []
    except Exception as err:  # noqa: BLE001
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
        except Exception as err:  # noqa: BLE001
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

    async def test_remove_device_refusal_comes_from_cores_remove_command(
        self, mcp_client
    ):
        """demo refuses to remove its devices, so this removes nothing. Core's
        refusal reaches the caller in Core's words, and Core logged no
        deprecation warning: the tool sent ``remove``, not the deprecated
        ``remove_config_entry``, which answers with the same refusal. The log
        check tells the two apart only on Core 2026.9 and later, which log
        the deprecation."""
        async with MCPAssertions(mcp_client) as mcp:
            found = await mcp.call_tool_success(
                "ha_get_device", {"entity_id": "notify.notifier"}
            )
        notifier = found["device"]
        baseline_total_lines = await _error_log_total(mcp_client)

        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_failure(
                "ha_remove_device", {"device_id": notifier["id"]}
            )
        assert data["error"]["message"] == (
            "Failed to remove device: Config entry does not support device removal"
        ), data
        deprecation_lines = [
            line
            for line in await _error_log_lines_since(mcp_client, baseline_total_lines)
            if "remove_config_entry is deprecated" in line
        ]
        assert deprecation_lines == []

    # A successful removal is not tested here: it would remove a device from
    # the shared test environment.


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
        device_id = list_data["devices"][0]["id"]
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
            {"device_id": device["id"]},
        )
        get_data = parse_mcp_result(get_result)
        if get_data.get("success") and get_data.get("entity_count", 0) > 0:
            device_with_entities = get_data
            break

    if not device_with_entities:
        pytest.skip("No devices with entities in test environment")

    device = device_with_entities["device"]
    entities = device["entities"]
    device_id = device["id"]
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

    new_entity_ids = [e["entity_id"] for e in get_data["device"]["entities"]]
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
    """ha_get_device with a device_id absent from the device registry."""

    async def test_get_device_nonexistent_device_id(self, mcp_client):
        """An unknown device_id fails with RESOURCE_NOT_FOUND and a suggestion."""
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
