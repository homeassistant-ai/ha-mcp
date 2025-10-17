"""
Tests for ha_deep_search tool - searches within automation/script/helper configs.
"""

import asyncio
import logging

import pytest
from ..utilities.assertions import assert_mcp_success

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_deep_search_automation(mcp_client):
    """Test deep search finds automations by config content."""
    logger.info("🔍 Testing deep search for automations")

    # First create a test automation with distinctive content
    automation_config = {
        "alias": "Deep Search Test Automation",
        "trigger": [
            {
                "platform": "state",
                "entity_id": "sensor.deep_search_test_sensor",
                "to": "triggered",
            }
        ],
        "action": [
            {
                "service": "light.turn_on",
                "target": {"entity_id": "light.deep_search_test_light"},
            }
        ],
    }

    # Create the automation
    create_result = await mcp_client.call_tool(
        "ha_config_set_automation",
        {"config": automation_config},
    )
    create_data = assert_mcp_success(create_result, "Create test automation")
    logger.info(f"✅ Created automation: {create_data}")

    # Wait for entity to register in HA before searching
    await asyncio.sleep(5)

    try:
        # Test: Search for the sensor entity mentioned in the trigger
        result = await mcp_client.call_tool(
            "ha_deep_search",
            {
                "query": "deep_search_test_sensor",
                "search_types": ["automation"],
                "limit": 10,
            },
        )
        data = assert_mcp_success(result, "Deep search for sensor in automation")

        # Verify we found the automation
        automations = data.get("automations", [])
        assert len(automations) > 0, "Should find automation containing the sensor"

        # Find our specific automation
        found = False
        for auto in automations:
            if "Deep Search Test" in auto.get("friendly_name", ""):
                found = True
                assert auto.get("match_in_config", False), (
                    "Should match in config, not just name"
                )
                logger.info(
                    f"✅ Found automation with score {auto.get('score')}, "
                    f"match_in_config={auto.get('match_in_config')}"
                )
                break

        assert found, "Should find our test automation"

        # Test: Search for the service call in the action
        result2 = await mcp_client.call_tool(
            "ha_deep_search",
            {"query": "light.turn_on", "search_types": ["automation"], "limit": 10},
        )
        data2 = assert_mcp_success(result2, "Deep search for service in automation")

        automations2 = data2.get("automations", [])
        assert len(automations2) > 0, "Should find automation with light.turn_on service"
        logger.info(f"✅ Found {len(automations2)} automations using light.turn_on")

    finally:
        # Cleanup: Delete the test automation
        await mcp_client.call_tool(
            "ha_config_remove_automation",
            {"identifier": "automation.deep_search_test_automation"},
        )
        logger.info("🧹 Cleaned up test automation")


@pytest.mark.asyncio
async def test_deep_search_script(mcp_client):
    """Test deep search finds scripts by config content."""
    logger.info("🔍 Testing deep search for scripts")

    # Create a test script with distinctive content
    script_config = {
        "alias": "Deep Search Test Script",
        "sequence": [
            {
                "service": "notify.persistent_notification",
                "data": {"message": "deep_search_unique_message"},
            },
            {"delay": {"seconds": 1}},
        ],
    }

    # Create the script
    create_result = await mcp_client.call_tool(
        "ha_config_set_script",
        {
            "script_id": "deep_search_test_script",
            "config": script_config,
        },
    )
    create_data = assert_mcp_success(create_result, "Create test script")
    logger.info(f"✅ Created script: {create_data}")

    # Wait for entity to register in HA before searching
    await asyncio.sleep(5)

    try:
        # Test: Search for the unique message in the script
        result = await mcp_client.call_tool(
            "ha_deep_search",
            {
                "query": "deep_search_unique_message",
                "search_types": ["script"],
                "limit": 10,
            },
        )
        data = assert_mcp_success(result, "Deep search for message in script")

        # Verify we found the script
        scripts = data.get("scripts", [])
        assert len(scripts) > 0, "Should find script containing the unique message"

        # Find our specific script
        found = False
        for script in scripts:
            if "Deep Search Test" in script.get("friendly_name", ""):
                found = True
                assert script.get("match_in_config", False), (
                    "Should match in config, not just name"
                )
                logger.info(
                    f"✅ Found script with score {script.get('score')}, "
                    f"match_in_config={script.get('match_in_config')}"
                )
                break

        assert found, "Should find our test script"

        # Test: Search for the delay action
        result2 = await mcp_client.call_tool(
            "ha_deep_search",
            {"query": "delay", "search_types": ["script"], "limit": 10},
        )
        data2 = assert_mcp_success(result2, "Deep search for delay in script")

        scripts2 = data2.get("scripts", [])
        logger.info(f"✅ Found {len(scripts2)} scripts with delay")

    finally:
        # Cleanup: Delete the test script
        await mcp_client.call_tool(
            "ha_config_remove_script",
            {"script_id": "script.deep_search_test_script"},
        )
        logger.info("🧹 Cleaned up test script")


@pytest.mark.asyncio
async def test_deep_search_helper(mcp_client):
    """Test deep search finds helpers by config content."""
    logger.info("🔍 Testing deep search for helpers")

    # Create a test input_select helper with distinctive options
    helper_config = {
        "name": "Deep Search Test Select",
        "options": ["deep_search_option_a", "deep_search_option_b", "option_c"],
    }

    # Create the helper
    create_result = await mcp_client.call_tool(
        "ha_config_set_helper",
        {
            "helper_type": "input_select",
            "name": helper_config["name"],
            "options": helper_config["options"],
        },
    )
    create_data = assert_mcp_success(create_result, "Create test helper")
    logger.info(f"✅ Created helper: {create_data}")

    # Wait for entity to register in HA before searching
    await asyncio.sleep(5)

    try:
        # Test: Search for the unique option in the helper
        result = await mcp_client.call_tool(
            "ha_deep_search",
            {
                "query": "deep_search_option_a",
                "search_types": ["helper"],
                "limit": 10,
            },
        )
        data = assert_mcp_success(result, "Deep search for option in helper")

        # Verify we found the helper
        helpers = data.get("helpers", [])
        assert len(helpers) > 0, "Should find helper containing the unique option"

        # Find our specific helper
        found = False
        for helper in helpers:
            helper_name = helper.get("name", helper.get("friendly_name", ""))
            if "Deep Search Test" in helper_name:
                found = True
                assert helper.get("match_in_config", False), (
                    "Should match in config, not just name"
                )
                logger.info(
                    f"✅ Found helper with score {helper.get('score')}, "
                    f"match_in_config={helper.get('match_in_config')}"
                )
                break

        assert found, "Should find our test helper"

    finally:
        # Cleanup: Delete the test helper
        await mcp_client.call_tool(
            "ha_config_remove_helper",
            {
                "helper_type": "input_select",
                "helper_id": "deep_search_test_select",
            },
        )
        logger.info("🧹 Cleaned up test helper")


@pytest.mark.asyncio
async def test_deep_search_all_types(mcp_client):
    """Test deep search across all types simultaneously."""
    logger.info("🔍 Testing deep search across all types")

    # Search for a common keyword that might appear in multiple types
    result = await mcp_client.call_tool(
        "ha_deep_search",
        {
            "query": "light",
            "limit": 20,
        },
    )
    data = assert_mcp_success(result, "Deep search across all types")

    # Verify we get results grouped by type
    automations = data.get("automations", [])
    scripts = data.get("scripts", [])
    helpers = data.get("helpers", [])

    total_results = len(automations) + len(scripts) + len(helpers)
    logger.info(
        f"✅ Found {total_results} total results: "
        f"{len(automations)} automations, {len(scripts)} scripts, "
        f"{len(helpers)} helpers"
    )

    # Each result should have the expected structure
    for auto in automations:
        assert "entity_id" in auto, "Automation should have entity_id"
        assert "friendly_name" in auto, "Automation should have friendly_name"
        assert "score" in auto, "Automation should have score"
        assert "match_in_name" in auto, "Automation should have match_in_name flag"
        assert "match_in_config" in auto, "Automation should have match_in_config flag"


@pytest.mark.asyncio
async def test_deep_search_limit(mcp_client):
    """Test that deep search respects the limit parameter."""
    logger.info("🔍 Testing deep search limit parameter")

    # Search with a small limit
    result = await mcp_client.call_tool(
        "ha_deep_search",
        {
            "query": "light",
            "limit": 5,
        },
    )
    data = assert_mcp_success(result, "Deep search with limit=5")

    # Count total results
    automations = data.get("automations", [])
    scripts = data.get("scripts", [])
    helpers = data.get("helpers", [])
    total_results = len(automations) + len(scripts) + len(helpers)

    assert total_results <= 5, f"Should respect limit of 5, got {total_results}"
    logger.info(f"✅ Correctly limited results to {total_results} (limit was 5)")


@pytest.mark.asyncio
async def test_deep_search_no_results(mcp_client):
    """Test deep search with query that matches nothing."""
    logger.info("🔍 Testing deep search with no matches")

    result = await mcp_client.call_tool(
        "ha_deep_search",
        {
            "query": "xyzabc123_nonexistent_query_string",
            "limit": 10,
        },
    )
    data = assert_mcp_success(result, "Deep search with no matches")

    # Verify we get empty results
    # Filter out any test entities that may not have been cleaned up from parallel tests
    automations = [a for a in data.get("automations", []) if "deep_search" not in a.get("entity_id", "").lower()]
    scripts = [s for s in data.get("scripts", []) if "deep_search" not in s.get("entity_id", "").lower()]
    helpers = [h for h in data.get("helpers", []) if "deep_search" not in h.get("entity_id", "").lower()]

    assert len(automations) == 0, "Should have no automation matches"
    assert len(scripts) == 0, "Should have no script matches"
    assert len(helpers) == 0, "Should have no helper matches"

    logger.info("✅ Correctly returned empty results for non-matching query")
