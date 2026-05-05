"""
E2E tests for ha_deep_search exact_match parameter and dashboard search type.

Tests the features added to address issue #801:
- exact_match=True (default) uses substring matching
- exact_match=False enables fuzzy matching
- search_types=["dashboard"] searches dashboard configurations
"""

import logging

import pytest

from ..utilities.assertions import assert_mcp_success
from ..utilities.wait_helpers import wait_for_tool_result

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_deep_search_exact_match_default(mcp_client):
    """Test that exact_match defaults to True and filters precisely."""
    logger.info("Testing deep search with exact_match=True (default)")

    # Create an automation with a distinctive entity reference
    config = {
        "alias": "Exact Match Test Automation",
        "trigger": [
            {
                "platform": "state",
                "entity_id": "sensor.exact_match_test_unique_abc123",
                "to": "on",
            }
        ],
        "action": [{"service": "light.turn_on", "target": {"entity_id": "light.test"}}],
    }

    create_result = await mcp_client.call_tool(
        "ha_config_set_automation", {"config": config}
    )
    assert_mcp_success(create_result, "Create test automation")

    try:
        # Wait for automation to be findable with exact match
        data = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_deep_search",
            arguments={
                "query": "exact_match_test_unique_abc123",
                "search_types": ["automation"],
                "limit": 10,
            },
            predicate=lambda d: len(d.get("automations", [])) > 0,
            description="exact match finds test automation",
        )

        assert len(data["automations"]) > 0
        logger.info(f"Exact match found {len(data['automations'])} result(s)")

        # Now search for something that would fuzzy-match but not exact-match
        result_no_match = await mcp_client.call_tool(
            "ha_deep_search",
            {
                "query": "exact_match_test_unique_xyz999",
                "search_types": ["automation"],
                "limit": 10,
                # exact_match=True by default
            },
        )
        data_no_match = assert_mcp_success(result_no_match, "Exact non-match")
        automations = data_no_match.get("automations", [])
        # With exact match, a different suffix should NOT match
        for auto in automations:
            assert "Exact Match Test" not in auto.get("friendly_name", ""), (
                "Exact match should not fuzzy-match similar strings"
            )

        logger.info("Exact match correctly excludes non-matching results")

    finally:
        await mcp_client.call_tool(
            "ha_config_remove_automation",
            {"identifier": "automation.exact_match_test_automation"},
        )
        logger.info("Cleaned up test automation")


@pytest.mark.asyncio
async def test_deep_search_fuzzy_match_opt_in(mcp_client):
    """Test that exact_match=False enables fuzzy matching."""
    logger.info("Testing deep search with exact_match=False")

    config = {
        "alias": "Fuzzy Search Test Automation",
        "trigger": [
            {
                "platform": "state",
                "entity_id": "sensor.fuzzy_test_temperature",
                "to": "hot",
            }
        ],
        "action": [{"service": "light.turn_on", "target": {"entity_id": "light.test"}}],
    }

    create_result = await mcp_client.call_tool(
        "ha_config_set_automation", {"config": config}
    )
    assert_mcp_success(create_result, "Create test automation")

    try:
        # Wait for automation to be registered
        data = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_deep_search",
            arguments={
                "query": "fuzzy_test_temperature",
                "search_types": ["automation"],
                "limit": 10,
                "exact_match": False,
            },
            predicate=lambda d: len(d.get("automations", [])) > 0,
            description="fuzzy search finds test automation",
        )

        assert len(data["automations"]) > 0
        logger.info(f"Fuzzy match found {len(data['automations'])} result(s)")

    finally:
        await mcp_client.call_tool(
            "ha_config_remove_automation",
            {"identifier": "automation.fuzzy_search_test_automation"},
        )
        logger.info("Cleaned up test automation")


@pytest.mark.asyncio
async def test_deep_search_dashboard_type(mcp_client):
    """Test that search_types=['dashboard'] searches dashboard configurations."""
    logger.info("Testing deep search with dashboard search type")

    # Create a dashboard with a distinctive entity reference
    dashboard_config = {
        "views": [
            {
                "title": "Deep Search Dashboard Test",
                "cards": [
                    {
                        "type": "markdown",
                        "content": "deep_search_dashboard_marker_xyz789",
                    }
                ],
            }
        ]
    }

    create_result = await mcp_client.call_tool(
        "ha_config_set_dashboard",
        {
            "url_path": "deep-search-test-dash",
            "title": "Deep Search Test",
            "config": dashboard_config,
        },
    )
    assert_mcp_success(create_result, "Create test dashboard")

    try:
        # Search for the marker string in dashboards
        result = await mcp_client.call_tool(
            "ha_deep_search",
            {
                "query": "deep_search_dashboard_marker_xyz789",
                "search_types": ["dashboard"],
                "limit": 10,
            },
        )
        data = assert_mcp_success(result, "Dashboard deep search")

        assert "dashboards" in data, "Response should contain 'dashboards' key"
        dashboards = data["dashboards"]
        assert len(dashboards) > 0, "Should find dashboard containing marker string"

        # Verify the match is from our test dashboard
        found = any(
            d.get("dashboard_url") == "deep-search-test-dash"
            or d.get("dashboard_title") == "Deep Search Test"
            for d in dashboards
        )
        assert found, "Should find our specific test dashboard"
        logger.info(f"Dashboard search found {len(dashboards)} matching dashboard(s)")

    finally:
        await mcp_client.call_tool(
            "ha_config_delete_dashboard",
            {"url_path": "deep-search-test-dash"},
        )
        logger.info("Cleaned up test dashboard")


@pytest.mark.asyncio
async def test_deep_search_dashboard_not_in_default(mcp_client):
    """Test that dashboards are NOT searched by default (opt-in only)."""
    logger.info("Testing that dashboard search is opt-in")

    result = await mcp_client.call_tool(
        "ha_deep_search",
        {"query": "anything", "limit": 5},
    )
    data = assert_mcp_success(result, "Default deep search")

    # Default search_types should not include dashboards key
    assert "dashboards" not in data, (
        "Dashboards should not appear in default search results"
    )
    logger.info("Confirmed: dashboards not in default search results")


@pytest.mark.asyncio
async def test_search_entities_exact_match_default(mcp_client):
    """Test that ha_search_entities uses exact match by default."""
    logger.info("Testing ha_search_entities with exact_match=True (default)")

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "light", "limit": 5},
    )
    data_raw = assert_mcp_success(result, "Exact match search")
    data = data_raw.get("data", data_raw)

    assert data.get("success") is True
    assert data.get("search_type") == "exact_match", (
        f"Default should use exact_match, got '{data.get('search_type')}'"
    )
    logger.info(
        f"Search returned {len(data.get('results', []))} results with exact match"
    )
