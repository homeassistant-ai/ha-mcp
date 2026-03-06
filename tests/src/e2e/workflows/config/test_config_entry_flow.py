"""
E2E tests for Config Entry Flow API.

Covers:
- Schema retrieval for form-based and menu-based helpers (ha_get_helper_schema)
- Creating a form-only helper (min_max)
- Creating a menu-based helper (group — menu then form)
- Error feedback on missing menu selection
- Deletion of config-entry-based helpers
"""

import logging

import pytest

from tests.src.e2e.utilities.assertions import assert_mcp_success, safe_call_tool
from tests.src.e2e.utilities.wait_helpers import wait_for_tool_result

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.config
@pytest.mark.slow
class TestConfigEntryFlow:
    """Test Config Entry Flow helper creation."""

    async def test_get_helper_schema_form_type(self, mcp_client):
        """Schema for a form-based helper returns data_schema fields."""
        result = await mcp_client.call_tool(
            "ha_get_helper_schema", {"helper_type": "min_max"}
        )
        data = assert_mcp_success(result, "Get min_max schema")

        assert data.get("helper_type") == "min_max"
        assert data.get("flow_type") == "form"
        assert "data_schema" in data
        assert isinstance(data["data_schema"], list)
        logger.info(f"min_max schema has {len(data['data_schema'])} fields")

    async def test_get_helper_schema_menu_type(self, mcp_client):
        """Schema for a menu-based helper (group) returns menu_options."""
        result = await mcp_client.call_tool(
            "ha_get_helper_schema", {"helper_type": "group"}
        )
        data = assert_mcp_success(result, "Get group schema")

        assert data.get("helper_type") == "group"
        assert "flow_type" in data

        if data.get("flow_type") == "menu":
            assert "menu_options" in data
            assert isinstance(data["menu_options"], list)
            assert len(data["menu_options"]) > 0, "Group should have at least one menu option"
            logger.info(f"Group has {len(data['menu_options'])} menu options: {data['menu_options']}")
        else:
            # HA may change group to form-based in future versions
            assert "data_schema" in data

    async def test_get_helper_schema_multiple_types(self, mcp_client):
        """Schema retrieval works for all supported helper types."""
        helper_types = ["template", "utility_meter", "min_max"]

        for helper_type in helper_types:
            result = await mcp_client.call_tool(
                "ha_get_helper_schema", {"helper_type": helper_type}
            )
            data = assert_mcp_success(result, f"Get {helper_type} schema")
            assert data.get("helper_type") == helper_type
            assert "flow_type" in data

    async def test_create_min_max_helper(self, mcp_client):
        """Create a min_max helper (single form step, no menu)."""
        helper_name = "test_min_max_e2e"
        config = {
            "name": helper_name,
            "entity_ids": ["sensor.demo_temperature", "sensor.demo_outside_temperature"],
            "type": "min",
        }

        result = await mcp_client.call_tool(
            "ha_create_config_entry_helper",
            {"helper_type": "min_max", "config": config},
        )
        data = assert_mcp_success(result, "Create min_max helper")
        assert data.get("success") is True
        assert data.get("entry_id") is not None
        entry_id = data["entry_id"]
        logger.info(f"Created min_max helper: {entry_id}")

        # Poll until the integration is registered
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_integration",
            arguments={"entry_id": entry_id},
            predicate=lambda d: d.get("success") is True,
            description="min_max helper is registered",
        )

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_delete_config_entry",
            {"entry_id": entry_id, "confirm": True},
        )

    async def test_create_group_helper_light(self, mcp_client):
        """Create a light group helper (menu then form flow)."""
        helper_name = "test_light_group_e2e"
        config = {
            "group_type": "light",
            "name": helper_name,
            "entities": [],  # empty list is valid
            "hide_members": False,
        }

        result = await mcp_client.call_tool(
            "ha_create_config_entry_helper",
            {"helper_type": "group", "config": config},
        )
        data = assert_mcp_success(result, "Create light group helper")
        assert data.get("success") is True
        assert data.get("entry_id") is not None
        entry_id = data["entry_id"]
        logger.info(f"Created light group helper: {entry_id}")

        # Poll until registered
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_integration",
            arguments={"entry_id": entry_id},
            predicate=lambda d: d.get("success") is True,
            description="light group helper is registered",
        )

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_delete_config_entry",
            {"entry_id": entry_id, "confirm": True},
        )

    async def test_create_group_helper_missing_menu_selection(self, mcp_client):
        """Creating a group helper without group_type returns a helpful error."""
        config = {"name": "my_group", "entities": []}  # missing group_type

        data = await safe_call_tool(
            mcp_client,
            "ha_create_config_entry_helper",
            {"helper_type": "group", "config": config},
        )
        assert data.get("success") is not True, "Should fail without group_type"
        # The error should mention available options or the missing key
        error_str = str(data)
        assert any(
            kw in error_str.lower()
            for kw in ("menu", "group_type", "next_step_id", "selection", "option")
        ), f"Error should mention menu selection: {error_str}"
