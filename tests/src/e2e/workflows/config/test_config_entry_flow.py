"""
E2E tests for Config Entry Flow API.

Tests cover:
- Schema retrieval for form-based and menu-based helpers
- Creating helpers that use single-step form flows (min_max)
- Creating helpers that use menu-then-form flows (group)
- Error handling for missing menu selections
- Deletion of config-entry-based helpers
"""

import asyncio
import logging

import pytest

from tests.src.e2e.utilities.assertions import assert_mcp_success, safe_call_tool

logger = logging.getLogger(__name__)


async def _find_config_entry(mcp_client, domain: str, title: str) -> str | None:
    """Find config entry ID by domain and title.

    Searches the integration list for an entry matching both domain and title.
    Returns the entry_id if found, None otherwise.
    """
    result = await mcp_client.call_tool(
        "ha_get_integration", {"domain": domain}
    )
    data = assert_mcp_success(result, f"List {domain} integrations")

    for entry in data.get("entries", []):
        if entry.get("title") == title:
            return entry.get("entry_id")
    return None


async def _delete_config_entry(mcp_client, entry_id: str) -> None:
    """Delete a config entry by ID, ignoring errors."""
    try:
        await safe_call_tool(
            mcp_client,
            "ha_delete_config_entry",
            {"entry_id": entry_id, "confirm": True},
        )
    except Exception:
        logger.warning(f"Failed to delete config entry {entry_id}")


@pytest.mark.asyncio
@pytest.mark.config
@pytest.mark.slow
class TestConfigEntryFlow:
    """Test Config Entry Flow helper creation."""

    async def test_get_config_entry(self, mcp_client):
        """Test getting config entry details."""
        # Get any entry first
        list_result = await mcp_client.call_tool("ha_get_integration", {})
        data = assert_mcp_success(list_result, "List integrations")

        if not data.get("entries"):
            pytest.skip("No config entries found")

        entry_id = data["entries"][0]["entry_id"]
        logger.info(f"Testing get_config_entry with: {entry_id}")

        result = await mcp_client.call_tool(
            "ha_get_integration", {"entry_id": entry_id}
        )
        data = assert_mcp_success(result, "Get config entry")
        assert "entry" in data, "Result should include entry data"

    async def test_get_config_entry_nonexistent(self, mcp_client):
        """Test error handling for non-existent config entry."""
        data = await safe_call_tool(
            mcp_client, "ha_get_integration", {"entry_id": "nonexistent_entry_id"}
        )
        # Should fail with 404 or similar error
        assert not data.get("success", False)

    async def test_get_helper_schema(self, mcp_client):
        """Test getting helper schema for various helper types."""
        # Test with group (which has a menu)
        result = await mcp_client.call_tool(
            "ha_get_helper_schema", {"helper_type": "group"}
        )
        data = assert_mcp_success(result, "Get group helper schema")

        # Verify schema structure
        assert data.get("helper_type") == "group"
        assert "step_id" in data
        assert "flow_type" in data

        # Group uses a menu for type selection
        if data.get("flow_type") == "menu":
            assert "menu_options" in data
            assert isinstance(data.get("menu_options"), list)
            logger.info(
                f"Group helper has {len(data.get('menu_options', []))} menu options"
            )
        elif data.get("flow_type") == "form":
            assert "data_schema" in data
            logger.info(
                f"Group helper schema has {len(data.get('data_schema', []))} fields"
            )

    async def test_get_helper_schema_multiple_types(self, mcp_client):
        """Test schema retrieval for multiple helper types."""
        helper_types = ["template", "utility_meter", "min_max"]

        for helper_type in helper_types:
            result = await mcp_client.call_tool(
                "ha_get_helper_schema", {"helper_type": helper_type}
            )
            data = assert_mcp_success(result, f"Get {helper_type} schema")
            assert data.get("helper_type") == helper_type
            assert "flow_type" in data

            # Log schema info based on flow type
            if data.get("flow_type") == "menu":
                logger.info(
                    f"{helper_type}: menu with {len(data.get('menu_options', []))} options"
                )
            elif data.get("flow_type") == "form":
                logger.info(
                    f"{helper_type}: form with {len(data.get('data_schema', []))} fields"
                )

    async def test_create_config_entry_helper_exists(self, mcp_client):
        """Test that ha_create_config_entry_helper tool exists."""
        # Try to call with minimal/invalid config to verify tool exists
        data = await safe_call_tool(
            mcp_client,
            "ha_create_config_entry_helper",
            {"helper_type": "template", "config": {}},
        )

        # We expect this to fail (invalid config), but it proves tool exists
        assert "success" in data, "Tool should return a result with success field"
        logger.info(
            f"Tool response (expected to fail with invalid config): {data.get('success')}"
        )


@pytest.mark.asyncio
@pytest.mark.config
@pytest.mark.slow
class TestConfigEntryFlowMenuBased:
    """Test menu-based config entry flow helpers (e.g. group).

    These helpers present a menu step first (select group type), then
    a form step for the actual configuration. This tests the fix for
    issue #548 where menu-based flows always failed.
    """

    async def test_create_group_helper_light(self, mcp_client, cleanup_tracker):
        """Test creating a light group helper via menu-based flow.

        The group helper flow:
        1. Menu step: select group type (light, switch, cover, etc.)
        2. Form step: provide name, entities, and options
        3. create_entry: success
        """
        helper_name = "E2E Test Light Group"

        result = await mcp_client.call_tool(
            "ha_create_config_entry_helper",
            {
                "helper_type": "group",
                "config": {
                    "group_type": "light",
                    "name": helper_name,
                    "entities": ["light.bed_light", "light.ceiling_lights"],
                },
            },
        )
        data = assert_mcp_success(result, "Create light group helper")

        assert data.get("domain") == "group", f"Expected domain 'group': {data}"
        assert data.get("entry_id"), f"Missing entry_id: {data}"
        entry_id = data["entry_id"]
        cleanup_tracker.track("config_entry", entry_id)
        logger.info(f"Created light group helper: {data.get('title')} ({entry_id})")

        # Wait for HA to register the entity
        await asyncio.sleep(2)

        # Verify the config entry exists
        verify_result = await mcp_client.call_tool(
            "ha_get_integration", {"entry_id": entry_id}
        )
        verify_data = assert_mcp_success(verify_result, "Verify group config entry")
        assert verify_data.get("entry", {}).get("domain") == "group"
        logger.info("Light group config entry verified")

        # Clean up
        await _delete_config_entry(mcp_client, entry_id)

    async def test_create_group_helper_switch(self, mcp_client, cleanup_tracker):
        """Test creating a switch group helper via menu-based flow."""
        helper_name = "E2E Test Switch Group"

        result = await mcp_client.call_tool(
            "ha_create_config_entry_helper",
            {
                "helper_type": "group",
                "config": {
                    "group_type": "switch",
                    "name": helper_name,
                    "entities": [],
                },
            },
        )
        data = assert_mcp_success(result, "Create switch group helper")

        assert data.get("entry_id"), f"Missing entry_id: {data}"
        entry_id = data["entry_id"]
        cleanup_tracker.track("config_entry", entry_id)
        logger.info(f"Created switch group helper: {data.get('title')} ({entry_id})")

        # Clean up
        await asyncio.sleep(1)
        await _delete_config_entry(mcp_client, entry_id)

    async def test_create_group_helper_next_step_id(self, mcp_client, cleanup_tracker):
        """Test creating a group helper using 'next_step_id' key instead of 'group_type'."""
        helper_name = "E2E Test Cover Group"

        result = await mcp_client.call_tool(
            "ha_create_config_entry_helper",
            {
                "helper_type": "group",
                "config": {
                    "next_step_id": "cover",
                    "name": helper_name,
                    "entities": [],
                },
            },
        )
        data = assert_mcp_success(result, "Create cover group helper via next_step_id")

        assert data.get("entry_id"), f"Missing entry_id: {data}"
        entry_id = data["entry_id"]
        cleanup_tracker.track("config_entry", entry_id)
        logger.info(f"Created cover group helper: {data.get('title')} ({entry_id})")

        # Clean up
        await asyncio.sleep(1)
        await _delete_config_entry(mcp_client, entry_id)

    async def test_create_group_helper_missing_menu_selection(self, mcp_client):
        """Test that missing menu selection returns helpful error with options."""
        data = await safe_call_tool(
            mcp_client,
            "ha_create_config_entry_helper",
            {
                "helper_type": "group",
                "config": {
                    "name": "Missing Menu Selection",
                    "entities": ["light.bed_light"],
                },
            },
        )

        # Should fail because no group_type / next_step_id was provided
        assert not data.get("success"), f"Should have failed: {data}"
        assert "menu" in str(data.get("error", "")).lower() or "menu_options" in data, (
            f"Error should mention menu: {data}"
        )
        logger.info(f"Missing menu selection properly rejected: {data.get('error')}")

        # Verify that menu_options are included in the error for discoverability
        if "menu_options" in data:
            logger.info(f"Available menu options: {data['menu_options']}")


@pytest.mark.asyncio
@pytest.mark.config
@pytest.mark.slow
class TestConfigEntryFlowFormBased:
    """Test single-step form-based config entry flow helpers (e.g. min_max)."""

    async def test_create_min_max_helper(self, mcp_client, cleanup_tracker):
        """Test creating a min_max helper via single-step form flow.

        The min_max helper flow:
        1. Form step: provide name, entity_ids, type (min/max/mean/etc.)
        2. create_entry: success
        """
        helper_name = "E2E Test Min Max"

        result = await mcp_client.call_tool(
            "ha_create_config_entry_helper",
            {
                "helper_type": "min_max",
                "config": {
                    "name": helper_name,
                    "entity_ids": ["sensor.dht_sensor_temperature"],
                    "type": "max",
                },
            },
        )
        data = assert_mcp_success(result, "Create min_max helper")

        assert data.get("domain") == "min_max", f"Expected domain 'min_max': {data}"
        assert data.get("entry_id"), f"Missing entry_id: {data}"
        entry_id = data["entry_id"]
        cleanup_tracker.track("config_entry", entry_id)
        logger.info(f"Created min_max helper: {data.get('title')} ({entry_id})")

        # Clean up
        await asyncio.sleep(1)
        await _delete_config_entry(mcp_client, entry_id)
