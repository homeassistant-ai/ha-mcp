"""
Integration Listing E2E Tests

Tests the ha_list_integrations tool for listing and filtering
Home Assistant config entries (integrations).

Note: Tests are designed to work with the Docker test environment.
The actual integrations available will vary based on the test setup.
"""

import logging

import pytest

from ...utilities.assertions import (
    assert_mcp_success,
    parse_mcp_result,
)

logger = logging.getLogger(__name__)


@pytest.mark.integrations
class TestListIntegrations:
    """Test integration listing functionality."""

    async def test_list_all_integrations(self, mcp_client):
        """
        Test: List all integrations without filters

        This test validates that we can retrieve all configured integrations
        from Home Assistant.
        """
        logger.info("Testing ha_list_integrations without filters...")

        result = await mcp_client.call_tool("ha_list_integrations", {})

        data = assert_mcp_success(result, "list all integrations")

        # Verify response structure
        assert "total" in data, "Response should include total count"
        assert "entries" in data, "Response should include entries list"
        assert "state_summary" in data, "Response should include state summary"
        assert "filters_applied" in data, "Response should include filters applied"

        total = data["total"]
        entries = data["entries"]
        state_summary = data["state_summary"]

        logger.info(f"Found {total} integrations")
        logger.info(f"State summary: {state_summary}")

        # In a fresh test environment, there should be at least some integrations
        # (default_config, etc.)
        assert total >= 0, "Total should be non-negative"
        assert isinstance(entries, list), "Entries should be a list"
        assert len(entries) == total, "Entry count should match total"

        # Verify entry structure (if we have entries)
        if entries:
            entry = entries[0]
            expected_fields = [
                "entry_id",
                "domain",
                "title",
                "state",
                "source",
                "supports_options",
                "supports_unload",
                "disabled_by",
            ]

            for field in expected_fields:
                assert field in entry, f"Entry should have '{field}' field"

            logger.info(f"Sample entry: domain={entry['domain']}, state={entry['state']}")

        # Verify state_summary matches entries
        total_from_summary = sum(state_summary.values())
        assert total_from_summary == total, (
            f"State summary total ({total_from_summary}) should match total ({total})"
        )

        # Verify filters applied shows no filters
        assert data["filters_applied"]["domain"] is None
        assert data["filters_applied"]["type_filter"] is None

        logger.info("All integrations listed successfully")

    async def test_filter_by_domain(self, mcp_client):
        """
        Test: Filter integrations by domain

        This test validates filtering integrations by a specific domain.
        We first get all integrations to find a valid domain to filter by.
        """
        logger.info("Testing ha_list_integrations with domain filter...")

        # First, get all integrations to find a valid domain
        all_result = await mcp_client.call_tool("ha_list_integrations", {})
        all_data = assert_mcp_success(all_result, "get all integrations")

        if all_data["total"] == 0:
            pytest.skip("No integrations available to test domain filtering")

        # Find a domain that has entries
        test_domain = all_data["entries"][0]["domain"]
        expected_count = sum(
            1 for e in all_data["entries"] if e["domain"] == test_domain
        )

        logger.info(f"Filtering by domain: {test_domain} (expected {expected_count} entries)")

        # Now filter by that domain
        filtered_result = await mcp_client.call_tool(
            "ha_list_integrations", {"domain": test_domain}
        )

        filtered_data = assert_mcp_success(filtered_result, f"filter by domain {test_domain}")

        # Verify filtering worked
        assert filtered_data["total"] == expected_count, (
            f"Expected {expected_count} entries for domain {test_domain}, "
            f"got {filtered_data['total']}"
        )

        # Verify all entries are from the specified domain
        for entry in filtered_data["entries"]:
            assert entry["domain"] == test_domain, (
                f"Entry domain {entry['domain']} should match filter {test_domain}"
            )

        # Verify filters applied
        assert filtered_data["filters_applied"]["domain"] == test_domain

        logger.info(f"Domain filter test passed: {filtered_data['total']} entries")

    async def test_filter_by_nonexistent_domain(self, mcp_client):
        """
        Test: Filter by domain that doesn't exist

        This should return empty results, not an error.
        """
        logger.info("Testing ha_list_integrations with nonexistent domain...")

        result = await mcp_client.call_tool(
            "ha_list_integrations", {"domain": "nonexistent_domain_xyz"}
        )

        data = assert_mcp_success(result, "filter by nonexistent domain")

        # Should succeed but with empty results
        assert data["total"] == 0, "Should have 0 results for nonexistent domain"
        assert len(data["entries"]) == 0, "Entries should be empty"
        assert data["filters_applied"]["domain"] == "nonexistent_domain_xyz"

        logger.info("Nonexistent domain filter test passed")

    async def test_filter_by_type(self, mcp_client):
        """
        Test: Filter integrations by type

        This tests the type_filter parameter which groups integrations
        by their function (hub, device, service, etc.).
        """
        logger.info("Testing ha_list_integrations with type filter...")

        # Test system type filter
        result = await mcp_client.call_tool(
            "ha_list_integrations", {"type_filter": "system"}
        )

        data = assert_mcp_success(result, "filter by type 'system'")

        # Verify filters applied
        assert data["filters_applied"]["type_filter"] == "system"

        # Log results - even if empty, filter should work
        logger.info(f"System type filter returned {data['total']} entries")

        if data["entries"]:
            domains = [e["domain"] for e in data["entries"]]
            logger.info(f"System domains found: {domains}")

        logger.info("Type filter test passed")

    async def test_combined_filters(self, mcp_client):
        """
        Test: Combine domain and type filters

        Both filters should be applied together.
        """
        logger.info("Testing ha_list_integrations with combined filters...")

        result = await mcp_client.call_tool(
            "ha_list_integrations",
            {"domain": "homeassistant", "type_filter": "system"},
        )

        data = assert_mcp_success(result, "combined filters")

        # Verify both filters are recorded
        assert data["filters_applied"]["domain"] == "homeassistant"
        assert data["filters_applied"]["type_filter"] == "system"

        logger.info(f"Combined filters returned {data['total']} entries")

    async def test_integration_states(self, mcp_client):
        """
        Test: Verify integration state information

        Check that we can see different integration states.
        """
        logger.info("Testing integration state information...")

        result = await mcp_client.call_tool("ha_list_integrations", {})
        data = assert_mcp_success(result, "get integrations for state check")

        state_summary = data["state_summary"]

        # Log the states we found
        logger.info(f"Integration states found: {list(state_summary.keys())}")

        # Most common state should be 'loaded' for working integrations
        if "loaded" in state_summary:
            logger.info(f"Loaded integrations: {state_summary['loaded']}")

        # Check for any problematic states
        problem_states = ["setup_error", "failed_unload", "migration_error"]
        for state in problem_states:
            if state in state_summary and state_summary[state] > 0:
                logger.warning(f"Found {state_summary[state]} integrations in {state} state")

        logger.info("State information test passed")

    async def test_entry_details(self, mcp_client):
        """
        Test: Verify detailed entry information

        Check that all expected fields are present and have valid values.
        """
        logger.info("Testing detailed entry information...")

        result = await mcp_client.call_tool("ha_list_integrations", {})
        data = assert_mcp_success(result, "get integrations for detail check")

        if data["total"] == 0:
            pytest.skip("No integrations available to check details")

        # Check each entry has required fields with valid types
        for entry in data["entries"]:
            # entry_id should be a string
            assert isinstance(entry["entry_id"], str), "entry_id should be string"
            assert len(entry["entry_id"]) > 0, "entry_id should not be empty"

            # domain should be a string
            assert isinstance(entry["domain"], str), "domain should be string"
            assert len(entry["domain"]) > 0, "domain should not be empty"

            # title should be a string (can be empty in some cases)
            assert isinstance(entry["title"], str), "title should be string"

            # state should be a string
            assert isinstance(entry["state"], str), "state should be string"

            # source should be a string
            assert isinstance(entry["source"], str), "source should be string"

            # supports_options should be boolean
            assert isinstance(entry["supports_options"], bool), (
                "supports_options should be boolean"
            )

            # supports_unload should be boolean
            assert isinstance(entry["supports_unload"], bool), (
                "supports_unload should be boolean"
            )

            # disabled_by can be None or string
            assert entry["disabled_by"] is None or isinstance(entry["disabled_by"], str), (
                "disabled_by should be None or string"
            )

        logger.info(f"All {data['total']} entries have valid structure")


@pytest.mark.integrations
async def test_integration_discovery(mcp_client):
    """
    Test: Basic integration discovery

    Quick smoke test to verify the integration listing tool works.
    """
    logger.info("Testing basic integration discovery...")

    result = await mcp_client.call_tool("ha_list_integrations", {})
    data = parse_mcp_result(result)

    # Handle nested data structure
    if "data" in data:
        actual_data = data["data"]
    else:
        actual_data = data

    assert actual_data.get("success"), f"Integration listing failed: {actual_data.get('error')}"
    assert "entries" in actual_data, "Response should contain entries"

    logger.info(f"Integration discovery test passed: found {actual_data['total']} integrations")
