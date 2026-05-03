"""
Integration Listing E2E Tests

Tests the ha_get_integration tool for listing and filtering
Home Assistant config entries (integrations).

Note: Tests are designed to work with the Docker test environment.
The actual integrations available will vary based on the test setup.
"""

import logging
from typing import ClassVar

import pytest

from ...utilities.assertions import assert_mcp_success, safe_call_tool

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
        Test: List all integrations without filters (paginated).

        This test validates that we can retrieve integrations
        from Home Assistant with pagination metadata.
        """
        logger.info("Testing ha_get_integration without filters...")

        result = await mcp_client.call_tool("ha_get_integration", {})

        data = assert_mcp_success(result, "list all integrations")

        # Verify response structure with pagination
        assert "total_count" in data, "Response should include total_count"
        assert "entries" in data, "Response should include entries list"
        assert "state_summary" in data, "Response should include state summary"
        assert "query" in data, "Response should include query field"
        assert "has_more" in data, "Response should include has_more"
        assert "offset" in data, "Response should include offset"
        assert "limit" in data, "Response should include limit"
        assert "count" in data, "Response should include count"

        total_count = data["total_count"]
        entries = data["entries"]
        count = data["count"]
        state_summary = data["state_summary"]

        logger.info(f"Found {total_count} integrations, {count} in page")
        logger.info(f"State summary: {state_summary}")

        # In a fresh test environment, there should be at least some integrations
        assert total_count >= 0, "Total should be non-negative"
        assert isinstance(entries, list), "Entries should be a list"
        assert count == len(entries), f"Count mismatch: {count} vs {len(entries)}"
        assert count <= 50, f"Default page should have at most 50, got {count}"

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

            logger.info(
                f"Sample entry: domain={entry['domain']}, state={entry['state']}"
            )

        # Verify state_summary covers all entries (not just paginated page)
        total_from_summary = sum(state_summary.values())
        assert total_from_summary == total_count, (
            f"State summary total ({total_from_summary}) should match total_count ({total_count})"
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

        if all_data["total_count"] == 0:
            pytest.skip("No integrations available to test query search")

        # Find a domain that has entries
        test_domain = all_data["entries"][0]["domain"]

        logger.info(f"Searching by query: {test_domain}")

        # Now search by that domain
        search_result = await mcp_client.call_tool(
            "ha_get_integration", {"query": test_domain}
        )

        search_data = assert_mcp_success(
            search_result, f"search by query {test_domain}"
        )

        # Fuzzy search should find at least the matching domain(s)
        assert search_data["total_count"] > 0, (
            f"Expected at least 1 entry for query {test_domain}, "
            f"got {search_data['total_count']}"
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

        logger.info(f"Query search test passed: {search_data['total_count']} entries")

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
        assert data["total_count"] == 0, "Should have 0 results for nonexistent query"
        assert len(data["entries"]) == 0, "Entries should be empty"
        assert data["query"] == "nonexistent_integration_xyz_12345"

        logger.info("Nonexistent query search test passed")

    async def test_pagination_limit_and_offset(self, mcp_client):
        """Test that limit/offset pagination works for integration listing."""
        logger.info("Testing integration pagination...")

        # Get first page with small limit
        page1_result = await mcp_client.call_tool(
            "ha_get_integration",
            {"limit": 2, "offset": 0},
        )
        page1 = assert_mcp_success(page1_result, "page 1")

        if page1["total_count"] < 3:
            pytest.skip("Not enough integrations to test pagination")

        assert page1["count"] == 2
        assert page1["offset"] == 0
        assert page1["has_more"] is True
        assert page1["next_offset"] == 2

        # Get second page
        page2_result = await mcp_client.call_tool(
            "ha_get_integration",
            {"limit": 2, "offset": 2},
        )
        page2 = assert_mcp_success(page2_result, "page 2")
        assert page2["offset"] == 2

        # Pages should not overlap
        ids1 = {e["entry_id"] for e in page1["entries"]}
        ids2 = {e["entry_id"] for e in page2["entries"]}
        assert ids1.isdisjoint(ids2), "Pages should not overlap"

        logger.info("Integration pagination test passed")

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
                logger.warning(
                    f"Found {state_summary[state]} integrations in {state} state"
                )

        logger.info("State information test passed")

    async def test_entry_details(self, mcp_client):
        """
        Test: Verify detailed entry information

        Check that all expected fields are present and have valid values.
        """
        logger.info("Testing detailed entry information...")

        result = await mcp_client.call_tool("ha_get_integration", {})
        data = assert_mcp_success(result, "get integrations for detail check")

        if data["total_count"] == 0:
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
            assert entry["disabled_by"] is None or isinstance(
                entry["disabled_by"], str
            ), "disabled_by should be None or string"

        logger.info(f"All {data['total_count']} entries have valid structure")


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

        if all_data["total_count"] == 0:
            pytest.skip("No integrations available to test domain filter")

        # Pick a domain that exists
        test_domain = all_data["entries"][0]["domain"]

        result = await mcp_client.call_tool(
            "ha_get_integration", {"domain": test_domain}
        )
        data = assert_mcp_success(result, f"filter by domain {test_domain}")

        assert data["total_count"] > 0, f"Expected entries for domain {test_domain}"
        assert data.get("domain_filter") == test_domain

        # All entries should be the filtered domain
        for entry in data["entries"]:
            assert entry["domain"] == test_domain, (
                f"Expected domain {test_domain}, got {entry['domain']}"
            )

        # Domain filter auto-enables options inclusion
        for entry in data["entries"]:
            assert "options" in entry, "Domain filter should include options"

        logger.info(
            f"Domain filter test passed: {data['total_count']} {test_domain} entries"
        )

    async def test_filter_by_nonexistent_domain(self, mcp_client):
        """
        Test: Filter by domain that doesn't exist returns empty results.
        """
        result = await mcp_client.call_tool(
            "ha_get_integration", {"domain": "nonexistent_domain_xyz"}
        )
        data = assert_mcp_success(result, "filter by nonexistent domain")

        assert data["total_count"] == 0, "Should have 0 results for nonexistent domain"
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

        if data["total_count"] == 0:
            pytest.skip("No integrations available")

        # All entries should have options field
        for entry in data["entries"]:
            assert "options" in entry, "include_options should add options field"

        logger.info(
            f"include_options test passed: {data['total_count']} entries with options"
        )

    async def test_specific_entry_includes_options(self, mcp_client):
        """
        Test: Getting a specific entry by entry_id returns full data including options.

        This validates the audit use case from issue #462 - being able to
        retrieve template definitions and other config entry options.
        """
        logger.info("Testing specific entry includes options...")

        # Find an entry that actually has options to validate the audit use case
        list_result = await mcp_client.call_tool(
            "ha_get_integration", {"include_options": True}
        )
        list_data = assert_mcp_success(list_result, "list with options")

        target_entry = next((e for e in list_data["entries"] if e.get("options")), None)
        if not target_entry:
            pytest.skip("No integrations with non-empty options found")

        entry_id = target_entry["entry_id"]

        result = await mcp_client.call_tool(
            "ha_get_integration", {"entry_id": entry_id}
        )
        data = assert_mcp_success(result, "get specific entry")

        assert "entry" in data, "Should have entry data"
        entry = data["entry"]

        # The raw REST API response should include these fields
        assert "entry_id" in entry
        assert "domain" in entry

        # Verify options are present and match what the list endpoint returned
        assert "options" in entry, "Specific entry should include options"
        assert entry["options"] == target_entry["options"], (
            "Options from specific entry should match list endpoint"
        )

        logger.info(
            f"Specific entry test passed: domain={entry.get('domain')}, "
            f"options_keys={list(entry['options'].keys())}"
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

    logger.info(
        f"Integration discovery test passed: found {data['total_count']} integrations"
    )


@pytest.mark.integrations
class TestIntegrationLogLevel:
    """Verify ha_get_integration surfaces per-integration log levels (#956)."""

    _ACCEPTED: ClassVar[set[str]] = {
        "DEFAULT",
        "NOTSET",
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }

    async def test_list_entries_include_log_level(self, mcp_client):
        """Every listed entry should expose log_level + log_level_raw."""
        result = await mcp_client.call_tool("ha_get_integration", {})
        data = assert_mcp_success(result, "list with log_level")

        if data["total_count"] == 0:
            pytest.skip("No integrations available to check log_level")

        for entry in data["entries"]:
            assert "log_level" in entry, (
                f"Entry {entry.get('domain')} missing log_level field"
            )
            assert isinstance(entry["log_level"], str), "log_level must be a string"
            # Accept canonical names, "DEFAULT", or "LEVEL_<n>" for non-standard ints
            assert (
                entry["log_level"] in self._ACCEPTED
                or entry["log_level"].startswith("LEVEL_")
            ), f"Unexpected log_level value: {entry['log_level']}"

            assert "log_level_raw" in entry, (
                f"Entry {entry.get('domain')} missing log_level_raw field"
            )
            raw = entry["log_level_raw"]
            assert raw is None or isinstance(raw, int), (
                f"log_level_raw must be int or None, got {type(raw).__name__}"
            )
            # When no override is set, log_level is DEFAULT and raw is None.
            if entry["log_level"] == "DEFAULT":
                assert raw is None, "DEFAULT log_level should pair with raw=None"

    async def test_single_entry_includes_log_level(self, mcp_client):
        """Single-entry response surfaces the log_level + log_level_raw fields.

        Round-trip against logger.set_level is already covered by
        test_logs_logger_source_reflects_set_level in tests/src/e2e/tools/test_logbook.py.
        Here we just verify the single-entry code path populates the field —
        flipping levels concurrently with other workers proved too flaky to
        assert a specific value.
        """
        list_result = await mcp_client.call_tool("ha_get_integration", {})
        list_data = assert_mcp_success(list_result, "fetch integrations")
        if list_data["total_count"] == 0:
            pytest.skip("No integrations available")

        entry_id = list_data["entries"][0]["entry_id"]
        single_result = await mcp_client.call_tool(
            "ha_get_integration", {"entry_id": entry_id}
        )
        single_data = assert_mcp_success(single_result, "single entry w/ log_level")

        assert "log_level" in single_data, "Single entry should include log_level"
        level = single_data["log_level"]
        assert isinstance(level, str), "log_level must be a string"
        assert (
            level in self._ACCEPTED or level.startswith("LEVEL_")
        ), f"Unexpected log_level value: {level}"

        assert "log_level_raw" in single_data, (
            "Single entry should include log_level_raw"
        )
        raw = single_data["log_level_raw"]
        assert raw is None or isinstance(raw, int), (
            f"log_level_raw must be int or None, got {type(raw).__name__}"
        )
        if level == "DEFAULT":
            assert raw is None, "DEFAULT log_level should pair with raw=None"


@pytest.mark.integrations
class TestGetIntegrationNegativeInputs:
    """
    A2 negative-input tests for ha_get_integration's single-entry lookup mode.

    Covers the nonexistent-entry_id failure path (entry_id provided, no match
    in the config-entry registry). The existing tests in this file exercise
    the list/query/domain-filter modes, which return empty results rather
    than errors — that is A4-style behavior. The entry_id branch goes through
    a distinct error path and was previously untested.

    Methodology: source-verified against tools_integrations.py. When
    _get_single_entry encounters a 404 from the underlying REST/WebSocket
    call, raise_tool_error is invoked with ErrorCode.RESOURCE_NOT_FOUND and
    the message "Config entry not found: ...".
    """

    async def test_get_integration_nonexistent_entry_id(self, mcp_client):
        """
        Test: ha_get_integration(entry_id="<nonexistent>") returns a
        structured error with code RESOURCE_NOT_FOUND, not success=True.

        Source path: tools_integrations.py — _get_single_entry catches a
        404/not-found exception and raises RESOURCE_NOT_FOUND.
        """
        data = await safe_call_tool(
            mcp_client,
            "ha_get_integration",
            {"entry_id": "nonexistent_entry_a2_e2e_xyz_404"},
        )

        assert not data.get("success"), (
            f"Expected failure for nonexistent entry_id, got success=True: {data}"
        )
        assert data["error"]["code"] == "RESOURCE_NOT_FOUND", (
            f"Expected error code RESOURCE_NOT_FOUND, got: {data['error']}"
        )
        assert "suggestion" in data["error"], (
            "Error response should include a suggestion"
        )
        error_msg = data["error"]["message"].lower()
        assert "not found" in error_msg, (
            f"Expected 'not found' in error message, got: {data['error']}"
        )
