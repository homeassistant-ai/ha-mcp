"""
E2E tests for Config Entry Options Flow API.

These tests verify the ability to configure existing integrations
through their options flows programmatically.
"""

import logging

import pytest

from tests.src.e2e.utilities.assertions import assert_mcp_success, parse_mcp_result

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.config
@pytest.mark.slow
class TestConfigEntryOptions:
    """Test Config Entry Options Flow tools."""

    async def test_list_config_entries(self, mcp_client):
        """Test listing all config entries."""
        result = await mcp_client.call_tool("ha_list_config_entries", {})
        data = assert_mcp_success(result, "List config entries")

        assert "count" in data, "Result should include count"
        assert "entries" in data, "Result should include entries list"
        assert isinstance(data["entries"], list), "Entries should be a list"

        if data["count"] > 0:
            entry = data["entries"][0]
            assert "entry_id" in entry, "Entry should have entry_id"
            assert "domain" in entry, "Entry should have domain"
            assert "title" in entry, "Entry should have title"
            assert "state" in entry, "Entry should have state"
            logger.info(
                f"Found {data['count']} config entries, "
                f"first: {entry['domain']} - {entry['title']}"
            )

    async def test_start_options_flow_with_options_support(self, mcp_client):
        """Test starting options flow for an entry that supports options."""
        # First, get entries that support options
        list_result = await mcp_client.call_tool("ha_list_config_entries", {})
        data = assert_mcp_success(list_result, "List config entries")

        # Find an entry with supports_options=True
        entries_with_options = [
            e for e in data.get("entries", []) if e.get("supports_options")
        ]

        if not entries_with_options:
            pytest.skip("No config entries with options support found")

        entry = entries_with_options[0]
        entry_id = entry["entry_id"]
        logger.info(
            f"Testing options flow for: {entry['domain']} - {entry['title']}"
        )

        # Start options flow
        result = await mcp_client.call_tool(
            "ha_start_config_entry_options_flow", {"entry_id": entry_id}
        )
        data = assert_mcp_success(result, "Start options flow")

        assert "flow_id" in data, "Result should include flow_id"
        assert "step_id" in data, "Result should include step_id"
        assert "type" in data, "Result should include type (menu or form)"

        flow_id = data["flow_id"]
        flow_type = data["type"]

        logger.info(f"Options flow started: flow_id={flow_id}, type={flow_type}")

        # Verify expected fields based on flow type
        if flow_type == "menu":
            assert "menu_options" in data, "Menu type should have menu_options"
            logger.info(f"Menu options: {data.get('menu_options')}")
        elif flow_type == "form":
            assert "data_schema" in data, "Form type should have data_schema"
            logger.info(f"Form has {len(data.get('data_schema', []))} fields")

        # Clean up - finish the flow without making changes
        finish_result = await mcp_client.call_tool(
            "ha_finish_config_entry_options_flow", {"flow_id": flow_id}
        )
        # Finishing may succeed or fail depending on flow state, both are OK
        logger.info(f"Flow cleanup result: {parse_mcp_result(finish_result)}")

    async def test_start_options_flow_invalid_entry(self, mcp_client):
        """Test error handling for invalid entry ID."""
        result = await mcp_client.call_tool(
            "ha_start_config_entry_options_flow",
            {"entry_id": "nonexistent_entry_id_12345"},
        )
        data = parse_mcp_result(result)

        # Should fail with an error
        assert not data.get("success", False), "Should fail for invalid entry"
        logger.info(f"Expected error for invalid entry: {data.get('error')}")

    async def test_submit_options_flow_invalid_flow(self, mcp_client):
        """Test error handling for invalid flow ID."""
        result = await mcp_client.call_tool(
            "ha_submit_config_entry_options_step",
            {
                "flow_id": "nonexistent_flow_id",
                "data": {"next_step_id": "test"},
            },
        )
        data = parse_mcp_result(result)

        # Should fail with an error
        assert not data.get("success", False), "Should fail for invalid flow"
        logger.info(f"Expected error for invalid flow: {data.get('error')}")

    async def test_submit_options_flow_invalid_data_type(self, mcp_client):
        """Test error handling for invalid data type."""
        # First start a valid flow
        list_result = await mcp_client.call_tool("ha_list_config_entries", {})
        data = parse_mcp_result(list_result)

        entries_with_options = [
            e for e in data.get("entries", []) if e.get("supports_options")
        ]

        if not entries_with_options:
            pytest.skip("No config entries with options support found")

        entry_id = entries_with_options[0]["entry_id"]

        start_result = await mcp_client.call_tool(
            "ha_start_config_entry_options_flow", {"entry_id": entry_id}
        )
        start_data = parse_mcp_result(start_result)

        if not start_data.get("success"):
            pytest.skip("Could not start options flow")

        flow_id = start_data["flow_id"]

        try:
            # Submit with invalid JSON string (not a dict)
            result = await mcp_client.call_tool(
                "ha_submit_config_entry_options_step",
                {"flow_id": flow_id, "data": '"not a dict"'},
            )
            data = parse_mcp_result(result)

            # Should fail with validation error
            assert not data.get("success", False), "Should fail for non-dict data"
            logger.info(f"Expected validation error: {data.get('error')}")
        finally:
            # Clean up
            await mcp_client.call_tool(
                "ha_finish_config_entry_options_flow", {"flow_id": flow_id}
            )

    async def test_finish_options_flow_invalid_flow(self, mcp_client):
        """Test error handling for finishing invalid flow."""
        result = await mcp_client.call_tool(
            "ha_finish_config_entry_options_flow",
            {"flow_id": "nonexistent_flow_id"},
        )
        data = parse_mcp_result(result)

        # Should fail with an error
        assert not data.get("success", False), "Should fail for invalid flow"
        logger.info(f"Expected error for invalid flow: {data.get('error')}")

    async def test_options_flow_complete_cycle(self, mcp_client):
        """Test a complete options flow cycle: start -> navigate -> finish."""
        # Get entries with options
        list_result = await mcp_client.call_tool("ha_list_config_entries", {})
        data = assert_mcp_success(list_result, "List config entries")

        entries_with_options = [
            e for e in data.get("entries", []) if e.get("supports_options")
        ]

        if not entries_with_options:
            pytest.skip("No config entries with options support found")

        entry = entries_with_options[0]
        logger.info(f"Testing complete cycle for: {entry['domain']} - {entry['title']}")

        # Start flow
        start_result = await mcp_client.call_tool(
            "ha_start_config_entry_options_flow", {"entry_id": entry["entry_id"]}
        )
        start_data = assert_mcp_success(start_result, "Start options flow")

        flow_id = start_data["flow_id"]
        flow_type = start_data["type"]

        # If it's a menu, try navigating to first option
        if flow_type == "menu" and start_data.get("menu_options"):
            first_option = start_data["menu_options"][0]
            logger.info(f"Navigating to menu option: {first_option}")

            submit_result = await mcp_client.call_tool(
                "ha_submit_config_entry_options_step",
                {"flow_id": flow_id, "data": {"next_step_id": first_option}},
            )
            submit_data = parse_mcp_result(submit_result)
            logger.info(
                f"After navigation - success: {submit_data.get('success')}, "
                f"step: {submit_data.get('step_id')}"
            )

        # Finish flow
        finish_result = await mcp_client.call_tool(
            "ha_finish_config_entry_options_flow", {"flow_id": flow_id}
        )
        finish_data = parse_mcp_result(finish_result)
        logger.info(f"Flow finished: {finish_data}")
