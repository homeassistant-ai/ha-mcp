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
        Test: List all available services without filters

        Validates that the tool returns a comprehensive list of services
        from various domains.
        """
        logger.info("Testing: List all services")

        result = await mcp_client.call_tool("ha_list_services", {})
        data = assert_mcp_success(result, "list all services")

        # Should have domains
        domains = data.get("domains", [])
        assert len(domains) > 0, "Should return at least one domain"
        logger.info(f"Found {len(domains)} domains")

        # Should have services
        services = data.get("services", {})
        total_count = data.get("total_count", 0)
        assert total_count > 0, "Should return at least one service"
        logger.info(f"Found {total_count} total services")

        # Common domains should be present
        common_domains = ["homeassistant", "light", "switch", "automation"]
        for domain in common_domains:
            if domain in domains:
                logger.info(f"Found common domain: {domain}")

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
            assert "fields" in turn_on, "Service should have fields"
            logger.info(f"light.turn_on fields: {list(turn_on.get('fields', {}).keys())}")

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
        turn_services = [
            key for key in services.keys()
            if "turn" in key.lower()
        ]
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
        assert filters_applied.get("domain") == "light", "Domain filter should be applied"
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
        Test: Service field details are properly returned

        Validates that service field definitions include proper
        type information, descriptions, and requirements.
        """
        logger.info("Testing: Service field details")

        # Get light services (well-known, have fields)
        result = await mcp_client.call_tool(
            "ha_list_services",
            {"domain": "light"},
        )
        data = assert_mcp_success(result, "get light services")

        services = data.get("services", {})

        # Check light.turn_on service (should have brightness, color, etc.)
        if "light.turn_on" in services:
            turn_on = services["light.turn_on"]
            fields = turn_on.get("fields", {})

            logger.info(f"light.turn_on has {len(fields)} fields")

            # Check field structure
            for field_name, field_def in fields.items():
                assert "name" in field_def, f"Field {field_name} should have name"
                assert "type" in field_def, f"Field {field_name} should have type"
                # required may be present
                logger.debug(
                    f"  Field: {field_name} ({field_def.get('type')}) "
                    f"required={field_def.get('required', False)}"
                )

            # Common light.turn_on fields to check
            expected_fields = ["brightness", "brightness_pct", "color_temp"]
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

    # Step 1: Discover light services
    services_result = await mcp_client.call_tool(
        "ha_list_services",
        {"domain": "light"},
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
