"""
E2E tests for category assignment via domain-specific config tools.

Tests that ha_config_set_automation, ha_config_set_script, and
ha_config_set_helper properly assign categories via the entity registry,
and that ha_config_get_automation/script include categories in responses.
"""

import logging

import pytest

from ...utilities.assertions import assert_mcp_success

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.config
class TestConfigToolCategories:
    """Test category assignment via domain-specific config tools."""

    async def test_automation_set_and_get_category(self, mcp_client, cleanup_tracker):
        """Test setting category on automation creation and retrieving it."""
        logger.info("Testing automation category via config tools")

        # Create a category first
        cat_result = await mcp_client.call_tool(
            "ha_config_set_category",
            {"name": "E2E Automation Cat Test", "scope": "automation"},
        )
        cat_data = assert_mcp_success(cat_result, "Create automation category")
        category_id = cat_data.get("category_id")
        assert category_id, f"Missing category_id: {cat_data}"
        cleanup_tracker.track("category", category_id)

        # Create automation with category
        auto_result = await mcp_client.call_tool(
            "ha_config_set_automation",
            {
                "config": {
                    "alias": "E2E Category Test Automation",
                    "description": "Test automation for category assignment",
                    "trigger": [{"platform": "time", "at": "03:00:00"}],
                    "action": [{"delay": {"seconds": 1}}],
                },
                "category": category_id,
            },
        )
        auto_data = assert_mcp_success(auto_result, "Create automation with category")
        entity_id = auto_data.get("entity_id")
        assert entity_id, f"Missing entity_id: {auto_data}"
        cleanup_tracker.track("automation", entity_id)
        assert auto_data.get("category") == category_id, (
            f"Category not set in response: {auto_data}"
        )
        logger.info(f"Created automation {entity_id} with category {category_id}")

        # Verify category appears in GET response
        get_result = await mcp_client.call_tool(
            "ha_config_get_automation",
            {"identifier": entity_id},
        )
        get_data = assert_mcp_success(get_result, "Get automation with category")
        config = get_data.get("config", {})
        assert config.get("category") == category_id, (
            f"Category missing from GET response: {config}"
        )
        logger.info("Automation category verified via GET")

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_automation", {"identifier": entity_id}
        )
        await mcp_client.call_tool(
            "ha_config_remove_category",
            {"scope": "automation", "category_id": category_id},
        )

    async def test_script_set_and_get_category(self, mcp_client, cleanup_tracker):
        """Test setting category on script creation and retrieving it."""
        logger.info("Testing script category via config tools")

        # Create a category first
        cat_result = await mcp_client.call_tool(
            "ha_config_set_category",
            {"name": "E2E Script Cat Test", "scope": "script"},
        )
        cat_data = assert_mcp_success(cat_result, "Create script category")
        category_id = cat_data.get("category_id")
        assert category_id, f"Missing category_id: {cat_data}"
        cleanup_tracker.track("category", category_id)

        # Create script with category
        script_result = await mcp_client.call_tool(
            "ha_config_set_script",
            {
                "script_id": "e2e_category_test_script",
                "config": {
                    "alias": "E2E Category Test Script",
                    "sequence": [{"delay": {"seconds": 1}}],
                },
                "category": category_id,
            },
        )
        script_data = assert_mcp_success(script_result, "Create script with category")
        assert script_data.get("category") == category_id, (
            f"Category not set in response: {script_data}"
        )
        logger.info(f"Created script with category {category_id}")

        # Verify category appears in GET response
        get_result = await mcp_client.call_tool(
            "ha_config_get_script",
            {"script_id": "e2e_category_test_script"},
        )
        get_data = assert_mcp_success(get_result, "Get script with category")
        config = get_data.get("config", {})
        assert config.get("category") == category_id, (
            f"Category missing from GET response: {config}"
        )
        logger.info("Script category verified via GET")

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_script",
            {"script_id": "e2e_category_test_script"},
        )
        await mcp_client.call_tool(
            "ha_config_remove_category",
            {"scope": "script", "category_id": category_id},
        )

    async def test_automation_category_in_config_dict(self, mcp_client, cleanup_tracker):
        """Test that category in config dict is extracted and applied."""
        logger.info("Testing category extraction from config dict")

        # Create a category
        cat_result = await mcp_client.call_tool(
            "ha_config_set_category",
            {"name": "E2E Config Dict Cat", "scope": "automation"},
        )
        cat_data = assert_mcp_success(cat_result, "Create category")
        category_id = cat_data.get("category_id")
        cleanup_tracker.track("category", category_id)

        # Create automation with category inside config dict (not as separate param)
        auto_result = await mcp_client.call_tool(
            "ha_config_set_automation",
            {
                "config": {
                    "alias": "E2E Config Dict Category Test",
                    "description": "Test category extraction from config dict",
                    "trigger": [{"platform": "time", "at": "04:00:00"}],
                    "action": [{"delay": {"seconds": 1}}],
                    "category": category_id,
                },
            },
        )
        auto_data = assert_mcp_success(auto_result, "Create automation with config-dict category")
        entity_id = auto_data.get("entity_id")
        assert entity_id, f"Missing entity_id: {auto_data}"
        cleanup_tracker.track("automation", entity_id)
        assert auto_data.get("category") == category_id, (
            f"Category not applied from config dict: {auto_data}"
        )
        logger.info("Category from config dict applied successfully")

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_automation", {"identifier": entity_id}
        )
        await mcp_client.call_tool(
            "ha_config_remove_category",
            {"scope": "automation", "category_id": category_id},
        )
