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
            # Default is summary mode — fields are omitted even with domain filter
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

        Validates that detail_level='full' includes Core's field schemas with
        translated names, selector definitions and section entries.
        """
        logger.info("Testing: Service field details (full mode)")

        # Get light services with full detail
        result = await mcp_client.call_tool(
            "ha_list_services",
            {"domain": "light", "detail_level": "full"},
        )
        data = assert_mcp_success(result, "get light services (full)")

        services = data.get("services", {})

        # light.turn_on is the reference full-detail entry: Core's own
        # light/services.yaml defines it, so its absence must fail rather than
        # skip the checks below.
        assert "light.turn_on" in services, (
            f"light domain must list turn_on, got {sorted(services)}"
        )
        turn_on = services["light.turn_on"]
        fields = turn_on.get("fields", {})

        assert len(fields) > 0, "Full mode should include fields"
        logger.info(f"light.turn_on has {len(fields)} fields")

        # Check field structure: Core's selector replaces the hand-derived type.
        for field_name, field_def in fields.items():
            assert "name" in field_def, f"Field {field_name} should have name"
            if "fields" in field_def:
                assert field_def["fields"], (
                    f"Section {field_name} should carry nested fields"
                )
            else:
                assert "selector" in field_def, (
                    f"Field {field_name} should carry Core's selector"
                )
            assert "type" not in field_def, (
                f"Field {field_name} should not carry a hand-derived type"
            )

        # The collapsed section Core serves for light.turn_on is preserved.
        # Required, not conditional: `additional_fields` ships in Core's own
        # light/services.yaml, and a silent absence would leave this check green
        # while the section shaping broke.
        additional = fields["additional_fields"]
        assert additional.get("collapsed") is True
        additional_fields = additional.get("fields", {})
        assert "rgbw_color" in additional_fields, (
            f"section fields should carry Core's keys, got {sorted(additional_fields)}"
        )
        assert "color_name" in additional_fields
        assert "brightness" in additional_fields

        # Core's common light.turn_on fields must survive the shaping.
        # `brightness` is a section field inside `additional_fields`, not a
        # top-level one, so only the top-level pair is asserted here; the
        # section itself is checked above.
        expected_fields = {"brightness_pct", "color_temp_kelvin"}
        assert expected_fields <= set(fields), (
            f"light.turn_on is missing Core fields: {sorted(expected_fields - set(fields))}"
        )

        logger.info("Service field details test passed")

    async def test_service_text_and_response_come_from_core(self, mcp_client):
        """Core's translated text and response shape reach the catalog.

        A domain with a Core integration exposes translated names/descriptions;
        a response-capable service reports its ``response`` block so an agent
        knows it can ask ``ha_call_service`` for a return value.
        """
        logger.info("Testing: translated service text and response metadata")

        light = await mcp_client.call_tool(
            "ha_list_services",
            {"domain": "light", "detail_level": "full"},
        )
        light_data = assert_mcp_success(light, "light service text")
        turn_on = light_data.get("services", {}).get("light.turn_on")
        assert turn_on is not None, "light.turn_on should be listed"
        assert turn_on["name"].strip(), "Service name should not be empty"
        assert turn_on["description"].strip(), (
            "Core ships a translation for light.turn_on; the catalog must use it"
        )

        calendar = await mcp_client.call_tool(
            "ha_list_services",
            {"domain": "calendar"},
        )
        calendar_data = assert_mcp_success(calendar, "calendar response metadata")
        calendar_services = calendar_data.get("services", {})
        # Every lane seeds a calendar entity (see test_call_service_return_response),
        # so the calendar domain is registered: a missing entry is a failure, not a
        # reason to skip the response assertion.
        assert "calendar.get_events" in calendar_services, (
            f"calendar domain must list get_events, got {sorted(calendar_services)}"
        )
        get_events = calendar_services["calendar.get_events"]
        assert get_events.get("response") == {"optional": False}, (
            "calendar.get_events must report Core's non-optional response"
        )

        logger.info("Translated text and response metadata test passed")

    async def test_core_catalog_shape_from_kitchen_sink(self, mcp_client):
        """Core's own catalog shape reaches the tool, verified against real Core.

        kitchen_sink ships translated service, field and section text with
        description placeholders, a translated field example, a collapsed
        section and required flags; the seeded config loads it on every lane.
        These assertions live here, against the pinned Core image, rather than
        in a hand-maintained copy of Core's catalog.
        """
        logger.info("Testing: Core catalog shape from kitchen_sink")

        result = await mcp_client.call_tool(
            "ha_list_services",
            {"domain": "kitchen_sink", "detail_level": "full"},
        )
        data = assert_mcp_success(result, "kitchen_sink catalog")
        services = data.get("services", {})
        assert "kitchen_sink.test_service_1" in services, sorted(services)
        service = services["kitchen_sink.test_service_1"]

        # Service-level placeholders resolve in the name and description.
        assert service["name"] == "Test action foo"
        assert service["description"] == "Fake action for testing bar"

        fields = service["fields"]
        field_1 = fields["field_1"]
        assert field_1["name"] == "Field 1 beer"
        assert field_1["description"] == "Number of seconds milk"
        assert field_1["example"] == "Example: https://example.com"
        assert field_1["required"] is True
        assert fields["field_2"]["example"] == "Field 2 example"

        section = fields["additional_fields"]
        assert section["name"] == "Additional options"
        assert section["collapsed"] is True
        assert {"field_3", "field_4"} <= set(section["fields"])
        assert "example" not in section["fields"]["field_3"]

        for field in fields.values():
            assert "type" not in field, f"Field {field['name']} carries a derived type"

        logger.info("Core catalog shape test passed")

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

    async def test_pagination_walk_covers_every_service_once(self, mcp_client):
        """A paged walk returns every service exactly once, in a stable order.

        The catalog has more services than one page, so two different page sizes
        must produce the same concatenated sequence. Duplicates or gaps would
        make a paged discovery miss a service or call it twice.
        """

        async def _walk(limit: int) -> tuple[list[str], int]:
            seen: list[str] = []
            offset = 0
            pages = 0
            while True:
                page = await mcp_client.call_tool(
                    "ha_list_services", {"limit": limit, "offset": offset}
                )
                data = assert_mcp_success(page, f"pagination walk limit={limit}")
                keys = list(data["services"])
                assert data["count"] == len(keys)
                seen.extend(keys)
                pages += 1
                assert pages < 100, "pagination walk did not terminate"
                if not data.get("has_more"):
                    return seen, data["total_count"]
                assert data["next_offset"] == offset + data["count"]
                offset = data["next_offset"]

        small, total = await _walk(50)
        large, total_large = await _walk(200)

        assert total == total_large
        assert len(small) == total, f"walk returned {len(small)} of {total} services"
        assert len(set(small)) == len(small), "a paged walk returned a service twice"
        assert small == large, "page size changed the service order"

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
        """Test that default summary mode (no filters) omits field schemas."""
        logger.info("Testing: detail_level=summary (no filters)")

        # No domain/query filter — stays in summary mode
        result = await mcp_client.call_tool(
            "ha_list_services",
            {"limit": 10},
        )
        data = assert_mcp_success(result, "summary detail")

        services = data.get("services", {})
        assert len(services) > 0, "Should return services"

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
        "ha_search",
        {"query": "light", "domain_filter": "light", "limit": 1},
    )
    search_data = parse_mcp_result(search_result)

    # Handle nested data structure
    results = search_data.get("entities", [])

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
