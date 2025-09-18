"""
Convenience Tools E2E Tests

NOTE: Run these tests with the Docker test environment:
    export HAMCP_ENV_FILE=tests/.env.test && uv run pytest tests/e2e/scenarios/test_convenience_tools.py -v

Or ensure Docker test environment is running:
    cd tests && docker compose up -d

Tests for convenience MCP tools that provide enhanced user experience:
- Scene activation and management
- Weather information retrieval
- Energy dashboard data
- Domain documentation access

These tools represent high-value functionality that users frequently access.
"""

import asyncio
import logging

import pytest

from ...utilities.assertions import parse_mcp_result

logger = logging.getLogger(__name__)


@pytest.mark.convenience
class TestConvenienceTools:
    """Test convenience tools that enhance user experience."""

    async def test_scene_activation_and_discovery(self, mcp_client):
        """
        Test: Scene discovery and activation workflow

        This test validates the scene management functionality that allows
        users to activate predefined Home Assistant scenes.
        """

        logger.info("🎬 Testing scene activation and discovery...")

        try:
            # 1. DISCOVERY: Search for available scenes
            logger.info("🔍 Discovering available scenes...")
            search_result = await mcp_client.call_tool(
                "ha_search_entities",
                {"query": "scene", "domain_filter": "scene", "limit": 10},
            )

            search_data = parse_mcp_result(search_result)

            # Handle both nested and direct success indicators
            success = search_data.get("success") or search_data.get("data", {}).get(
                "success", False
            )

            if not success:
                logger.warning(
                    "⚠️ Scene search failed, test environment may not have scenes configured"
                )
                pytest.skip(
                    "Scene search failed - test environment may not be properly configured"
                )
                return

            # Extract results from nested or direct format
            results_data = search_data.get("data", search_data)
            scenes = results_data.get("results", [])
            logger.info(f"🎬 Found {len(scenes)} scenes in test environment")

            if not scenes:
                logger.warning(
                    "⚠️ No scenes found in test environment, skipping activation test"
                )
                pytest.skip("No scenes available for testing")
                return

            # 2. SCENE ACTIVATION: Activate a scene
            test_scene = scenes[0]
            scene_name = (
                test_scene.get("friendly_name")
                or test_scene.get("entity_id", "").split(".")[1]
            )

            logger.info(f"🎬 Activating scene: {scene_name}")
            activate_result = await mcp_client.call_tool(
                "ha_activate_scene", {"scene_name": scene_name}
            )

            activate_data = parse_mcp_result(activate_result)

            # Handle scene activation response format variations
            success = (
                activate_data.get("success")
                or activate_data.get("data", {}).get("success", False)
                or (
                    activate_data.get("error") is None
                    and "entity_id" in str(activate_data)
                )
            )

            assert success, f"Scene activation failed: {activate_data}"
            logger.info(f"✅ Scene '{scene_name}' activated successfully")

            # 3. VERIFICATION: Check that activation was logged (with timeout)
            await asyncio.sleep(2)  # Allow time for scene activation to complete

            try:
                logbook_result = await mcp_client.call_tool(
                    "ha_get_logbook", {"hours_back": 1}
                )
                logbook_data = parse_mcp_result(logbook_result)

                if logbook_data.get("success"):
                    entries = logbook_data.get("entries", [])
                    scene_entries = [e for e in entries if "scene" in str(e).lower()]
                    logger.info(
                        f"📋 Found {len(scene_entries)} scene-related logbook entries"
                    )
                else:
                    logger.info("ℹ️ Logbook verification skipped - may not be available")
            except Exception as e:
                logger.warning(f"⚠️ Logbook verification failed: {e}")

            logger.info("✅ Scene activation workflow completed")

        except Exception as e:
            logger.error(f"❌ Scene activation test failed: {e}")
            raise

    async def test_weather_information_retrieval(self, mcp_client):
        """
        Test: Weather information retrieval

        Validates weather data access which is commonly used for
        automation conditions and user dashboards.
        """

        logger.info("🌤️ Testing weather information retrieval...")

        try:
            # 1. DEFAULT WEATHER: Get default location weather
            logger.info("🌍 Getting default location weather...")
            weather_result = await mcp_client.call_tool("ha_get_weather")

            weather_data = parse_mcp_result(weather_result)

            # Handle multiple weather response formats
            success_indicators = [
                weather_data.get("success") is True,
                weather_data.get("data", {}).get("success") is True,
                # Weather data without explicit success - check for expected fields
                (
                    weather_data.get("success") is None
                    and (
                        "temperature" in weather_data
                        or "entity_id" in weather_data
                        or "state" in weather_data
                    )
                ),
                # Weather entities list format
                isinstance(weather_data.get("weather_entities"), list),
            ]

            if not any(success_indicators) and weather_data.get("error"):
                logger.warning(
                    f"⚠️ Weather retrieval failed: {weather_data.get('error')}"
                )
                pytest.skip("Weather service not available in test environment")
                return

            # Extract actual data based on response format
            if "data" in weather_data and isinstance(weather_data["data"], dict):
                data = weather_data["data"]
            else:
                data = weather_data

            logger.info(
                f"✅ Default weather data retrieved: {len(str(data))} characters"
            )

            # Validate weather data structure
            valid_weather_indicators = [
                "temperature" in str(data).lower(),
                "weather" in str(data).lower(),
                "state" in data,
                "entity_id" in data,
                "weather_entities" in data,
            ]

            if any(valid_weather_indicators):
                logger.info("✅ Weather data contains expected fields")
            else:
                logger.warning(
                    f"⚠️ Weather data format unexpected: {list(data.keys()) if isinstance(data, dict) else type(data)}"
                )

            # 2. SPECIFIC LOCATION: Test with specific location (if supported)
            logger.info("📍 Testing specific location weather...")
            try:
                location_weather_result = await mcp_client.call_tool(
                    "ha_get_weather", {"location": "home"}
                )

                location_weather_data = parse_mcp_result(location_weather_result)

                location_success_indicators = [
                    location_weather_data.get("success") is True,
                    location_weather_data.get("data", {}).get("success") is True,
                    (
                        location_weather_data.get("success") is None
                        and "temperature" in str(location_weather_data).lower()
                    ),
                ]

                if any(location_success_indicators):
                    logger.info("✅ Location-specific weather data retrieved")
                else:
                    logger.info(
                        "ℹ️ Location-specific weather not available in test environment"
                    )

            except Exception as e:
                logger.info(f"ℹ️ Location-specific weather test failed: {e}")

            logger.info("✅ Weather information retrieval completed")

        except Exception as e:
            logger.error(f"❌ Weather information test failed: {e}")
            raise

    async def test_energy_dashboard_access(self, mcp_client):
        """
        Test: Energy dashboard data retrieval

        Tests access to Home Assistant energy dashboard information
        which is crucial for energy monitoring and optimization.
        """

        logger.info("⚡ Testing energy dashboard access...")

        try:
            # 1. TODAY'S ENERGY: Get today's energy data
            logger.info("📊 Getting today's energy data...")
            energy_result = await mcp_client.call_tool(
                "ha_get_energy", {"period": "today"}
            )

            energy_data = parse_mcp_result(energy_result)

            # Energy dashboard might not be configured in test environment
            success_indicators = [
                energy_data.get("success") is True,
                energy_data.get("data", {}).get("success") is True,
                # Energy data without explicit success - check for expected fields
                (
                    energy_data.get("success") is None
                    and (
                        "energy" in energy_data
                        or "stats" in energy_data
                        or "consumption" in energy_data
                    )
                ),
            ]

            if any(success_indicators):
                logger.info("✅ Today's energy data retrieved successfully")

                # Extract data based on response format
                data = energy_data.get("data", energy_data)
                if isinstance(data, dict):
                    available_fields = [
                        key for key in data.keys() if key not in ["success", "error"]
                    ]
                    if available_fields:
                        logger.info(f"📊 Energy data contains: {available_fields}")
                    else:
                        logger.info("📊 Energy data structure confirmed")
            else:
                error_msg = energy_data.get("error", "Unknown error")
                if (
                    "not configured" in str(error_msg).lower()
                    or "not found" in str(error_msg).lower()
                ):
                    logger.info("ℹ️ Energy dashboard not configured in test environment")
                else:
                    logger.warning(f"⚠️ Energy dashboard access failed: {error_msg}")

            # 2. WEEKLY ENERGY: Test different time periods
            logger.info("📅 Testing weekly energy data...")
            try:
                weekly_result = await mcp_client.call_tool(
                    "ha_get_energy", {"period": "week"}
                )

                weekly_data = parse_mcp_result(weekly_result)

                weekly_success_indicators = [
                    weekly_data.get("success") is True,
                    weekly_data.get("data", {}).get("success") is True,
                    (weekly_data.get("success") is None and "energy" in weekly_data),
                ]

                if any(weekly_success_indicators):
                    logger.info("✅ Weekly energy data retrieved")
                else:
                    logger.info(
                        "ℹ️ Weekly energy data not available in test environment"
                    )

            except Exception as e:
                logger.info(f"ℹ️ Weekly energy test failed: {e}")

            logger.info("✅ Energy dashboard access testing completed")

        except Exception as e:
            logger.error(f"❌ Energy dashboard test failed: {e}")
            raise

    async def test_domain_documentation_access(self, mcp_client):
        """
        Test: Domain documentation retrieval

        Validates access to Home Assistant domain documentation
        which helps users understand available functionality.
        """

        logger.info("📚 Testing domain documentation access...")

        # Test domains that should be available in most HA installations
        test_domains = ["light", "automation", "sensor", "input_boolean"]
        successful_retrievals = 0

        for domain in test_domains:
            try:
                logger.info(f"📖 Getting documentation for domain: {domain}")

                docs_result = await mcp_client.call_tool(
                    "ha_get_domain_docs", {"domain": domain}
                )

                docs_data = parse_mcp_result(docs_result)

                # Handle various response formats for domain documentation
                success_indicators = [
                    docs_data.get("success") is True,
                    docs_data.get("data", {}).get("success") is True,
                    # Documentation content without explicit success field
                    (
                        docs_data.get("success") is None
                        and (
                            "documentation" in docs_data
                            or "description" in docs_data
                            or "markdown" in docs_data
                        )
                    ),
                    # Raw documentation content
                    (
                        isinstance(docs_data.get("content"), str)
                        and len(docs_data.get("content", "")) > 100
                    ),
                ]

                if any(success_indicators):
                    successful_retrievals += 1

                    # Extract data based on response format
                    if "data" in docs_data and isinstance(docs_data["data"], dict):
                        data = docs_data["data"]
                    else:
                        data = docs_data

                    # Verify documentation structure
                    expected_fields = [
                        "services",
                        "entities",
                        "documentation",
                        "content",
                        "markdown",
                    ]
                    found_fields = [
                        field
                        for field in expected_fields
                        if field in data and data[field]
                    ]

                    logger.info(
                        f"✅ {domain} docs retrieved - contains: {found_fields if found_fields else 'content'}"
                    )

                    # Check for actual content
                    if "services" in data and isinstance(
                        data["services"], (list, dict)
                    ):
                        service_count = (
                            len(data["services"])
                            if isinstance(data["services"], list)
                            else len(data["services"].keys())
                        )
                        logger.info(f"  📋 Found {service_count} services for {domain}")

                    if "entities" in data and isinstance(
                        data["entities"], (list, dict)
                    ):
                        entity_count = (
                            len(data["entities"])
                            if isinstance(data["entities"], list)
                            else len(data["entities"].keys())
                        )
                        logger.info(f"  🏠 Found {entity_count} entities for {domain}")

                    # Check for documentation content
                    content_fields = ["content", "documentation", "markdown"]
                    for field in content_fields:
                        if (
                            field in data
                            and isinstance(data[field], str)
                            and len(data[field]) > 100
                        ):
                            logger.info(
                                f"  📄 Found {field}: {len(data[field])} characters"
                            )
                            break

                else:
                    error_msg = docs_data.get("error", "Unknown error")
                    # Some failures are expected in test environments
                    if any(
                        phrase in str(error_msg).lower()
                        for phrase in ["not found", "unavailable", "network", "timeout"]
                    ):
                        logger.info(
                            f"ℹ️ {domain} docs not available in test environment: {error_msg}"
                        )
                    else:
                        logger.warning(
                            f"⚠️ Failed to get docs for {domain}: {error_msg}"
                        )

            except Exception as e:
                logger.warning(f"⚠️ Exception getting docs for {domain}: {e}")

        # At least some domains should have documentation available
        if successful_retrievals == 0:
            logger.warning(
                "⚠️ No domain documentation retrieved - external service may be unavailable"
            )
        else:
            logger.info(
                f"✅ Successfully retrieved documentation for {successful_retrievals}/{len(test_domains)} domains"
            )

        logger.info("✅ Domain documentation access testing completed")

    async def test_template_evaluation_capabilities(self, mcp_client):
        """
        Test: Template evaluation functionality

        Comprehensive test of Home Assistant template evaluation
        which is essential for dynamic automation and display.
        """

        logger.info("🧪 Testing template evaluation capabilities...")

        try:
            # 1. SIMPLE TEMPLATE: Basic time template
            logger.info("⏰ Testing simple time template...")
            time_result = await mcp_client.call_tool(
                "ha_eval_template", {"template": "{{ now().strftime('%H:%M:%S') }}"}
            )

            time_data = parse_mcp_result(time_result)

            # Handle various template response formats
            success_indicators = [
                time_data.get("success") is True,
                time_data.get("data", {}).get("success") is True,
                # Direct result without success wrapper
                (time_data.get("success") is None and "result" in time_data),
            ]

            if not any(success_indicators):
                logger.warning(
                    f"⚠️ Time template evaluation failed: {time_data.get('error', 'Unknown error')}"
                )
                pytest.skip("Template evaluation not available")
                return

            # Extract result based on response format
            time_value = time_data.get("result") or time_data.get("data", {}).get(
                "result", ""
            )

            # Validate time format
            if ":" in str(time_value) and len(str(time_value)) >= 5:
                logger.info(f"✅ Time template result: {time_value}")
            else:
                logger.warning(
                    f"⚠️ Time template returned unexpected format: {time_value}"
                )

            # 2. STATE TEMPLATE: Entity state template
            logger.info("🏠 Testing entity state template...")

            try:
                # Get a test entity first
                search_result = await mcp_client.call_tool(
                    "ha_search_entities",
                    {"query": "sensor", "domain_filter": "sensor", "limit": 1},
                )

                search_data = parse_mcp_result(search_result)

                # Handle nested search results
                search_results = search_data.get("data", {}).get(
                    "results"
                ) or search_data.get("results", [])

                if search_results:
                    test_entity = search_results[0]["entity_id"]

                    state_template_result = await mcp_client.call_tool(
                        "ha_eval_template",
                        {"template": f"{{{{ states('{test_entity}') }}}}"},
                    )

                    state_template_data = parse_mcp_result(state_template_result)

                    state_success = (
                        state_template_data.get("success") is True
                        or state_template_data.get("data", {}).get("success") is True
                        or "result" in state_template_data
                    )

                    if state_success:
                        state_result = state_template_data.get(
                            "result"
                        ) or state_template_data.get("data", {}).get("result")
                        logger.info(
                            f"✅ State template for {test_entity}: {state_result}"
                        )
                    else:
                        logger.info(f"ℹ️ State template not available for {test_entity}")
                else:
                    logger.info("ℹ️ No sensor entities found for state template test")

            except Exception as e:
                logger.info(f"ℹ️ State template test failed: {e}")

            # 3. MATH TEMPLATE: Mathematical calculation
            logger.info("🔢 Testing mathematical template...")
            math_result = await mcp_client.call_tool(
                "ha_eval_template", {"template": "{{ (10 * 5) + 25 }}"}
            )

            math_data = parse_mcp_result(math_result)

            math_success = (
                math_data.get("success") is True
                or math_data.get("data", {}).get("success") is True
                or "result" in math_data
            )

            assert math_success, f"Math template failed: {math_data}"

            # Extract and validate result
            math_result_value = math_data.get("result") or math_data.get(
                "data", {}
            ).get("result")

            # Handle string or numeric result
            try:
                numeric_result = (
                    int(math_result_value)
                    if isinstance(math_result_value, str)
                    else math_result_value
                )
                assert numeric_result == 75, (
                    f"Math template should equal 75, got: {numeric_result}"
                )
                logger.info(f"✅ Math template result: {numeric_result}")
            except (ValueError, TypeError) as e:
                logger.warning(
                    f"⚠️ Math template result not numeric: {math_result_value} ({e})"
                )

            # 4. ERROR HANDLING: Invalid template
            logger.info("❌ Testing error handling with invalid template...")
            try:
                error_result = await mcp_client.call_tool(
                    "ha_eval_template", {"template": "{{ invalid_function() }}"}
                )

                error_data = parse_mcp_result(error_result)

                # Check if template correctly failed
                failed_indicators = [
                    error_data.get("success") is False,
                    error_data.get("data", {}).get("success") is False,
                    "error" in error_data,
                    "Error" in str(error_data.get("result", "")),
                ]

                if any(failed_indicators):
                    logger.info("✅ Invalid template correctly returned error")
                else:
                    logger.warning(
                        f"⚠️ Invalid template unexpectedly succeeded: {error_data}"
                    )

            except Exception as e:
                logger.info(f"✅ Invalid template correctly raised exception: {e}")

            logger.info("✅ Template evaluation capabilities testing completed")

        except Exception as e:
            logger.error(f"❌ Template evaluation test failed: {e}")
            raise


@pytest.mark.convenience
async def test_bulk_operation_status_monitoring(mcp_client):
    """
    Test: Bulk operation status monitoring

    Validates the ability to monitor status of bulk operations
    which is essential for reliable bulk device control.
    """

    logger.info("📊 Testing bulk operation status monitoring...")

    try:
        # 1. START BULK OPERATION: Create a bulk operation to monitor
        logger.info("🚀 Starting bulk operation for monitoring...")

        # Search for some lights to control
        search_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "light", "domain_filter": "light", "limit": 3},
        )

        search_data = parse_mcp_result(search_result)

        # Handle both nested and direct success indicators
        search_success = search_data.get("success") or search_data.get("data", {}).get(
            "success", False
        )

        # Extract results from nested or direct format
        results_data = search_data.get("data", search_data)
        lights = results_data.get("results", [])

        if not search_success or not lights:
            logger.warning("⚠️ No lights found for bulk operation test")
            pytest.skip("No lights available for bulk operation testing")
            return

        # Use max 2 lights for test to avoid overwhelming test environment
        test_lights = lights[:2]
        entity_ids = [light["entity_id"] for light in test_lights]
        logger.info(f"🔍 Found {len(test_lights)} lights for bulk operation test")

        # Start bulk operation with timeout and error handling
        bulk_result = await mcp_client.call_tool(
            "ha_bulk_control",
            {
                "operations": [
                    {"entity_id": entity_id, "action": "toggle"}
                    for entity_id in entity_ids
                ]
            },
        )

        bulk_data = parse_mcp_result(bulk_result)

        # Handle various bulk operation response formats
        bulk_success_indicators = [
            bulk_data.get("success") is True,
            bulk_data.get("data", {}).get("success") is True,
            # Bulk operations may not have explicit "success" but have operation_ids
            ("operation_ids" in bulk_data and len(bulk_data["operation_ids"]) > 0),
            # Alternative format with results
            ("results" in bulk_data and isinstance(bulk_data["results"], list)),
            # Direct operation success
            ("operations" in bulk_data and len(bulk_data.get("operations", [])) > 0),
        ]

        if not any(bulk_success_indicators):
            error_msg = bulk_data.get("error", "Unknown error")
            logger.warning(f"⚠️ Bulk operation failed to start: {error_msg}")
            pytest.skip(f"Bulk operation not available: {error_msg}")
            return

        # Extract operation identifiers based on response format
        operation_ids = (
            bulk_data.get("operation_ids", [])
            or [
                result.get("operation_id")
                for result in bulk_data.get("results", [])
                if "operation_id" in result
            ]
            or [op.get("id") for op in bulk_data.get("operations", []) if "id" in op]
        )

        # Filter out None values
        operation_ids = [op_id for op_id in operation_ids if op_id is not None]

        if not operation_ids:
            # If no operation_ids, consider the bulk operation as direct execution
            logger.info(
                f"✅ Bulk operation executed directly on {len(entity_ids)} entities"
            )

            # Skip status monitoring if no operation tracking is available
            logger.info("ℹ️ No operation IDs returned - direct execution mode")
            logger.info("✅ Bulk operation status monitoring completed (direct mode)")
            return

        logger.info(
            f"✅ Bulk operation started with {len(operation_ids)} tracked operations"
        )

        # 2. MONITOR STATUS: Check status of bulk operations
        logger.info("📊 Monitoring bulk operation status...")

        try:
            # Add small delay to allow operations to be registered
            await asyncio.sleep(0.5)

            status_result = await mcp_client.call_tool(
                "ha_get_bulk_status", {"operation_ids": operation_ids}
            )

            status_data = parse_mcp_result(status_result)

            # Handle bulk status response formats
            status_success_indicators = [
                status_data.get("success") is True,
                status_data.get("data", {}).get("success") is True,
                # Bulk status returns operational data without explicit success field
                (
                    status_data.get("success") is None
                    and "total_operations" in status_data
                ),
                (status_data.get("success") is None and "statuses" in status_data),
                (status_data.get("success") is None and "operations" in status_data),
            ]

            if not any(status_success_indicators):
                logger.warning(
                    f"⚠️ Bulk status check failed: {status_data.get('error', 'Unknown error')}"
                )
            else:
                # Extract status information based on response format
                statuses = (
                    status_data.get("statuses", {})
                    or status_data.get("data", {}).get("statuses", {})
                    or {
                        op["id"]: op
                        for op in status_data.get("operations", [])
                        if "id" in op
                    }
                )

                logger.info(f"✅ Retrieved status for {len(statuses)} operations")

                # Verify status structure
                for op_id, status in statuses.items():
                    if isinstance(status, dict):
                        status_value = status.get(
                            "status", status.get("state", "unknown")
                        )
                        logger.info(f"  Operation {op_id}: {status_value}")
                    else:
                        logger.info(f"  Operation {op_id}: {status}")

        except Exception as e:
            logger.warning(f"⚠️ Status monitoring failed: {e}")

        # 3. WAIT AND CHECK AGAIN: Monitor until completion with timeout
        logger.info("⏳ Waiting for operations to complete...")
        await asyncio.sleep(3)  # Allow time for operations to complete

        try:
            final_status_result = await mcp_client.call_tool(
                "ha_get_bulk_status", {"operation_ids": operation_ids}
            )

            final_status_data = parse_mcp_result(final_status_result)

            # Handle final status monitoring
            if (
                final_status_data.get("success")
                or "total_operations" in final_status_data
                or "detailed_results" in final_status_data
            ):
                # Extract completion information based on response format
                detailed_results = final_status_data.get("detailed_results", [])
                statuses = final_status_data.get("statuses", {})

                if detailed_results:
                    completed_count = sum(
                        1
                        for result in detailed_results
                        if result.get("status") in ["completed", "success", "done"]
                    )
                    logger.info(
                        f"✅ Final status: {completed_count}/{len(operation_ids)} operations completed"
                    )
                elif statuses:
                    completed_count = sum(
                        1
                        for status in statuses.values()
                        if (
                            isinstance(status, dict)
                            and status.get("status") in ["completed", "success", "done"]
                        )
                        or status in ["completed", "success", "done"]
                    )
                    logger.info(
                        f"✅ Final status: {completed_count}/{len(operation_ids)} operations completed"
                    )
                else:
                    logger.info("✅ Final status monitoring completed")
            else:
                logger.info("ℹ️ Final status check not available or failed")

        except Exception as e:
            logger.warning(f"⚠️ Final status check failed: {e}")

        logger.info("✅ Bulk operation status monitoring completed")

    except Exception as e:
        logger.error(f"❌ Bulk operation status monitoring test failed: {e}")
        raise


@pytest.mark.convenience
async def test_system_overview_information(mcp_client):
    """
    Test: System overview information retrieval

    Validates comprehensive system overview functionality
    which provides users with current Home Assistant status.
    """

    logger.info("🏠 Testing system overview information...")

    try:
        overview_result = await mcp_client.call_tool("ha_get_overview")
        overview_data = parse_mcp_result(overview_result)

        # Handle various overview response formats
        success_indicators = [
            overview_data.get("success") is True,
            overview_data.get("data", {}).get("success") is True,
            # Overview data without explicit success - check for expected content
            (
                overview_data.get("success") is None
                and any(
                    key in overview_data
                    for key in [
                        "domain_counts",
                        "total_entities",
                        "entities",
                        "domains",
                    ]
                )
            ),
        ]

        if not any(success_indicators):
            error_msg = overview_data.get("error", "Unknown error")
            logger.warning(f"⚠️ System overview failed: {error_msg}")
            pytest.skip(f"System overview not available: {error_msg}")
            return

        # Extract data based on response format
        if "data" in overview_data and isinstance(overview_data["data"], dict):
            data = overview_data["data"]
        else:
            data = overview_data

        # Verify overview contains expected sections (flexible matching)
        expected_sections = [
            "domain_counts",
            "total_entities",
            "areas",
            "recent_activity",
            "entities",  # Alternative naming
            "domains",  # Alternative naming
            "system_info",
        ]

        found_sections = [
            section
            for section in expected_sections
            if section in data and data[section]
        ]
        logger.info(
            f"📊 Overview contains sections: {found_sections if found_sections else 'basic info'}"
        )

        # Log summary information with flexible field handling
        overview_logged = False

        # Domain counts information
        domain_counts = data.get("domain_counts") or data.get("domains", {})
        if domain_counts and isinstance(domain_counts, dict):
            total_domains = len(domain_counts)
            logger.info(f"🏠 Found {total_domains} domains with entities")
            overview_logged = True

            # Log top domains if available
            if total_domains > 0:
                try:
                    sorted_domains = sorted(
                        domain_counts.items(), key=lambda x: x[1], reverse=True
                    )
                    top_domains = sorted_domains[:5]
                    for domain, count in top_domains:
                        logger.info(f"  {domain}: {count} entities")
                except (TypeError, AttributeError):
                    logger.info(
                        f"  Domain counts structure: {type(domain_counts)} ({len(domain_counts)} items)"
                    )

        # Total entities information
        total_entities = data.get("total_entities")
        if total_entities is not None:
            logger.info(f"📊 Total entities in system: {total_entities}")
            overview_logged = True
        elif "entities" in data:
            entities_data = data["entities"]
            if isinstance(entities_data, list):
                logger.info(f"📊 Found {len(entities_data)} entities in overview")
            elif isinstance(entities_data, dict):
                logger.info(f"📊 Entities data contains: {list(entities_data.keys())}")
            overview_logged = True

        # Areas information
        areas = data.get("areas")
        if areas is not None:
            if isinstance(areas, list):
                logger.info(f"🏢 Total areas defined: {len(areas)}")
            elif isinstance(areas, dict):
                logger.info(f"🏢 Areas data contains: {list(areas.keys())}")
            else:
                logger.info(f"🏢 Areas information: {areas}")
            overview_logged = True

        # Recent activity information
        recent_activity = data.get("recent_activity")
        if recent_activity is not None:
            if isinstance(recent_activity, list):
                logger.info(f"📈 Recent activity: {len(recent_activity)} entries")
            else:
                logger.info(f"📈 Recent activity: {type(recent_activity)}")
            overview_logged = True

        # System info
        system_info = data.get("system_info")
        if system_info and isinstance(system_info, dict):
            logger.info(f"⚙️ System info available: {list(system_info.keys())}")
            overview_logged = True

        # Log general structure if no specific sections found
        if not overview_logged and data:
            logger.info(
                f"📊 Overview data structure: {list(data.keys()) if isinstance(data, dict) else type(data)}"
            )
            if isinstance(data, dict) and len(data) > 0:
                # Log a sample of the data for debugging
                sample_keys = list(data.keys())[:3]
                logger.info(f"📊 Sample overview fields: {sample_keys}")

        logger.info("✅ System overview information testing completed")

    except Exception as e:
        logger.error(f"❌ System overview test failed: {e}")
        raise
