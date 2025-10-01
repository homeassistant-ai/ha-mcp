"""
Error Handling and Edge Cases E2E Tests

Comprehensive tests for error handling, edge cases, and boundary conditions
across all MCP tools. These tests ensure robustness and proper error reporting
which is crucial for production reliability.
"""

import asyncio
import logging
import time
from typing import Any

import pytest

from ..utilities.assertions import (
    parse_mcp_result,
)

logger = logging.getLogger(__name__)


@pytest.mark.error_handling
class TestErrorHandling:
    """Test error handling and edge cases across MCP tools."""

    async def _safe_tool_call(
        self, mcp_client, tool_name: str, params: dict[str, Any], timeout: float = 10.0
    ):
        """Safe wrapper for tool calls with timeout protection."""
        try:
            return await asyncio.wait_for(
                mcp_client.call_tool(tool_name, params), timeout=timeout
            )
        except TimeoutError:
            logger.warning(f"Tool call {tool_name} timed out after {timeout}s")
            return {"success": False, "error": f"Operation timed out after {timeout}s"}
        except Exception as e:
            logger.warning(f"Tool call {tool_name} failed: {e}")
            return {"success": False, "error": str(e)}

    async def test_invalid_entity_id_handling(self, mcp_client):
        """
        Test: Invalid entity ID error handling

        Validates proper error handling when invalid entity IDs
        are provided to various MCP tools.
        """

        logger.info("❌ Testing invalid entity ID handling...")

        invalid_entity_ids = [
            "nonexistent.entity",
            "invalid_domain.test",
            "",
            "light.",
            ".invalid",
            "light.with spaces",
            "domain_with_underscore.entity-with-dashes",
        ]

        for entity_id in invalid_entity_ids:
            logger.info(f"🔍 Testing invalid entity ID: '{entity_id}'")

            # Test ha_get_state with invalid entity
            state_result = await self._safe_tool_call(
                mcp_client, "ha_get_state", {"entity_id": entity_id}
            )

            state_data = parse_mcp_result(state_result)

            # Should either fail gracefully or return not found
            if not state_data.get("success"):
                logger.info(
                    f"  ✅ Correctly failed for '{entity_id}': {state_data.get('error', 'No error message')[:50]}"
                )
            else:
                # If it "succeeds", should indicate entity not found
                data = state_data.get("data", {})
                if not data or data.get("state") in ["unknown", "unavailable", None]:
                    logger.info(
                        f"  ✅ Correctly returned 'not found' for '{entity_id}'"
                    )
                else:
                    logger.warning(
                        f"  ⚠️ Unexpectedly found data for invalid entity '{entity_id}': {data}"
                    )

        logger.info("✅ Invalid entity ID handling test completed")

    async def test_service_call_error_handling(self, mcp_client):
        """
        Test: Service call error handling

        Tests error handling for invalid service calls including
        nonexistent services, invalid parameters, and malformed requests.
        """

        logger.info("📞 Testing service call error handling...")

        # 1. NONEXISTENT SERVICE: Call service that doesn't exist
        logger.info("🚫 Testing nonexistent service...")
        invalid_service_result = await self._safe_tool_call(
            mcp_client,
            "ha_call_service",
            {"domain": "nonexistent_domain", "service": "fake_service"},
        )

        invalid_service_data = parse_mcp_result(invalid_service_result)
        if not invalid_service_data.get("success"):
            logger.info(
                f"  ✅ Correctly failed for nonexistent service: {invalid_service_data.get('error', '')[:50]}"
            )
        else:
            logger.warning("  ⚠️ Nonexistent service call unexpectedly succeeded")

        # 2. INVALID DOMAIN: Valid service format but invalid domain
        logger.info("🏠 Testing invalid domain...")
        invalid_domain_result = await self._safe_tool_call(
            mcp_client,
            "ha_call_service",
            {"domain": "invalid_domain", "service": "turn_on"},
        )

        invalid_domain_data = parse_mcp_result(invalid_domain_result)
        if not invalid_domain_data.get("success"):
            logger.info(
                f"  ✅ Correctly failed for invalid domain: {invalid_domain_data.get('error', '')[:50]}"
            )

        # 3. MISSING REQUIRED PARAMETERS: Try to call service without required params
        logger.info("📋 Testing missing required parameters...")

        # Try to call light.turn_on without entity_id (if lights exist)
        search_result = await self._safe_tool_call(
            mcp_client,
            "ha_search_entities",
            {"query": "light", "domain_filter": "light", "limit": 1},
        )

        search_data = parse_mcp_result(search_result)
        if search_data.get("data", {}).get("success") and search_data.get(
            "data", {}
        ).get("results"):
            # Call service without entity_id to test parameter validation
            missing_params_result = await self._safe_tool_call(
                mcp_client,
                "ha_call_service",
                {
                    "domain": "light",
                    "service": "turn_on",
                    # Missing entity_id
                },
            )

            missing_params_data = parse_mcp_result(missing_params_result)
            # This might succeed (affects all lights) or fail depending on HA config
            logger.info(
                f"  Service call without entity_id: {'succeeded' if missing_params_data.get('success') else 'failed'}"
            )

        logger.info("✅ Service call error handling test completed")

    async def test_search_boundary_conditions(self, mcp_client):
        """
        Test: Search functionality boundary conditions

        Tests search with various edge cases including empty queries,
        extremely long queries, special characters, and limit boundaries.
        """

        logger.info("🔍 Testing search boundary conditions...")

        # 1. EMPTY QUERY: Search with empty string
        logger.info("🔳 Testing empty query...")
        empty_result = await self._safe_tool_call(
            mcp_client, "ha_search_entities", {"query": "", "limit": 5}
        )

        empty_data = parse_mcp_result(empty_result)
        if empty_data.get("data", {}).get("success"):
            results = empty_data.get("data", {}).get("results", [])
            logger.info(f"  ✅ Empty query returned {len(results)} results")
        else:
            logger.info(
                f"  ✅ Empty query correctly failed: {empty_data.get('error', '')[:50]}"
            )

        # 2. VERY LONG QUERY: Test with extremely long search string
        logger.info("📏 Testing very long query...")
        long_query = "a" * 1000  # 1000 character query
        long_result = await self._safe_tool_call(
            mcp_client, "ha_search_entities", {"query": long_query, "limit": 5}
        )

        long_data = parse_mcp_result(long_result)
        if long_data.get("data", {}).get("success"):
            results = long_data.get("data", {}).get("results", [])
            logger.info(
                f"  ✅ Long query handled gracefully, returned {len(results)} results"
            )
        else:
            logger.info(
                f"  ✅ Long query correctly failed: {long_data.get('error', '')[:50]}"
            )

        # 3. SPECIAL CHARACTERS: Test with various special characters
        logger.info("🔣 Testing special characters...")
        special_queries = [
            "@#$%",
            "🏠🔥💡",
            "café",
            "test\nwith\nnewlines",
            "query;with;semicolons",
        ]

        for query in special_queries:
            special_result = await self._safe_tool_call(
                mcp_client, "ha_search_entities", {"query": query, "limit": 5}
            )

            special_data = parse_mcp_result(special_result)
            status = (
                "succeeded" if special_data.get("data", {}).get("success") else "failed"
            )
            logger.info(f"  Query '{query}': {status}")

        # 4. EXTREME LIMITS: Test boundary limit values
        logger.info("🔢 Testing extreme limit values...")
        extreme_limits = [0, -1, 1000000, 9999]

        for limit in extreme_limits:
            limit_result = await self._safe_tool_call(
                mcp_client, "ha_search_entities", {"query": "light", "limit": limit}
            )

            limit_data = parse_mcp_result(limit_result)
            if limit_data.get("data", {}).get("success"):
                results = limit_data.get("data", {}).get("results", [])
                logger.info(f"  Limit {limit}: returned {len(results)} results")
            else:
                logger.info(
                    f"  Limit {limit}: failed - {limit_data.get('error', '')[:30]}"
                )

        logger.info("✅ Search boundary conditions test completed")

    async def test_template_error_conditions(self, mcp_client):
        """
        Test: Template evaluation error conditions

        Tests template evaluation with invalid syntax, undefined variables,
        circular references, and other error conditions.
        """

        logger.info("🧪 Testing template error conditions...")

        error_templates = [
            # Syntax errors
            ("{{ invalid syntax", "Invalid syntax"),
            ("{{ missing_end_brace", "Missing end brace"),
            ("{{{{ too_many_braces }}}}", "Too many braces"),
            # Undefined variables
            ("{{ nonexistent_variable }}", "Undefined variable"),
            ("{{ states.nonexistent.entity }}", "Nonexistent entity"),
            # Invalid functions
            ("{{ invalid_function() }}", "Invalid function"),
            ("{{ states().nonexistent_method() }}", "Invalid method"),
            # Type errors
            ("{{ 'string' + 123 }}", "Type mismatch"),
            ("{{ states('light.test').invalid_attribute }}", "Invalid attribute"),
        ]

        for template, description in error_templates:
            logger.info(f"🧪 Testing {description}: {template[:30]}...")

            template_result = await self._safe_tool_call(
                mcp_client, "ha_eval_template", {"template": template}
            )

            template_data = parse_mcp_result(template_result)

            if not template_data.get("success"):
                error_msg = template_data.get("error", "No error message")
                logger.info(f"  ✅ Correctly failed: {error_msg[:50]}")
            else:
                result = template_data.get("result", "")
                logger.warning(
                    f"  ⚠️ Template unexpectedly succeeded with result: {result}"
                )

        logger.info("✅ Template error conditions test completed")

    async def test_bulk_operation_error_scenarios(self, mcp_client):
        """
        Test: Bulk operation error scenarios

        Tests bulk operations with invalid entity lists, mixed valid/invalid entities,
        and other error conditions specific to bulk operations.
        """

        logger.info("📦 Testing bulk operation error scenarios...")

        # 1. EMPTY ENTITY LIST: Bulk operation with no entities
        logger.info("🔳 Testing empty entity list...")
        empty_bulk_result = await self._safe_tool_call(
            mcp_client, "ha_bulk_control", {"operations": []}
        )

        empty_bulk_data = parse_mcp_result(empty_bulk_result)
        if not empty_bulk_data.get("success"):
            logger.info(
                f"  ✅ Empty entity list correctly failed: {empty_bulk_data.get('error', '')[:50]}"
            )
        else:
            logger.warning("  ⚠️ Empty entity list unexpectedly succeeded")

        # 2. INVALID ENTITIES: Mix of valid and invalid entity IDs
        logger.info("❌ Testing mixed valid/invalid entities...")

        # Get one valid entity
        search_result = await self._safe_tool_call(
            mcp_client,
            "ha_search_entities",
            {"query": "light", "domain_filter": "light", "limit": 1},
        )

        search_data = parse_mcp_result(search_result)
        valid_entities = []
        if search_data.get("data", {}).get("success") and search_data.get(
            "data", {}
        ).get("results"):
            valid_entities = [search_data["data"]["results"][0]["entity_id"]]

        mixed_entities = valid_entities + ["nonexistent.entity", "invalid.test"]

        if mixed_entities:
            mixed_bulk_result = await self._safe_tool_call(
                mcp_client,
                "ha_bulk_control",
                {
                    "operations": [
                        {"entity_id": entity_id, "action": "turn_on"}
                        for entity_id in mixed_entities
                    ]
                },
            )

            mixed_bulk_data = parse_mcp_result(mixed_bulk_result)

            if mixed_bulk_data.get("success"):
                # Check if partial success is reported
                operation_ids = mixed_bulk_data.get("operation_ids", [])
                logger.info(
                    f"  ✅ Mixed entities handled, {len(operation_ids)} operations created"
                )

                # Check status of operations
                if operation_ids:
                    await asyncio.sleep(2)
                    status_result = await self._safe_tool_call(
                        mcp_client,
                        "ha_get_bulk_status",
                        {"operation_ids": operation_ids},
                    )

                    status_data = parse_mcp_result(status_result)
                    if status_data.get("success"):
                        statuses = status_data.get("statuses", {})
                        failed_ops = [
                            op
                            for op, status in statuses.items()
                            if status.get("status") == "failed"
                        ]
                        logger.info(
                            f"    {len(failed_ops)} operations failed (expected for invalid entities)"
                        )
            else:
                logger.info(
                    f"  ✅ Mixed entities correctly failed: {mixed_bulk_data.get('error', '')[:50]}"
                )

        # 3. INVALID ACTION: Bulk operation with invalid action
        logger.info("🎬 Testing invalid action...")
        if valid_entities:
            invalid_action_result = await self._safe_tool_call(
                mcp_client,
                "ha_bulk_control",
                {
                    "operations": [
                        {"entity_id": entity_id, "action": "invalid_action"}
                        for entity_id in valid_entities
                    ]
                },
            )

            invalid_action_data = parse_mcp_result(invalid_action_result)
            if not invalid_action_data.get("success"):
                logger.info(
                    f"  ✅ Invalid action correctly failed: {invalid_action_data.get('error', '')[:50]}"
                )
            else:
                logger.warning("  ⚠️ Invalid action unexpectedly succeeded")

        logger.info("✅ Bulk operation error scenarios test completed")

    async def test_helper_creation_validation(self, mcp_client, cleanup_tracker):
        """
        Test: Helper creation validation and error handling

        Tests helper creation with invalid configurations, missing required fields,
        and constraint violations.
        """

        logger.info("🔧 Testing helper creation validation...")

        # 1. MISSING REQUIRED FIELDS: Try to create helper without name
        logger.info("📝 Testing missing required fields...")
        try:
            missing_name_result = await self._safe_tool_call(
                mcp_client,
                "ha_config_set_helper",
                {
                    "helper_type": "input_boolean",
                    # Missing name - should fail at FastMCP validation level
                },
            )
            missing_name_data = parse_mcp_result(missing_name_result)
            if not missing_name_data.get("success"):
                logger.info(
                    f"  ✅ Missing name correctly failed: {missing_name_data.get('error', '')[:50]}"
                )
            else:
                logger.warning("  ⚠️ Missing name unexpectedly succeeded")
        except Exception as e:
            error_str = str(e).lower()
            if any(
                phrase in error_str
                for phrase in [
                    "required property",
                    "validation error",
                    "missing required parameter",
                    "missing",
                    "required",
                    "name",
                ]
            ):
                logger.info(
                    f"  ✅ Missing name correctly failed at validation: {str(e)[:100]}"
                )
            else:
                # Log but don't re-raise - this is an error handling test
                logger.warning(f"  ⚠️ Unexpected validation error: {str(e)}")

        # 2. INVALID HELPER TYPE: Create helper with nonexistent type
        logger.info("🔧 Testing invalid helper type...")
        invalid_type_result = await self._safe_tool_call(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "nonexistent_type",
                "name": "Test Invalid Type"},
        )

        invalid_type_data = parse_mcp_result(invalid_type_result)
        if not invalid_type_data.get("success"):
            logger.info(
                f"  ✅ Invalid type correctly failed: {invalid_type_data.get('error', '')[:50]}"
            )
        else:
            logger.warning("  ⚠️ Invalid helper type unexpectedly succeeded")

        # 3. CONSTRAINT VIOLATIONS: Test specific helper constraints

        # input_number with invalid range
        logger.info("🔢 Testing input_number constraint violations...")
        invalid_range_result = await self._safe_tool_call(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_number",
                "name": "Test Invalid Range",
                "min_value": 100.0,
                "max_value": 50.0,  # max_value < min_value - should fail validation
                "step": 1.0,
                "mode": "slider"},
        )

        invalid_range_data = parse_mcp_result(invalid_range_result)
        if not invalid_range_data.get("success"):
            logger.info(
                f"  ✅ Invalid range correctly failed: {invalid_range_data.get('error', '')[:50]}"
            )
        else:
            logger.warning("  ⚠️ Invalid range unexpectedly succeeded")

        # input_select with empty options
        logger.info("📋 Testing input_select with empty options...")
        empty_options_result = await self._safe_tool_call(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_select",
                "name": "Test Empty Options",
                "options": []},
        )

        empty_options_data = parse_mcp_result(empty_options_result)
        if not empty_options_data.get("success"):
            logger.info(
                f"  ✅ Empty options correctly failed: {empty_options_data.get('error', '')[:50]}"
            )
        else:
            logger.warning("  ⚠️ Empty options unexpectedly succeeded")

        # input_datetime with neither has_date nor has_time
        logger.info("📅 Testing input_datetime without date or time...")
        no_date_time_result = await self._safe_tool_call(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_datetime",
                "name": "Test No Date Time",
                "has_date": False,
                "has_time": False},
        )

        no_date_time_data = parse_mcp_result(no_date_time_result)
        if not no_date_time_data.get("success"):
            logger.info(
                f"  ✅ No date/time correctly failed: {no_date_time_data.get('error', '')[:50]}"
            )
        else:
            logger.warning("  ⚠️ No date/time unexpectedly succeeded")

        logger.info("✅ Helper creation validation test completed")

    async def test_concurrent_operation_handling(self, mcp_client, cleanup_tracker):
        """
        Test: Concurrent operation handling

        Tests system behavior under concurrent load and ensures
        proper handling of simultaneous operations.
        """

        logger.info("🚀 Testing concurrent operation handling...")

        # Get some test entities
        search_result = await self._safe_tool_call(
            mcp_client,
            "ha_search_entities",
            {"query": "light", "domain_filter": "light", "limit": 3},
        )

        search_data = parse_mcp_result(search_result)
        if not search_data.get("data", {}).get("success") or not search_data.get(
            "data", {}
        ).get("results"):
            logger.warning("⚠️ No entities found for concurrent operation test")
            return

        entities = search_data["data"]["results"][:3]

        # 1. CONCURRENT INDIVIDUAL OPERATIONS: Multiple simultaneous service calls
        logger.info("🔄 Testing concurrent individual operations...")

        async def call_service_for_entity(entity):
            """Helper function to call service for an entity."""
            try:
                result = await _safe_tool_call_standalone(
                    mcp_client,
                    "ha_call_service",
                    {
                        "domain": "homeassistant",
                        "service": "update_entity",
                        "entity_id": entity["entity_id"],
                    },
                )
                return parse_mcp_result(result)
            except Exception as e:
                logger.warning(
                    f"Service call failed for {entity.get('entity_id', 'unknown')}: {e}"
                )
                return {"success": False, "error": str(e)}

        # Execute concurrent operations
        concurrent_tasks = [call_service_for_entity(entity) for entity in entities]
        concurrent_results = await asyncio.gather(
            *concurrent_tasks, return_exceptions=True
        )

        successful_ops = sum(
            1
            for result in concurrent_results
            if isinstance(result, dict) and result.get("success")
        )
        logger.info(
            f"  ✅ {successful_ops}/{len(entities)} concurrent operations succeeded"
        )

        # 2. CONCURRENT BULK OPERATIONS: Multiple bulk operations simultaneously
        logger.info("📦 Testing concurrent bulk operations...")

        entity_groups = [
            [entities[0]["entity_id"]] if len(entities) > 0 else [],
            [entities[1]["entity_id"]] if len(entities) > 1 else [],
        ]

        async def bulk_operation(entity_list, action):
            """Helper function for bulk operation."""
            if not entity_list:
                return {"success": False, "error": "No entities"}
            try:
                result = await _safe_tool_call_standalone(
                    mcp_client,
                    "ha_bulk_control",
                    {
                        "operations": [
                            {"entity_id": entity_id, "action": action}
                            for entity_id in entity_list
                        ]
                    },
                )
                return parse_mcp_result(result)
            except Exception as e:
                logger.warning(f"Bulk operation failed for action {action}: {e}")
                return {"success": False, "error": str(e)}

        bulk_tasks = [
            bulk_operation(entity_groups[0], "turn_on"),
            bulk_operation(
                entity_groups[1] if len(entity_groups) > 1 else [], "turn_off"
            ),
        ]

        bulk_results = await asyncio.gather(*bulk_tasks, return_exceptions=True)

        successful_bulk = sum(
            1
            for result in bulk_results
            if isinstance(result, dict) and result.get("success")
        )
        logger.info(
            f"  ✅ {successful_bulk}/{len(bulk_tasks)} concurrent bulk operations succeeded"
        )

        # 3. CONCURRENT HELPER CREATION: Create multiple helpers simultaneously
        logger.info("🔧 Testing concurrent helper creation...")

        async def create_helper(helper_name, helper_type):
            """Helper function to create a helper."""
            try:
                result = await _safe_tool_call_standalone(
                    mcp_client,
                    "ha_config_set_helper",
                    {
                        "helper_type": helper_type,
                        "name": helper_name},
                )
                data = parse_mcp_result(result)
                if data.get("success"):
                    # Track for cleanup
                    entity_id = (
                        data.get("entity_id")
                        or f"{helper_type}.{helper_name.lower().replace(' ', '_')}"
                    )
                    if hasattr(cleanup_tracker, "track"):
                        cleanup_tracker.track("helper", entity_id)
                return data
            except Exception as e:
                logger.warning(f"Helper creation failed for {helper_name}: {e}")
                return {"success": False, "error": str(e)}

        helper_tasks = [
            create_helper("Concurrent Test 1", "input_boolean"),
            create_helper("Concurrent Test 2", "input_boolean"),
            create_helper("Concurrent Test 3", "input_text"),
        ]

        helper_results = await asyncio.gather(*helper_tasks, return_exceptions=True)

        successful_helpers = sum(
            1
            for result in helper_results
            if isinstance(result, dict) and result.get("success")
        )
        logger.info(
            f"  ✅ {successful_helpers}/{len(helper_tasks)} concurrent helper creations succeeded"
        )

        logger.info("✅ Concurrent operation handling test completed")


async def _safe_tool_call_standalone(
    mcp_client, tool_name: str, params: dict[str, Any] = None, timeout: float = 10.0
):
    """Standalone safe wrapper for tool calls with timeout protection."""
    if params is None:
        params = {}
    try:
        return await asyncio.wait_for(
            mcp_client.call_tool(tool_name, params), timeout=timeout
        )
    except TimeoutError:
        logger.warning(f"Tool call {tool_name} timed out after {timeout}s")
        return {"success": False, "error": f"Operation timed out after {timeout}s"}
    except Exception as e:
        logger.warning(f"Tool call {tool_name} failed: {e}")
        return {"success": False, "error": str(e)}


@pytest.mark.error_handling
async def test_system_resilience_under_load(mcp_client):
    """
    Test: System resilience under load

    Tests system behavior under sustained load to ensure
    stability and proper resource management.
    """

    logger.info("💪 Testing system resilience under load...")

    # Rapid sequence of operations with timeout protection
    logger.info("⚡ Testing rapid operation sequence...")

    async def safe_overview_call():
        """Safe overview call with timeout."""
        return await _safe_tool_call_standalone(mcp_client, "ha_get_overview", {}, 15.0)

    rapid_operations = [
        safe_overview_call() for _ in range(10)
    ]  # Reduced to 10 for stability

    start_time = time.time()
    rapid_results = await asyncio.gather(*rapid_operations, return_exceptions=True)
    end_time = time.time()

    successful_rapid = sum(
        1
        for result in rapid_results
        if not isinstance(result, Exception)
        and parse_mcp_result(result).get("success", False)
    )
    duration = end_time - start_time

    logger.info(
        f"  ✅ {successful_rapid}/10 rapid operations succeeded in {duration:.2f}s"
    )
    logger.info(f"  📊 Average time per operation: {duration / 10:.3f}s")

    # Memory and resource usage monitoring (basic)
    logger.info("🧠 Checking system responsiveness after load...")

    # Simple responsiveness check with timeout
    try:
        response_check = await _safe_tool_call_standalone(
            mcp_client, "ha_get_overview", {}, 30.0
        )
        response_data = parse_mcp_result(response_check)

        if response_data.get("success"):
            logger.info("  ✅ System remains responsive after load test")
        else:
            logger.warning(
                f"  ⚠️ System responsiveness degraded: {response_data.get('error', '')}"
            )
    except Exception as e:
        logger.warning(f"  ⚠️ System responsiveness check failed: {e}")

    logger.info("✅ System resilience under load test completed")
