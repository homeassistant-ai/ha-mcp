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

    async def test_get_helper_schema_template_menu_top(self, mcp_client):
        """Template schema without menu_option returns top-level menu with sensor/binary_sensor."""
        result = await mcp_client.call_tool(
            "ha_get_helper_schema", {"helper_type": "template"}
        )
        data = assert_mcp_success(result, "Get template schema top-level")

        assert data.get("helper_type") == "template"
        assert data.get("flow_type") == "menu"
        assert "menu_options" in data
        assert "sensor" in data["menu_options"], f"Expected 'sensor' in {data['menu_options']}"
        assert "binary_sensor" in data["menu_options"], f"Expected 'binary_sensor' in {data['menu_options']}"
        logger.info(f"Template menu_options: {data['menu_options']}")

    async def test_get_helper_schema_template_sensor(self, mcp_client):
        """Template schema with menu_option='sensor' returns form fields including 'state'."""
        result = await mcp_client.call_tool(
            "ha_get_helper_schema",
            {"helper_type": "template", "menu_option": "sensor"},
        )
        data = assert_mcp_success(result, "Get template sensor schema")

        assert data.get("helper_type") == "template"
        assert data.get("flow_type") == "form"
        assert data.get("menu_option") == "sensor"
        assert "data_schema" in data
        field_names = [f.get("name") for f in data["data_schema"]]
        assert "state" in field_names, f"Expected 'state' field, got: {field_names}"
        logger.info(f"Template sensor fields: {field_names}")

    async def test_get_helper_schema_menu_option_invalid_for_form_helper(self, mcp_client):
        """Passing menu_option to a form-based helper returns a validation error."""
        data = await safe_call_tool(
            mcp_client,
            "ha_get_helper_schema",
            {"helper_type": "min_max", "menu_option": "sensor"},
        )
        assert data.get("success") is not True
        error_str = str(data).lower()
        assert any(
            kw in error_str for kw in ("menu_option", "form", "not 'menu'", "not applicable")
        ), f"Error should mention menu_option or flow type mismatch: {data}"

    async def test_get_helper_schema_invalid_menu_option(self, mcp_client):
        """Passing an invalid menu_option value returns a clear validation error."""
        data = await safe_call_tool(
            mcp_client,
            "ha_get_helper_schema",
            {"helper_type": "template", "menu_option": "nonexistent_type"},
        )
        assert data.get("success") is not True
        error_str = str(data).lower()
        assert any(
            kw in error_str
            for kw in ("valid options", "not valid", "menu_option", "400", "api error")
        ), f"Error should mention valid options or invalid menu_option: {data}"

    async def test_get_helper_schema_multiple_types(self, mcp_client):
        """Schema retrieval works for all supported helper types."""
        helper_types = ["utility_meter", "min_max"]

        for helper_type in helper_types:
            result = await mcp_client.call_tool(
                "ha_get_helper_schema", {"helper_type": helper_type}
            )
            data = assert_mcp_success(result, f"Get {helper_type} schema")
            assert data.get("helper_type") == helper_type
            assert "flow_type" in data

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
        """Creating a group helper without group_type returns a helpful error."""
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
