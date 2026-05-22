"""
E2E tests for Config Entry Flow API.

Covers:
- Creating a form-only helper (min_max)
- Creating a menu-based helper (group — menu then form)
- Error feedback on missing menu selection (data_schema_unavailable_reason
  marker + menu_options inline on validation errors)
- Deletion of config-entry-based helpers
"""

import logging

import pytest

from tests.src.e2e.utilities.assertions import assert_mcp_success, safe_call_tool
from tests.src.e2e.utilities.wait_helpers import wait_for_tool_result

logger = logging.getLogger(__name__)


async def _create_config_entry_helper(
    mcp_client, helper_type: str, config: dict, description: str
) -> str:
    """Create a config entry helper via unified ha_config_set_helper.

    The unified tool expects either a top-level `name` param or a `name` key
    in the `config` dict. The test fixtures place `name` inside `config`, so
    we forward it as-is. Polls until the new entry is registered, returns entry_id.
    """
    result = await mcp_client.call_tool(
        "ha_config_set_helper",
        {"helper_type": helper_type, "name": config.get("name", ""), "config": config},
    )
    data = assert_mcp_success(result, f"Create {description}")
    assert data.get("success") is True
    entry_id = data.get("entry_id")
    assert entry_id is not None
    logger.info(f"Created {description}: {entry_id}")

    await wait_for_tool_result(
        mcp_client,
        tool_name="ha_get_integration",
        arguments={"entry_id": entry_id},
        predicate=lambda d: d.get("success") is True,
        description=f"{description} is registered",
    )
    return entry_id


@pytest.mark.asyncio
@pytest.mark.config
@pytest.mark.slow
class TestConfigEntryFlow:
    """Test Config Entry Flow helper creation."""

    async def test_create_min_max_helper(self, mcp_client):
        """Create a min_max helper (single form step, no menu)."""
        config = {
            "name": "test_min_max_e2e",
            "entity_ids": ["sensor.demo_temperature", "sensor.demo_outside_temperature"],
            "type": "min",
        }
        entry_id = await _create_config_entry_helper(mcp_client, "min_max", config, "min_max helper")

        await safe_call_tool(
            mcp_client, "ha_delete_helpers_integrations", {"target": entry_id, "confirm": True}
        )

    async def test_create_group_helper_light(self, mcp_client):
        """Create a light group helper (menu then form flow)."""
        config = {
            "group_type": "light",
            "name": "test_light_group_e2e",
            "entities": [],  # empty list is valid
            "hide_members": False,
        }
        entry_id = await _create_config_entry_helper(mcp_client, "group", config, "light group helper")

        await safe_call_tool(
            mcp_client, "ha_delete_helpers_integrations", {"target": entry_id, "confirm": True}
        )

    async def test_create_template_sensor(self, mcp_client):
        """Create a template sensor helper end-to-end."""
        config = {
            "next_step_id": "sensor",
            "name": "test_template_sensor_e2e",
            "state": "{{ states('sun.sun') }}",
        }
        entry_id = await _create_config_entry_helper(mcp_client, "template", config, "template sensor")

        await safe_call_tool(
            mcp_client, "ha_delete_helpers_integrations", {"target": entry_id, "confirm": True}
        )

    async def test_create_template_binary_sensor(self, mcp_client):
        """Create a template binary sensor helper end-to-end."""
        config = {
            "next_step_id": "binary_sensor",
            "name": "test_template_binary_sensor_e2e",
            "state": "{{ is_state('sun.sun', 'above_horizon') }}",
        }
        entry_id = await _create_config_entry_helper(
            mcp_client, "template", config, "template binary sensor"
        )

        await safe_call_tool(
            mcp_client, "ha_delete_helpers_integrations", {"target": entry_id, "confirm": True}
        )

    async def test_update_min_max_helper(self, mcp_client):
        """Update an existing min_max helper via options flow (upsert with entry_id)."""
        config = {
            "name": "test_min_max_update_e2e",
            "entity_ids": ["sensor.demo_temperature"],
            "type": "min",
        }
        entry_id = await _create_config_entry_helper(
            mcp_client, "min_max", config, "min_max helper for update test"
        )

        # Update via options flow
        updated_config = {
            "entity_ids": ["sensor.demo_temperature", "sensor.demo_outside_temperature"],
            "type": "max",
        }
        update_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "min_max",
                "name": "test_min_max_update_e2e",
                "config": updated_config,
                "helper_id": entry_id,  # unified tool normalizes entry_id -> helper_id for flow helpers
            },
        )
        update_data = assert_mcp_success(update_result, "Update min_max helper")
        assert update_data.get("updated") is True

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_delete_helpers_integrations",
            {"target": entry_id, "confirm": True},
        )

    async def test_get_integration_include_schema(self, mcp_client):
        """ha_get_integration with include_schema=True returns options_schema for eligible entries."""
        # Find an entry that supports options
        list_result = await mcp_client.call_tool("ha_get_integration", {})
        list_data = assert_mcp_success(list_result, "List integrations")
        entry = next(
            (e for e in list_data.get("entries", []) if e.get("supports_options")),
            None,
        )
        if entry is None:
            pytest.skip("No config entries with supports_options=true in test environment")

        result = await mcp_client.call_tool(
            "ha_get_integration",
            {"entry_id": entry["entry_id"], "include_schema": True},
        )
        data = assert_mcp_success(result, "Get integration with schema")
        assert "options_schema" in data, "Expected options_schema in response"
        schema = data["options_schema"]
        assert schema.get("flow_type") in ("form", "menu")
        logger.info(f"options_schema flow_type={schema['flow_type']} for {entry['domain']}")

    async def test_create_group_helper_missing_menu_selection(self, mcp_client):
        """Creating a group helper without group_type returns a helpful error
        with the legal sub-types inline as ``menu_options`` (issue #1186).
        """
        config = {"name": "my_group", "entities": []}  # missing group_type

        data = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {"helper_type": "group", "name": "my_group", "config": config},
        )
        assert data.get("success") is not True, "Should fail without group_type"
        # The error should mention available options or the missing key
        error_str = str(data)
        assert any(
            kw in error_str.lower()
            for kw in ("menu", "group_type", "next_step_id", "selection", "option")
        ), f"Error should mention menu selection: {error_str}"
        # The error context must carry the legal sub-types inline so the
        # caller can pick a branch on the next try without a discovery
        # round-trip — see _handle_menu_step in tools_config_entry_flow.
        menu_options = data.get("menu_options")
        assert isinstance(menu_options, list) and menu_options, (
            f"Error should carry menu_options list: {data}"
        )
        assert "light" in menu_options, (
            f"Group menu_options should include 'light': {menu_options}"
        )
