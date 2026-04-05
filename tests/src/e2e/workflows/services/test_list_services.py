"""
Service Discovery E2E Tests

Tests the ha_list_services tool for discovering available Home Assistant
services/actions with their parameters.
"""

import logging

import pytest

from ...utilities.assertions import (
    assert_mcp_success,
    parse_mcp_result,
)

logger = logging.getLogger(__name__)


@pytest.mark.services
class TestServiceDiscovery:
    """Test service discovery functionality."""

    async def test_list_all_services(self, mcp_client):
        """
        Test: List all available services without filters (paginated, summary mode).

        Validates default pagination (limit=50) and summary mode behavior.
        """
        logger.info("Testing: List all services (default pagination)")

        result = await mcp_client.call_tool("ha_list_services", {})
        data = assert_mcp_success(result, "list all services")

        # Should have domains
        domains = data.get("domains", [])
        assert len(domains) > 0, "Should return at least one domain"
        logger.info(f"Found {len(domains)} domains")

        # Should have pagination metadata
        total_count = data.get("total_count", 0)
        assert total_count > 0, "Should return at least one service"
        assert "has_more" in data, "Should include has_more pagination field"
        assert "count" in data, "Should include count pagination field"
        assert "offset" in data, "Should include offset pagination field"
        assert "limit" in data, "Should include limit pagination field"

        # Default limit is 50, so count should be <= 50
        count = data.get("count", 0)
        assert count <= 50, f"Default page should have at most 50 services, got {count}"

        # Default detail_level is summary — services should not have fields
        services = data.get("services", {})
        if services:
            first_service = next(iter(services.values()))
            assert "name" in first_service, "Summary should include name"
            assert "description" in first_service, "Summary should include description"
            assert "fields" not in first_service, "Summary mode should omit fields"

        logger.info(f"Found {total_count} total, {count} in page")
        logger.info("All services list test passed")

    async def test_filter_by_domain(self, mcp_client):
        """
        Test: Filter services by domain

        Validates that domain filtering works correctly and returns
        only services from the specified domain.
        """
        logger.info("Testing: Filter services by domain")

        # Test with 'light' domain
        result = await mcp_client.call_tool(
            "ha_list_services",
            {"domain": "light"},
        )
        data = assert_mcp_success(result, "filter by light domain")

        services = data.get("services", {})
        domains = data.get("domains", [])

        # Should only return light domain
        assert "light" in domains, "Light domain should be present"
        assert len(domains) == 1, f"Should only have light domain, got: {domains}"

        # All services should be from light domain
        for service_key in services.keys():
            assert service_key.startswith("light."), (
                f"Service {service_key} should be from light domain"
            )

        # Should have common light services
        light_services = list(services.keys())
        logger.info(f"Found {len(light_services)} light services: {light_services[:5]}")

        # Check that turn_on exists (common service)
        if "light.turn_on" in services:
            turn_on = services["light.turn_on"]
            assert "name" in turn_on, "Service should have name"
            # Default is summary mode — fields are omitted
            assert "fields" not in turn_on, "Summary mode should omit fields"

        logger.info("Domain filter test passed")

    async def test_filter_by_query(self, mcp_client):
        """
        Test: Filter services by search query

        Validates that query-based filtering works correctly,
        matching against service names and descriptions.
        """
        logger.info("Testing: Filter services by query")

        # Search for 'turn' which should match turn_on, turn_off, etc.
        result = await mcp_client.call_tool(
            "ha_list_services",
            {"query": "turn"},
        )
        data = assert_mcp_success(result, "filter by query 'turn'")

        services = data.get("services", {})
        total_count = data.get("total_count", 0)

        assert total_count > 0, "Should find services matching 'turn'"
        logger.info(f"Found {total_count} services matching 'turn'")

        # Check that results contain 'turn' in service names
        turn_services = [key for key in services.keys() if "turn" in key.lower()]
        logger.info(f"Services with 'turn' in name: {turn_services[:10]}")

        logger.info("Query filter test passed")

    async def test_combined_filters(self, mcp_client):
        """
        Test: Combine domain and query filters

        Validates that both filters work together correctly.
        """
        logger.info("Testing: Combined domain and query filters")

        # Filter by light domain and 'on' query
        result = await mcp_client.call_tool(
            "ha_list_services",
            {"domain": "light", "query": "on"},
        )
        data = assert_mcp_success(result, "combined filters")

        services = data.get("services", {})
        filters_applied = data.get("filters_applied", {})

        # Verify filters were applied
        assert filters_applied.get("domain") == "light", (
            "Domain filter should be applied"
        )
        assert filters_applied.get("query") == "on", "Query filter should be applied"

        # All services should be from light domain
        for service_key in services.keys():
            assert service_key.startswith("light."), (
                f"Service {service_key} should be from light domain"
            )

        logger.info(f"Combined filter returned {len(services)} services")
        logger.info("Combined filters test passed")

    async def test_service_field_details(self, mcp_client):
        """
        Test: Service field details are properly returned in full detail mode.

        Validates that detail_level='full' includes field definitions with
        type information, descriptions, and requirements.
        """
        logger.info("Testing: Service field details (full mode)")

        # Get light services with full detail
        result = await mcp_client.call_tool(
            "ha_list_services",
            {"domain": "light", "detail_level": "full"},
        )
        data = assert_mcp_success(result, "get light services (full)")

        services = data.get("services", {})

        # Check light.turn_on service (should have brightness, color, etc.)
        if "light.turn_on" in services:
            turn_on = services["light.turn_on"]
            fields = turn_on.get("fields", {})

            assert len(fields) > 0, "Full mode should include fields"
            logger.info(f"light.turn_on has {len(fields)} fields")

            # Check field structure
            for field_name, field_def in fields.items():
                assert "name" in field_def, f"Field {field_name} should have name"
                assert "type" in field_def, f"Field {field_name} should have type"

            # Common light.turn_on fields to check
            expected_fields = ["brightness", "brightness_pct", "color_temp_kelvin"]
            found_fields = [f for f in expected_fields if f in fields]
            logger.info(f"Found expected fields: {found_fields}")

        logger.info("Service field details test passed")

    async def test_nonexistent_domain(self, mcp_client):
        """
        Test: Filter by non-existent domain returns empty result

        Validates graceful handling of invalid domain filters.
        """
        logger.info("Testing: Non-existent domain filter")

        result = await mcp_client.call_tool(
            "ha_list_services",
            {"domain": "nonexistent_domain_xyz"},
        )
        data = assert_mcp_success(result, "nonexistent domain filter")

        services = data.get("services", {})
        total_count = data.get("total_count", 0)
        domains = data.get("domains", [])

        # Should return empty results, not an error
        assert total_count == 0, f"Should return 0 services, got {total_count}"
        assert len(services) == 0, "Services should be empty"
        assert len(domains) == 0, "Domains should be empty"

        logger.info("Non-existent domain test passed")

    async def test_query_no_matches(self, mcp_client):
        """
        Test: Query with no matches returns empty result

        Validates graceful handling of queries that match nothing.
        """
        logger.info("Testing: Query with no matches")

        result = await mcp_client.call_tool(
            "ha_list_services",
            {"query": "xyznonexistentquery123"},
        )
        data = assert_mcp_success(result, "no match query")

        total_count = data.get("total_count", 0)
        services = data.get("services", {})

        # Should return empty results, not an error
        assert total_count == 0, f"Should return 0 services, got {total_count}"
        assert len(services) == 0, "Services should be empty"

        logger.info("No matches query test passed")

    async def test_homeassistant_domain_services(self, mcp_client):
        """
        Test: Check homeassistant domain services

        The homeassistant domain contains universal services like
        turn_on, turn_off, toggle that work with any entity.
        """
        logger.info("Testing: homeassistant domain services")

        result = await mcp_client.call_tool(
            "ha_list_services",
            {"domain": "homeassistant"},
        )
        data = assert_mcp_success(result, "homeassistant domain")

        services = data.get("services", {})

        # Check for universal services
        universal_services = [
            "homeassistant.turn_on",
            "homeassistant.turn_off",
            "homeassistant.toggle",
        ]

        for service_name in universal_services:
            if service_name in services:
                logger.info(f"Found universal service: {service_name}")
                service = services[service_name]
                # These should have target info for entity selection
                assert "name" in service, f"{service_name} should have name"

        logger.info("homeassistant domain test passed")

    async def test_pagination_limit_and_offset(self, mcp_client):
        """Test that limit and offset correctly paginate service results."""
        logger.info("Testing: Pagination with limit and offset")

        # Get first page with small limit
        page1 = await mcp_client.call_tool(
            "ha_list_services",
            {"limit": 5, "offset": 0},
        )
        data1 = assert_mcp_success(page1, "page 1")

        assert data1["count"] == 5, f"Should return 5 services, got {data1['count']}"
        assert data1["offset"] == 0
        assert data1["limit"] == 5
        assert data1["has_more"] is True, "Should have more pages"
        assert data1["next_offset"] == 5

        # Get second page
        page2 = await mcp_client.call_tool(
            "ha_list_services",
            {"limit": 5, "offset": 5},
        )
        data2 = assert_mcp_success(page2, "page 2")

        assert data2["count"] == 5
        assert data2["offset"] == 5

        # Pages should not overlap
        keys1 = set(data1["services"].keys())
        keys2 = set(data2["services"].keys())
        assert keys1.isdisjoint(keys2), "Pages should not overlap"

        logger.info("Pagination limit/offset test passed")

    async def test_detail_level_full_includes_fields(self, mcp_client):
        """Test that detail_level='full' includes field schemas."""
        logger.info("Testing: detail_level=full")

        result = await mcp_client.call_tool(
            "ha_list_services",
            {"domain": "light", "detail_level": "full"},
        )
        data = assert_mcp_success(result, "full detail")

        services = data.get("services", {})
        assert len(services) > 0, "Should return light services"

        # Full mode should include fields
        for key, svc in services.items():
            assert "fields" in svc, f"{key} should have fields in full mode"

        logger.info("Detail level full test passed")

    async def test_detail_level_summary_omits_fields(self, mcp_client):
        """Test that detail_level='summary' (default) omits field schemas."""
        logger.info("Testing: detail_level=summary")

        result = await mcp_client.call_tool(
            "ha_list_services",
            {"domain": "light", "detail_level": "summary"},
        )
        data = assert_mcp_success(result, "summary detail")

        services = data.get("services", {})
        assert len(services) > 0, "Should return light services"

        # Summary mode should omit fields
        for key, svc in services.items():
            assert "fields" not in svc, f"{key} should not have fields in summary mode"
            assert "name" in svc, f"{key} should have name"
            assert "description" in svc, f"{key} should have description"

        logger.info("Detail level summary test passed")


@pytest.mark.services
async def test_service_discovery_integration(mcp_client):
    """
    Test: Service discovery integrates with other tools

    Demonstrates the workflow of:
    1. Discovering available services
    2. Getting details about a specific service
    3. Using ha_call_service with discovered parameters
    """
    logger.info("Testing: Service discovery integration workflow")

    # Step 1: Discover light services (use full detail to see fields)
    services_result = await mcp_client.call_tool(
        "ha_list_services",
        {"domain": "light", "detail_level": "full"},
    )
    services_data = assert_mcp_success(services_result, "discover light services")

    services = services_data.get("services", {})
    logger.info(f"Discovered {len(services)} light services")

    # Step 2: Find a light entity to test with
    search_result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "light", "domain_filter": "light", "limit": 1},
    )
    search_data = parse_mcp_result(search_result)

    # Handle nested data structure
    if "data" in search_data:
        results = search_data.get("data", {}).get("results", [])
    else:
        results = search_data.get("results", [])

    if not results:
        logger.info("No light entities available, skipping call test")
        return

    test_light = results[0].get("entity_id")
    logger.info(f"Using test light: {test_light}")

    # Step 3: Use discovered service to control light
    # First get current state
    state_result = await mcp_client.call_tool(
        "ha_get_state",
        {"entity_id": test_light},
    )
    state_data = parse_mcp_result(state_result)
    current_state = state_data.get("data", {}).get("state", "unknown")
    logger.info(f"Current light state: {current_state}")

    # Step 4: Call discovered service
    call_result = await mcp_client.call_tool(
        "ha_call_service",
        {
            "domain": "light",
            "service": "turn_on",  # Discovered from ha_list_services
            "entity_id": test_light,
        },
    )
    assert_mcp_success(call_result, "call discovered service")

    logger.info("Successfully called discovered service")
    logger.info("Service discovery integration test passed")
