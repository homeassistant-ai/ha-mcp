"""
Custom assertion helpers for E2E testing.

This module provides specialized assertion functions that make E2E tests
more readable and provide better error messages for common test scenarios.
"""

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def parse_mcp_result(result) -> dict[str, Any]:
    """Parse MCP tool result from FastMCP client response.

    Handles both success responses and error responses (isError=true).
    When isError is true, the error content is parsed as JSON if possible.
    """
    # Check if this is an error response (isError=true from ToolError)
    if hasattr(result, "isError") and result.isError:
        if hasattr(result, "content") and result.content:
            if hasattr(result.content[0], "text"):
                error_text = result.content[0].text
                try:
                    # ToolError content is JSON-serialized structured error
                    return json.loads(error_text)
                except json.JSONDecodeError:
                    return {"success": False, "error": error_text}
        return {"success": False, "error": "Unknown error (isError=true)"}

    if hasattr(result, "content") and result.content:
        if hasattr(result.content[0], "text"):
            response_text = result.content[0].text
            try:
                return json.loads(response_text)
            except json.JSONDecodeError:
                try:
                    return eval(response_text)
                except Exception:
                    return {"raw_response": response_text}
        return {"content": str(result.content[0])}
    return {"error": "No content in result"}


def assert_mcp_success(result, operation_name: str = "operation"):
    """
    Assert that MCP tool result indicates success.

    Args:
        result: FastMCP client result
        operation_name: Name of operation for error message
    """
    data = parse_mcp_result(result)

    # Handle different success indicators
    # Some tools return success in the top level, others in data.success
    success_indicators = [
        data.get("success") is True,
        data.get("data", {}).get("success") is True,
        # If no explicit success field but has data and no error, consider success
        ("data" in data and data.get("error") is None and data.get("success") is None),
        # Bulk operations success: has operational data without explicit success field
        (
            data.get("success") is None
            and data.get("error") is None
            and any(
                field in data
                for field in [
                    "total_operations",
                    "successful_commands",
                    "operation_ids",
                    "results",
                ]
            )
        ),
    ]

    if not any(success_indicators):
        error_msg = data.get("error") or data.get("data", {}).get(
            "error", "Unknown error"
        )
        suggestions = data.get("suggestions", [])

        failure_msg = f"{operation_name} failed: {error_msg}"
        if suggestions:
            failure_msg += f"\nSuggestions: {', '.join(suggestions[:3])}"

        raise AssertionError(failure_msg)

    logger.debug(f"✅ {operation_name} succeeded")
    return data


def assert_mcp_failure(
    result, operation_name: str = "operation", expected_error: str | None = None
):
    """
    Assert that MCP tool result indicates failure.

    Args:
        result: FastMCP client result
        operation_name: Name of operation for error message
        expected_error: Optional substring that should appear in error message
    """
    data = parse_mcp_result(result)

    # Check that operation actually failed
    if data.get("success"):
        raise AssertionError(f"{operation_name} should have failed but succeeded")

    # If expected error specified, check for it
    if expected_error:
        # Error can be a string or a dict with 'message' key
        error_obj = data.get("error", "")
        if isinstance(error_obj, dict):
            error_msg = str(error_obj.get("message", ""))
        else:
            error_msg = str(error_obj)

        if expected_error.lower() not in error_msg.lower():
            raise AssertionError(
                f"{operation_name} failed but error message doesn't contain '{expected_error}'. "
                f"Actual error: {error_msg}"
            )

    logger.debug(f"✅ {operation_name} failed as expected")
    return data


def assert_entity_state(
    state_data: dict[str, Any], expected_state: str, entity_id: str
):
    """
    Assert that entity has expected state.

    Args:
        state_data: Parsed MCP get_state result
        expected_state: Expected state value
        entity_id: Entity ID for error message
    """
    if not state_data.get("success", True):
        raise AssertionError(
            f"Failed to get state for {entity_id}: {state_data.get('error')}"
        )

    actual_state = state_data.get("data", {}).get("state", "unknown")

    if actual_state != expected_state:
        raise AssertionError(
            f"Entity {entity_id} has state '{actual_state}', expected '{expected_state}'"
        )

    logger.debug(f"✅ Entity {entity_id} has expected state: {expected_state}")


def assert_entity_attribute(
    state_data: dict[str, Any], attribute_name: str, expected_value: Any, entity_id: str
):
    """
    Assert that entity has expected attribute value.

    Args:
        state_data: Parsed MCP get_state result
        attribute_name: Name of attribute to check
        expected_value: Expected attribute value
        entity_id: Entity ID for error message
    """
    if not state_data.get("success", True):
        raise AssertionError(
            f"Failed to get state for {entity_id}: {state_data.get('error')}"
        )

    attributes = state_data.get("data", {}).get("attributes", {})

    if attribute_name not in attributes:
        raise AssertionError(f"Entity {entity_id} missing attribute '{attribute_name}'")

    actual_value = attributes[attribute_name]

    if actual_value != expected_value:
        raise AssertionError(
            f"Entity {entity_id} attribute '{attribute_name}' is {actual_value}, expected {expected_value}"
        )

    logger.debug(f"✅ Entity {entity_id} attribute {attribute_name} = {expected_value}")


def assert_automation_config(
    config_data: dict[str, Any], expected_fields: dict[str, Any], automation_id: str
):
    """
    Assert that automation configuration contains expected fields.

    Args:
        config_data: Parsed automation config from get action
        expected_fields: Dictionary of field name -> expected value
        automation_id: Automation ID for error message
    """
    if not config_data.get("success", True):
        raise AssertionError(
            f"Failed to get config for {automation_id}: {config_data.get('error')}"
        )

    config = config_data.get("config", {})

    for field_name, expected_value in expected_fields.items():
        if field_name not in config:
            raise AssertionError(
                f"Automation {automation_id} missing field '{field_name}'"
            )

        actual_value = config[field_name]

        # Handle list/dict comparisons
        if isinstance(expected_value, list | dict):
            if len(actual_value) != len(expected_value):
                raise AssertionError(
                    f"Automation {automation_id} field '{field_name}' has {len(actual_value)} items, "
                    f"expected {len(expected_value)}"
                )
        else:
            if actual_value != expected_value:
                raise AssertionError(
                    f"Automation {automation_id} field '{field_name}' is {actual_value}, "
                    f"expected {expected_value}"
                )

    logger.debug(f"✅ Automation {automation_id} config matches expected fields")


def assert_search_results(
    search_data: dict[str, Any],
    min_results: int = 0,
    max_results: int | None = None,
    domain_filter: str | None = None,
    contains_entity: str | None = None,
):
    """
    Assert search results meet criteria.

    Args:
        search_data: Parsed search result
        min_results: Minimum number of results expected
        max_results: Maximum number of results expected
        domain_filter: If specified, all results should be from this domain
        contains_entity: If specified, results should contain this entity ID
    """
    if not search_data.get("success", True):
        raise AssertionError(f"Search failed: {search_data.get('error')}")

    results = search_data.get("results", [])
    result_count = len(results)

    if result_count < min_results:
        raise AssertionError(
            f"Search returned {result_count} results, expected at least {min_results}"
        )

    if max_results is not None and result_count > max_results:
        raise AssertionError(
            f"Search returned {result_count} results, expected at most {max_results}"
        )

    if domain_filter:
        for result in results:
            entity_id = result.get("entity_id", "")
            if not entity_id.startswith(f"{domain_filter}."):
                raise AssertionError(
                    f"Search result {entity_id} doesn't match domain filter {domain_filter}"
                )

    if contains_entity:
        entity_ids = [r.get("entity_id", "") for r in results]
        if contains_entity not in entity_ids:
            raise AssertionError(
                f"Search results don't contain expected entity {contains_entity}. "
                f"Found: {entity_ids[:5]}"
            )

    logger.debug(f"✅ Search results meet criteria: {result_count} results")


def assert_template_evaluation(
    template_data: dict[str, Any],
    expected_result: Any = None,
    should_succeed: bool = True,
):
    """
    Assert template evaluation result.

    Args:
        template_data: Parsed template evaluation result
        expected_result: Expected template result (if specified)
        should_succeed: Whether template should succeed or fail
    """
    success = template_data.get("success", False)

    if should_succeed and not success:
        error = template_data.get("error", "Unknown error")
        raise AssertionError(
            f"Template evaluation should have succeeded but failed: {error}"
        )

    if not should_succeed and success:
        raise AssertionError("Template evaluation should have failed but succeeded")

    if expected_result is not None and success:
        actual_result = template_data.get("result")
        if actual_result != expected_result:
            raise AssertionError(
                f"Template result is {actual_result}, expected {expected_result}"
            )

    logger.debug(
        f"✅ Template evaluation {'succeeded' if success else 'failed'} as expected"
    )


def assert_bulk_operation_success(
    bulk_data: dict[str, Any],
    expected_operations: int,
    allow_partial_failure: bool = False,
):
    """
    Assert bulk operation completed successfully.

    Args:
        bulk_data: Parsed bulk operation result
        expected_operations: Number of operations that should have been submitted
        allow_partial_failure: Whether individual operation failures are acceptable
    """
    if not bulk_data.get("success", False):
        raise AssertionError(f"Bulk operation failed: {bulk_data.get('error')}")

    operation_ids = bulk_data.get("operation_ids", [])

    if len(operation_ids) != expected_operations:
        raise AssertionError(
            f"Bulk operation created {len(operation_ids)} operations, expected {expected_operations}"
        )

    logger.debug(f"✅ Bulk operation started {len(operation_ids)} operations")


def assert_logbook_contains(
    logbook_data: dict[str, Any], search_text: str, case_sensitive: bool = False
):
    """
    Assert that logbook contains entries with specified text.

    Args:
        logbook_data: Parsed logbook result
        search_text: Text to search for in logbook entries
        case_sensitive: Whether search should be case sensitive
    """
    if not logbook_data.get("success", False):
        raise AssertionError(f"Logbook query failed: {logbook_data.get('error')}")

    entries = logbook_data.get("entries", [])

    if not entries:
        raise AssertionError("Logbook contains no entries")

    search_func = str if case_sensitive else lambda x: str(x).lower()
    target = search_func(search_text)

    for entry in entries:
        entry_text = search_func(entry)
        if target in entry_text:
            logger.debug(f"✅ Found '{search_text}' in logbook")
            return

    raise AssertionError(
        f"Logbook doesn't contain '{search_text}' in {len(entries)} entries"
    )


class MCPAssertions:
    """
    Context manager for MCP-specific assertions with better error reporting.

    Usage:
        async with MCPAssertions(mcp_client) as mcp:
            result = await mcp.call_tool_success("ha_get_state", {"entity_id": "light.test"})
            mcp.assert_entity_state(result, "on", "light.test")
    """

    def __init__(self, mcp_client):
        self.client = mcp_client

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def call_tool_success(
        self, tool_name: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Call MCP tool and assert success."""
        result = await self.client.call_tool(tool_name, params)
        return assert_mcp_success(result, f"{tool_name}({list(params.keys())})")

    async def call_tool_failure(
        self, tool_name: str, params: dict[str, Any], expected_error: str | None = None
    ) -> dict[str, Any]:
        """Call MCP tool and assert failure.

        Handles both legacy dict returns and new ToolError exceptions (isError=true).
        """
        result = await self.client.call_tool(tool_name, params)
        return assert_mcp_failure(
            result, f"{tool_name}({list(params.keys())})", expected_error
        )

    def assert_entity_state(
        self, state_data: dict[str, Any], expected_state: str, entity_id: str
    ):
        """Assert entity state wrapper."""
        return assert_entity_state(state_data, expected_state, entity_id)

    def assert_search_results(self, search_data: dict[str, Any], **kwargs):
        """Assert search results wrapper."""
        return assert_search_results(search_data, **kwargs)

    def assert_template_success(
        self, template_data: dict[str, Any], expected_result: Any = None
    ):
        """Assert template evaluation success."""
        return assert_template_evaluation(
            template_data, expected_result, should_succeed=True
        )

    def assert_template_failure(self, template_data: dict[str, Any]):
        """Assert template evaluation failure."""
        return assert_template_evaluation(template_data, should_succeed=False)


async def wait_for_automation(
    mcp_client,
    automation_id: str,
    timeout: float = 10.0,
    poll_interval: float = 0.5,
) -> dict[str, Any] | None:
    """
    Wait for an automation to be retrievable from Home Assistant.

    Polls ha_config_get_automation until the automation is found or timeout is reached.
    This is more robust than a fixed sleep for waiting after automation creation.

    Args:
        mcp_client: MCP client instance
        automation_id: Automation entity_id or unique_id to wait for
        timeout: Maximum seconds to wait (default: 10.0)
        poll_interval: Seconds between poll attempts (default: 0.5)

    Returns:
        Automation config dict if found, None if timeout reached

    Example:
        config = await wait_for_automation(mcp_client, "automation.test")
        assert config is not None, "Automation not found after creation"
    """
    import asyncio
    import time

    start_time = time.time()

    while time.time() - start_time < timeout:
        result = await mcp_client.call_tool(
            "ha_config_get_automation",
            {"identifier": automation_id},
        )
        parsed = parse_mcp_result(result)

        if parsed.get("success"):
            logger.debug(
                f"Automation {automation_id} found after {time.time() - start_time:.2f}s"
            )
            return parsed.get("config")

        await asyncio.sleep(poll_interval)

    logger.warning(
        f"Automation {automation_id} not found after {timeout}s timeout"
    )
    return None
