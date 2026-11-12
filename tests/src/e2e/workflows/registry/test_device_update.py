"""
Device Update E2E Tests

ha_set_device: rename, labels, a call without fields, and blank or
quote-only names.
"""

import logging
from typing import Any

import pytest

from ...utilities.assertions import (
    MCPAssertions,
    parse_mcp_result,
    safe_call_tool,
)
from ...utilities.cleanup import CleanupSteps

logger = logging.getLogger(__name__)


@pytest.mark.registry
class TestDeviceSet:
    """Test ha_set_device functionality."""

    async def test_device_rename_sets_name_by_user(self, mcp_client):
        """``name`` lands in Core's ``name_by_user``; Core's ``name`` stays."""
        logger.info("Testing device name update")

        # First, get a device ID
        list_result = await mcp_client.call_tool("ha_get_device", {})
        list_data = parse_mcp_result(list_result)
        assert list_data.get("success"), f"Failed to list devices: {list_data}"

        assert list_data.get("devices"), f"The seed has devices: {list_data}"

        device = list_data["devices"][0]
        device_id = device["id"]
        original_name = device.get("name")
        test_name = "Test Device Name E2E"
        logger.info(f"Updating device {device_id} name: {original_name} -> {test_name}")

        try:
            update_data = await MCPAssertions(mcp_client).call_tool_success(
                "ha_set_device", {"device_id": device_id, "name": test_name}
            )
            assert "note" in update_data, "Response should note entity names stay"
            updated_entry = update_data["device_entry"]
            assert updated_entry["name_by_user"] == test_name, update_data
            assert updated_entry["name"] == original_name, update_data
        finally:
            with CleanupSteps() as cleanup, cleanup.step():
                # Put back the name the device had; '' clears a custom name.
                await MCPAssertions(mcp_client).call_tool_success(
                    "ha_set_device",
                    {"device_id": device_id, "name": device.get("name_by_user") or ""},
                )

    async def test_device_labels_replace_the_set_and_empty_list_clears(
        self, mcp_client
    ):
        """
        ``labels`` replaces the device's label set, and ``[]`` empties it.

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

        assert list_data.get("devices"), f"The seed has devices: {list_data}"

        device_id = list_data["devices"][0]["id"]

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
            assert clear_data["device_entry"]["labels"] == [], clear_data
            logger.info("Labels cleared")
        finally:
            # Restore the device's original labels even when an assertion
            # above fails, then drop the labels this test created.
            with CleanupSteps() as cleanup:
                with cleanup.step():
                    await MCPAssertions(mcp_client).call_tool_success(
                        "ha_set_device",
                        {"device_id": device_id, "labels": original_labels},
                    )
                for label_id in test_labels:
                    with cleanup.step():
                        await safe_call_tool(
                            mcp_client, "ha_config_remove_label", {"label_id": label_id}
                        )

    async def test_update_without_fields_is_refused(self, mcp_client):
        """A call that names no field to change is refused before Core."""
        logger.info("Testing device update with no changes")

        # First, get a device ID
        list_result = await mcp_client.call_tool("ha_get_device", {})
        list_data = parse_mcp_result(list_result)
        assert list_data.get("success"), f"Failed to list devices: {list_data}"

        assert list_data.get("devices"), f"The seed has devices: {list_data}"

        device_id = list_data["devices"][0]["id"]

        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_failure(
                "ha_set_device",
                {"device_id": device_id},
                expected_error="no updates",
            )
        logger.info("No-changes update correctly rejected")


async def _device_for_entity(mcp_client: Any, entity_id: str) -> dict[str, Any]:
    data = parse_mcp_result(
        await mcp_client.call_tool("ha_get_device", {"entity_id": entity_id})
    )
    assert data.get("success"), f"Failed to resolve device of {entity_id}: {data}"
    return data["device"]


@pytest.mark.registry
class TestDeviceSetBlankAndQuoteOnlyValues:
    """Issue #2585: clients that cannot send ``''`` send ``'""'`` or ``' '``.

    Wiring checks against real HA; the value table lives in the unit tests.
    Each test uses a demo device no other e2e test touches and restores it.
    """

    async def test_quote_only_name_is_not_stored(self, mcp_client: Any) -> None:
        name = '""'
        device = await _device_for_entity(mcp_client, "sensor.total_gas_ft3")
        device_id = device["id"]
        original = device.get("name_by_user")
        try:
            async with MCPAssertions(mcp_client) as mcp:
                data = await mcp.call_tool_failure(
                    "ha_set_device", {"device_id": device_id, "name": name}
                )
            assert data["error"]["code"] == "VALIDATION_INVALID_PARAMETER", data

            after = await _device_for_entity(mcp_client, "sensor.total_gas_ft3")
            assert after.get("name_by_user") == original, after
        finally:
            with CleanupSteps() as cleanup, cleanup.step():
                await MCPAssertions(mcp_client).call_tool_success(
                    "ha_set_device", {"device_id": device_id, "name": original or ""}
                )

    async def test_whitespace_name_clears_custom_name(self, mcp_client: Any) -> None:
        device = await _device_for_entity(mcp_client, "number.small_range")
        device_id = device["id"]
        original = device.get("name_by_user")
        try:
            async with MCPAssertions(mcp_client) as mcp:
                await mcp.call_tool_success(
                    "ha_set_device", {"device_id": device_id, "name": "E2E 2585"}
                )
                data = await mcp.call_tool_success(
                    "ha_set_device", {"device_id": device_id, "name": " "}
                )
            assert data["device_entry"]["name_by_user"] is None, data
        finally:
            with CleanupSteps() as cleanup, cleanup.step():
                await MCPAssertions(mcp_client).call_tool_success(
                    "ha_set_device", {"device_id": device_id, "name": original or ""}
                )
