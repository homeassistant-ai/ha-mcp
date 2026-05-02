"""
E2E tests for category assignment via domain-specific config tools.

Tests that ha_config_set_automation, ha_config_set_script, and
ha_config_set_helper properly assign categories via the entity registry,
and that ha_config_get_automation/script include categories in responses.
"""

import logging

import pytest

from ...utilities.assertions import assert_mcp_success
from ...utilities.wait_helpers import wait_for_tool_result

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

        # Poll until category appears in GET response (registry metadata may lag)
        get_data = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_config_get_automation",
            arguments={"identifier": entity_id},
            predicate=lambda d: d.get("config", {}).get("category") == category_id,
            description="automation GET includes category",
        )
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

        # Poll until category appears in GET response (registry metadata may lag)
        get_data = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_config_get_script",
            arguments={"script_id": "e2e_category_test_script"},
            predicate=lambda d: d.get("config", {}).get("category") == category_id,
            description="script GET includes category",
        )
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

    async def test_automation_param_takes_precedence_over_config_dict(
        self, mcp_client, cleanup_tracker
    ):
        """Test that category parameter takes precedence over config dict value."""
        logger.info("Testing category parameter precedence over config dict")

        # Create two categories
        cat_a_result = await mcp_client.call_tool(
            "ha_config_set_category",
            {"name": "E2E Precedence Cat A", "scope": "automation"},
        )
        cat_a_data = assert_mcp_success(cat_a_result, "Create category A")
        cat_a_id = cat_a_data.get("category_id")
        assert cat_a_id, f"Missing category_id: {cat_a_data}"
        cleanup_tracker.track("category", cat_a_id)

        cat_b_result = await mcp_client.call_tool(
            "ha_config_set_category",
            {"name": "E2E Precedence Cat B", "scope": "automation"},
        )
        cat_b_data = assert_mcp_success(cat_b_result, "Create category B")
        cat_b_id = cat_b_data.get("category_id")
        assert cat_b_id, f"Missing category_id: {cat_b_data}"
        cleanup_tracker.track("category", cat_b_id)

        # Create automation with category in both config dict and parameter
        # Parameter (cat_a_id) should win over config dict (cat_b_id)
        auto_result = await mcp_client.call_tool(
            "ha_config_set_automation",
            {
                "config": {
                    "alias": "E2E Precedence Test Automation",
                    "trigger": [{"platform": "time", "at": "05:00:00"}],
                    "action": [{"delay": {"seconds": 1}}],
                    "category": cat_b_id,
                },
                "category": cat_a_id,
            },
        )
        auto_data = assert_mcp_success(auto_result, "Create automation with both category sources")
        entity_id = auto_data.get("entity_id")
        assert entity_id, f"Missing entity_id: {auto_data}"
        cleanup_tracker.track("automation", entity_id)
        assert auto_data.get("category") == cat_a_id, (
            f"Parameter category should take precedence, got: {auto_data.get('category')}"
        )
        logger.info("Category parameter correctly took precedence over config dict")

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_automation", {"identifier": entity_id}
        )
        await mcp_client.call_tool(
            "ha_config_remove_category",
            {"scope": "automation", "category_id": cat_a_id},
        )
        await mcp_client.call_tool(
            "ha_config_remove_category",
            {"scope": "automation", "category_id": cat_b_id},
        )

    async def test_automation_category_on_update(self, mcp_client, cleanup_tracker):
        """Test adding a category to an existing automation via update."""
        logger.info("Testing category assignment on automation update")

        # Create a category
        cat_result = await mcp_client.call_tool(
            "ha_config_set_category",
            {"name": "E2E Update Cat Test", "scope": "automation"},
        )
        cat_data = assert_mcp_success(cat_result, "Create category")
        category_id = cat_data.get("category_id")
        assert category_id, f"Missing category_id: {cat_data}"
        cleanup_tracker.track("category", category_id)

        # Create automation WITHOUT category
        auto_result = await mcp_client.call_tool(
            "ha_config_set_automation",
            {
                "config": {
                    "alias": "E2E Update Category Test",
                    "trigger": [{"platform": "time", "at": "06:00:00"}],
                    "action": [{"delay": {"seconds": 1}}],
                },
            },
        )
        auto_data = assert_mcp_success(auto_result, "Create automation without category")
        entity_id = auto_data.get("entity_id")
        assert entity_id, f"Missing entity_id: {auto_data}"
        cleanup_tracker.track("automation", entity_id)
        logger.info(f"Created automation {entity_id} without category")

        # Update the automation to add a category
        update_result = await mcp_client.call_tool(
            "ha_config_set_automation",
            {
                "identifier": entity_id,
                "config": {
                    "alias": "E2E Update Category Test",
                    "trigger": [{"platform": "time", "at": "06:00:00"}],
                    "action": [{"delay": {"seconds": 1}}],
                },
                "category": category_id,
            },
        )
        update_data = assert_mcp_success(update_result, "Update automation with category")
        assert update_data.get("category") == category_id, (
            f"Category not set on update: {update_data}"
        )
        logger.info("Category added to existing automation via update")

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_automation", {"identifier": entity_id}
        )
        await mcp_client.call_tool(
            "ha_config_remove_category",
            {"scope": "automation", "category_id": category_id},
        )

    async def test_script_category_in_config_dict(self, mcp_client, cleanup_tracker):
        """Test that category in script config dict is extracted and applied."""
        logger.info("Testing script category extraction from config dict")

        # Create a category
        cat_result = await mcp_client.call_tool(
            "ha_config_set_category",
            {"name": "E2E Script Config Dict Cat", "scope": "script"},
        )
        cat_data = assert_mcp_success(cat_result, "Create script category")
        category_id = cat_data.get("category_id")
        assert category_id, f"Missing category_id: {cat_data}"
        cleanup_tracker.track("category", category_id)

        # Create script with category inside config dict
        script_result = await mcp_client.call_tool(
            "ha_config_set_script",
            {
                "script_id": "e2e_script_config_dict_cat",
                "config": {
                    "alias": "E2E Script Config Dict Category Test",
                    "sequence": [{"delay": {"seconds": 1}}],
                    "category": category_id,
                },
            },
        )
        script_data = assert_mcp_success(script_result, "Create script with config-dict category")
        assert script_data.get("category") == category_id, (
            f"Category not applied from config dict: {script_data}"
        )
        logger.info("Script category from config dict applied successfully")

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_script",
            {"script_id": "e2e_script_config_dict_cat"},
        )
        await mcp_client.call_tool(
            "ha_config_remove_category",
            {"scope": "script", "category_id": category_id},
        )

    async def test_helper_set_category(self, mcp_client, cleanup_tracker):
        """Test setting category on helper creation."""
        logger.info("Testing helper category via config tools")

        # Create a category for helpers
        cat_result = await mcp_client.call_tool(
            "ha_config_set_category",
            {"name": "E2E Helper Cat Test", "scope": "helpers"},
        )
        cat_data = assert_mcp_success(cat_result, "Create helper category")
        category_id = cat_data.get("category_id")
        assert category_id, f"Missing category_id: {cat_data}"
        cleanup_tracker.track("category", category_id)

        # Create input_boolean helper with category (no helper_id = create)
        helper_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E Category Test Toggle",
                "category": category_id,
            },
        )
        helper_data = assert_mcp_success(helper_result, "Create helper with category")
        # entity_id may be None for some helper types — derive from helper_data.id
        entity_id = helper_data.get("entity_id")
        inner_data = helper_data.get("helper_data", {})
        helper_id = inner_data.get("id")
        if not entity_id and helper_id:
            entity_id = f"input_boolean.{helper_id}"
        assert entity_id, f"Missing entity_id and helper_data.id: {helper_data}"
        cleanup_tracker.track("input_boolean", entity_id)

        # Check category was applied (in helper_data sub-dict)
        assert inner_data.get("category") == category_id, (
            f"Category not set in helper response: {helper_data}"
        )
        logger.info(f"Created helper {entity_id} with category {category_id}")

        # Clean up
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {
                "helper_type": "input_boolean",
                "target": entity_id,
                "confirm": True,
            },
        )
        await mcp_client.call_tool(
            "ha_config_remove_category",
            {"scope": "helpers", "category_id": category_id},
        )
