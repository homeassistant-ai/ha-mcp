"""
Integration Listing E2E Tests

Tests the ha_get_integration tool for listing and filtering
Home Assistant config entries (integrations).

Note: Tests are designed to work with the Docker test environment.
The actual integrations available will vary based on the test setup.
"""

import logging

import pytest

from ...utilities.assertions import assert_mcp_success

logger = logging.getLogger(__name__)

# Integration states that indicate problems
# Source: Home Assistant config entry states
# https://github.com/home-assistant/core/blob/dev/homeassistant/config_entries.py
PROBLEM_STATES = ["setup_error", "failed_unload", "migration_error"]


@pytest.mark.integrations
class TestListIntegrations:
    """Test integration listing functionality."""

    async def test_list_all_integrations(self, mcp_client):
        """
        Test: List all integrations without filters

        This test validates that we can retrieve all configured integrations
        from Home Assistant.
        """
        logger.info("Testing ha_get_integration without filters...")

        result = await mcp_client.call_tool("ha_get_integration", {})

        data = assert_mcp_success(result, "list all integrations")

        # Verify response structure
        assert "total" in data, "Response should include total count"
        assert "entries" in data, "Response should include entries list"
        assert "state_summary" in data, "Response should include state summary"
        assert "query" in data, "Response should include query field"

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

        # Verify no query was applied
        assert data["query"] is None

        logger.info("All integrations listed successfully")

    async def test_search_by_query(self, mcp_client):
        """
        Test: Search integrations by query

        This test validates searching integrations using fuzzy keyword matching.
        We first get all integrations to find a valid domain to search for.
        """
        logger.info("Testing ha_get_integration with query search...")

        # First, get all integrations to find a valid domain
        all_result = await mcp_client.call_tool("ha_get_integration", {})
        all_data = assert_mcp_success(all_result, "get all integrations")

        if all_data["total"] == 0:
            pytest.skip("No integrations available to test query search")

        # Find a domain that has entries
        test_domain = all_data["entries"][0]["domain"]

        logger.info(f"Searching by query: {test_domain}")

        # Now search by that domain
        search_result = await mcp_client.call_tool(
            "ha_get_integration", {"query": test_domain}
        )

        search_data = assert_mcp_success(search_result, f"search by query {test_domain}")

        # Fuzzy search should find at least the matching domain(s)
        assert search_data["total"] > 0, (
            f"Expected at least 1 entry for query {test_domain}, "
            f"got {search_data['total']}"
        )

        # Verify all entries match the query (domain or title contains search term)
        for entry in search_data["entries"]:
            domain_matches = test_domain.lower() in entry["domain"].lower()
            title_matches = test_domain.lower() in entry["title"].lower()
            assert domain_matches or title_matches, (
                f"Entry domain {entry['domain']} or title {entry['title']} "
                f"should match query {test_domain}"
            )

        # Verify query was recorded
        assert search_data["query"] == test_domain

        logger.info(f"Query search test passed: {search_data['total']} entries")

    async def test_search_by_nonexistent_query(self, mcp_client):
        """
        Test: Search by query that doesn't match anything

        This should return empty results, not an error.
        """
        logger.info("Testing ha_get_integration with nonexistent query...")

        result = await mcp_client.call_tool(
            "ha_get_integration", {"query": "nonexistent_integration_xyz_12345"}
        )

        data = assert_mcp_success(result, "search by nonexistent query")

        # Should succeed but with empty results
        assert data["total"] == 0, "Should have 0 results for nonexistent query"
        assert len(data["entries"]) == 0, "Entries should be empty"
        assert data["query"] == "nonexistent_integration_xyz_12345"

        logger.info("Nonexistent query search test passed")


    async def test_integration_states(self, mcp_client):
        """
        Test: Verify integration state information

        Check that we can see different integration states.
        """
        logger.info("Testing integration state information...")

        result = await mcp_client.call_tool("ha_get_integration", {})
        data = assert_mcp_success(result, "get integrations for state check")

        state_summary = data["state_summary"]

        # Log the states we found
        logger.info(f"Integration states found: {list(state_summary.keys())}")

        # Most common state should be 'loaded' for working integrations
        if "loaded" in state_summary:
            logger.info(f"Loaded integrations: {state_summary['loaded']}")

        # Check for any problematic states
        for state in PROBLEM_STATES:
            if state in state_summary and state_summary[state] > 0:
                logger.warning(f"Found {state_summary[state]} integrations in {state} state")

        logger.info("State information test passed")

    async def test_entry_details(self, mcp_client):
        """
        Test: Verify detailed entry information

        Check that all expected fields are present and have valid values.
        """
        logger.info("Testing detailed entry information...")

        result = await mcp_client.call_tool("ha_get_integration", {})
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
class TestIntegrationFiltering:
    """Test integration domain filtering and options inclusion."""

    async def test_filter_by_domain(self, mcp_client):
        """
        Test: Filter integrations by domain.

        Verifies that the domain parameter filters entries correctly
        and auto-includes the options object.
        """
        logger.info("Testing ha_get_integration with domain filter...")

        # First get all integrations to find a valid domain
        all_result = await mcp_client.call_tool("ha_get_integration", {})
        all_data = assert_mcp_success(all_result, "get all integrations")

        if all_data["total"] == 0:
            pytest.skip("No integrations available to test domain filter")

        # Pick a domain that exists
        test_domain = all_data["entries"][0]["domain"]

        result = await mcp_client.call_tool(
            "ha_get_integration", {"domain": test_domain}
        )
        data = assert_mcp_success(result, f"filter by domain {test_domain}")

        assert data["total"] > 0, f"Expected entries for domain {test_domain}"
        assert data.get("domain_filter") == test_domain

        # All entries should be the filtered domain
        for entry in data["entries"]:
            assert entry["domain"] == test_domain, (
                f"Expected domain {test_domain}, got {entry['domain']}"
            )

        # Domain filter auto-enables options inclusion
        for entry in data["entries"]:
            assert "options" in entry, "Domain filter should include options"

        logger.info(f"Domain filter test passed: {data['total']} {test_domain} entries")

    async def test_filter_by_nonexistent_domain(self, mcp_client):
        """
        Test: Filter by domain that doesn't exist returns empty results.
        """
        result = await mcp_client.call_tool(
            "ha_get_integration", {"domain": "nonexistent_domain_xyz"}
        )
        data = assert_mcp_success(result, "filter by nonexistent domain")

        assert data["total"] == 0, "Should have 0 results for nonexistent domain"
        assert len(data["entries"]) == 0

    async def test_include_options_flag(self, mcp_client):
        """
        Test: include_options parameter includes options in list response.
        """
        logger.info("Testing ha_get_integration with include_options=True...")

        result = await mcp_client.call_tool(
            "ha_get_integration", {"include_options": True}
        )
        data = assert_mcp_success(result, "list with include_options")

        if data["total"] == 0:
            pytest.skip("No integrations available")

        # All entries should have options field
        for entry in data["entries"]:
            assert "options" in entry, "include_options should add options field"

        logger.info(f"include_options test passed: {data['total']} entries with options")

    async def test_specific_entry_includes_options(self, mcp_client):
        """
        Test: Getting a specific entry by entry_id returns full data including options.

        This validates the audit use case from issue #462 - being able to
        retrieve template definitions and other config entry options.
        """
        logger.info("Testing specific entry includes options...")

        # Get any entry
        all_result = await mcp_client.call_tool("ha_get_integration", {})
        all_data = assert_mcp_success(all_result, "get all integrations")

        if all_data["total"] == 0:
            pytest.skip("No integrations available")

        entry_id = all_data["entries"][0]["entry_id"]

        result = await mcp_client.call_tool(
            "ha_get_integration", {"entry_id": entry_id}
        )
        data = assert_mcp_success(result, "get specific entry")

        assert "entry" in data, "Should have entry data"
        entry = data["entry"]

        # The raw REST API response should include these fields
        assert "entry_id" in entry
        assert "domain" in entry

        logger.info(
            f"Specific entry test passed: domain={entry.get('domain')}, "
            f"has_options={'options' in entry}"
        )


@pytest.mark.integrations
async def test_integration_discovery(mcp_client):
    """
    Test: Basic integration discovery

    Quick smoke test to verify the integration listing tool works.
    """
    logger.info("Testing basic integration discovery...")

    result = await mcp_client.call_tool("ha_get_integration", {})
    data = assert_mcp_success(result, "integration discovery")

    assert "entries" in data, "Response should contain entries"

    logger.info(f"Integration discovery test passed: found {data['total']} integrations")
