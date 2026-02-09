"""
Async waiting utilities for E2E testing.

This module provides helper functions for waiting for state changes, operations
to complete, and other asynchronous conditions in Home Assistant.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from .assertions import parse_mcp_result

logger = logging.getLogger(__name__)


async def wait_for_entity_state(
    mcp_client,
    entity_id: str,
    expected_state: str,
    timeout: int = 10,
    poll_interval: float = 0.5,
) -> bool:
    """
    Wait for entity to reach expected state.

    Args:
        mcp_client: FastMCP client instance
        entity_id: Entity to monitor
        expected_state: State to wait for
        timeout: Maximum wait time in seconds
        poll_interval: Time between checks in seconds

    Returns:
        True if state reached, False if timeout
    """
    start_time = time.time()

    logger.info(
        f"⏳ Waiting for {entity_id} to reach state '{expected_state}' (timeout: {timeout}s)"
    )

    while time.time() - start_time < timeout:
        try:
            state_result = await mcp_client.call_tool(
                "ha_get_state", {"entity_id": entity_id}
            )
            state_data = parse_mcp_result(state_result)

            # Check if 'data' key exists (not 'success' key which doesn't exist in parse_mcp_result)
            if 'data' in state_data and state_data['data'] is not None:
                current_state = state_data.get("data", {}).get("state")
                logger.debug(f"🔍 {entity_id} current state: {current_state}")

                if current_state == expected_state:
                    elapsed = time.time() - start_time
                    logger.info(
                        f"✅ {entity_id} reached state '{expected_state}' after {elapsed:.1f}s"
                    )
                    return True

        except Exception as e:
            logger.debug(f"⚠️ Error checking state for {entity_id}: {e}")

        await asyncio.sleep(poll_interval)

    logger.warning(
        f"⚠️ {entity_id} did not reach state '{expected_state}' within {timeout}s"
    )
    return False


async def wait_for_entity_attribute(
    mcp_client,
    entity_id: str,
    attribute_name: str,
    expected_value: Any,
    timeout: int = 10,
    poll_interval: float = 0.5,
) -> bool:
    """
    Wait for entity attribute to reach expected value.

    Args:
        mcp_client: FastMCP client instance
        entity_id: Entity to monitor
        attribute_name: Attribute to monitor
        expected_value: Value to wait for
        timeout: Maximum wait time in seconds
        poll_interval: Time between checks in seconds

    Returns:
        True if value reached, False if timeout
    """
    start_time = time.time()

    logger.info(
        f"⏳ Waiting for {entity_id}.{attribute_name} = {expected_value} (timeout: {timeout}s)"
    )

    while time.time() - start_time < timeout:
        try:
            state_result = await mcp_client.call_tool(
                "ha_get_state", {"entity_id": entity_id}
            )
            state_data = parse_mcp_result(state_result)

            # Check if 'data' key exists (not 'success' key which doesn't exist in parse_mcp_result)
            if 'data' in state_data and state_data['data'] is not None:
                attributes = state_data.get("data", {}).get("attributes", {})
                current_value = attributes.get(attribute_name)

                logger.debug(
                    f"🔍 {entity_id}.{attribute_name} current value: {current_value}"
                )

                if current_value == expected_value:
                    elapsed = time.time() - start_time
                    logger.info(
                        f"✅ {entity_id}.{attribute_name} = {expected_value} after {elapsed:.1f}s"
                    )
                    return True

        except Exception as e:
            logger.debug(f"⚠️ Error checking attribute for {entity_id}: {e}")

        await asyncio.sleep(poll_interval)

    logger.warning(
        f"⚠️ {entity_id}.{attribute_name} did not reach {expected_value} within {timeout}s"
    )
    return False


async def wait_for_operation_completion(
    mcp_client, operation_id: str, timeout: int = 15, poll_interval: float = 1.0
) -> dict[str, Any]:
    """
    Wait for bulk operation to complete.

    Args:
        mcp_client: FastMCP client instance
        operation_id: Operation to monitor
        timeout: Maximum wait time in seconds
        poll_interval: Time between status checks in seconds

    Returns:
        Operation status data
    """
    start_time = time.time()

    logger.info(
        f"⏳ Waiting for operation {operation_id} to complete (timeout: {timeout}s)"
    )

    while time.time() - start_time < timeout:
        try:
            status_result = await mcp_client.call_tool(
                "ha_get_operation_status",
                {
                    "operation_id": operation_id,
                    "timeout_seconds": min(5, timeout),  # Don't wait too long per check
                },
            )

            status_data = parse_mcp_result(status_result)
            operation_status = status_data.get("status", "unknown")

            logger.debug(f"🔍 Operation {operation_id} status: {operation_status}")

            # Check for completion states
            if operation_status in ["completed", "failed", "timeout"]:
                elapsed = time.time() - start_time
                logger.info(
                    f"✅ Operation {operation_id} finished with status '{operation_status}' after {elapsed:.1f}s"
                )
                return status_data

        except Exception as e:
            logger.debug(f"⚠️ Error checking operation {operation_id}: {e}")

        await asyncio.sleep(poll_interval)

    logger.warning(f"⚠️ Operation {operation_id} did not complete within {timeout}s")
    return {
        "status": "timeout",
        "error": f"Operation monitoring timed out after {timeout}s",
    }


async def wait_for_bulk_operations(
    mcp_client, operation_ids: list[str], timeout: int = 30, poll_interval: float = 1.0
) -> dict[str, dict[str, Any]]:
    """
    Wait for multiple bulk operations to complete.

    Args:
        mcp_client: FastMCP client instance
        operation_ids: List of operation IDs to monitor
        timeout: Maximum wait time in seconds
        poll_interval: Time between checks in seconds

    Returns:
        Dictionary mapping operation_id to status data
    """
    start_time = time.time()
    results = {}
    pending_operations = set(operation_ids)

    logger.info(
        f"⏳ Waiting for {len(operation_ids)} operations to complete (timeout: {timeout}s)"
    )

    while pending_operations and time.time() - start_time < timeout:
        for op_id in list(pending_operations):
            try:
                status_result = await mcp_client.call_tool(
                    "ha_get_operation_status",
                    {
                        "operation_id": op_id,
                        "timeout_seconds": 3,  # Quick check per operation
                    },
                )

                status_data = parse_mcp_result(status_result)
                operation_status = status_data.get("status", "unknown")

                if operation_status in ["completed", "failed", "timeout"]:
                    results[op_id] = status_data
                    pending_operations.remove(op_id)
                    logger.debug(f"✅ Operation {op_id} finished: {operation_status}")

            except Exception as e:
                logger.debug(f"⚠️ Error checking operation {op_id}: {e}")

        if pending_operations:
            await asyncio.sleep(poll_interval)

    # Add timeout results for any remaining operations
    for op_id in pending_operations:
        results[op_id] = {
            "status": "monitoring_timeout",
            "error": f"Operation monitoring timed out after {timeout}s",
        }

    completed = len(results) - len(pending_operations)
    elapsed = time.time() - start_time
    logger.info(
        f"📊 {completed}/{len(operation_ids)} operations completed after {elapsed:.1f}s"
    )

    return results


async def wait_for_logbook_entry(
    mcp_client,
    search_text: str,
    timeout: int = 30,
    poll_interval: float = 2.0,
    hours_back: int = 1,
) -> bool:
    """
    Wait for logbook entry containing specific text.

    Args:
        mcp_client: FastMCP client instance
        search_text: Text to search for in logbook
        timeout: Maximum wait time in seconds
        poll_interval: Time between logbook checks in seconds
        hours_back: How many hours of logbook to search

    Returns:
        True if entry found, False if timeout
    """
    start_time = time.time()

    logger.info(
        f"⏳ Waiting for logbook entry containing '{search_text}' (timeout: {timeout}s)"
    )

    while time.time() - start_time < timeout:
        try:
            logbook_result = await mcp_client.call_tool(
                "ha_get_logbook", {"hours_back": hours_back}
            )

            logbook_data = parse_mcp_result(logbook_result)

            # Check if 'data' key exists (not 'success' key which doesn't exist in parse_mcp_result)
            if 'data' in logbook_data and logbook_data['data'] is not None:
                entries = logbook_data.get("entries", [])

                for entry in entries:
                    entry_text = str(entry).lower()
                    if search_text.lower() in entry_text:
                        elapsed = time.time() - start_time
                        logger.info(
                            f"✅ Found logbook entry with '{search_text}' after {elapsed:.1f}s"
                        )
                        return True

        except Exception as e:
            logger.debug(f"⚠️ Error checking logbook: {e}")

        await asyncio.sleep(poll_interval)

    logger.warning(
        f"⚠️ Logbook entry containing '{search_text}' not found within {timeout}s"
    )
    return False


async def wait_for_condition(
    condition_func: Callable[[], Any],
    timeout: int = 10,
    poll_interval: float = 0.5,
    condition_name: str = "condition",
) -> bool:
    """
    Wait for custom condition function to return truthy value.

    Args:
        condition_func: Function that returns truthy when condition is met
        timeout: Maximum wait time in seconds
        poll_interval: Time between checks in seconds
        condition_name: Name of condition for logging

    Returns:
        True if condition met, False if timeout
    """
    start_time = time.time()

    logger.info(f"⏳ Waiting for {condition_name} (timeout: {timeout}s)")

    while time.time() - start_time < timeout:
        try:
            if (
                await condition_func()
                if asyncio.iscoroutinefunction(condition_func)
                else condition_func()
            ):
                elapsed = time.time() - start_time
                logger.info(f"✅ {condition_name} met after {elapsed:.1f}s")
                return True
        except Exception as e:
            logger.debug(f"⚠️ Error checking {condition_name}: {e}")

        await asyncio.sleep(poll_interval)

    logger.warning(f"⚠️ {condition_name} not met within {timeout}s")
    return False


async def wait_for_state_change(
    mcp_client, entity_id: str, timeout: int = 10, poll_interval: float = 0.5
) -> str | None:
    """
    Wait for entity state to change from current state.

    Args:
        mcp_client: FastMCP client instance
        entity_id: Entity to monitor
        timeout: Maximum wait time in seconds
        poll_interval: Time between checks in seconds

    Returns:
        New state if changed, None if timeout or error
    """
    # Get initial state
    try:
        initial_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": entity_id}
        )
        initial_data = parse_mcp_result(initial_result)

        # Check if 'data' key exists (not 'success' key which doesn't exist in parse_mcp_result)
        if 'data' not in initial_data or initial_data['data'] is None:
            logger.warning(f"⚠️ Could not get initial state for {entity_id}")
            return None

        initial_state = initial_data.get("data", {}).get("state")
        logger.info(
            f"⏳ Waiting for {entity_id} to change from '{initial_state}' (timeout: {timeout}s)"
        )

    except Exception as e:
        logger.warning(f"⚠️ Error getting initial state for {entity_id}: {e}")
        return None

    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            state_result = await mcp_client.call_tool(
                "ha_get_state", {"entity_id": entity_id}
            )
            state_data = parse_mcp_result(state_result)

            # Check if 'data' key exists (not 'success' key which doesn't exist in parse_mcp_result)
            if 'data' in state_data and state_data['data'] is not None:
                current_state = state_data.get("data", {}).get("state")

                if current_state != initial_state:
                    elapsed = time.time() - start_time
                    logger.info(
                        f"✅ {entity_id} changed: '{initial_state}' → '{current_state}' after {elapsed:.1f}s"
                    )
                    return current_state

        except Exception as e:
            logger.debug(f"⚠️ Error checking state change for {entity_id}: {e}")

        await asyncio.sleep(poll_interval)

    logger.warning(
        f"⚠️ {entity_id} did not change from '{initial_state}' within {timeout}s"
    )
    return None


async def wait_for_tool_result(
    mcp_client,
    tool_name: str,
    arguments: dict[str, Any],
    predicate: Callable[[dict[str, Any]], bool],
    timeout: int = 15,
    poll_interval: float = 1.0,
    description: str = "tool result",
) -> dict[str, Any]:
    """
    Poll an MCP tool until the result satisfies a predicate.

    Useful when an entity was just created and needs time to be registered
    in Home Assistant before it becomes visible to search/query tools.

    Args:
        mcp_client: FastMCP client instance
        tool_name: MCP tool to call repeatedly
        arguments: Arguments to pass to the tool
        predicate: Function that receives parsed tool result and returns
                   True when the desired condition is met
        timeout: Maximum wait time in seconds
        poll_interval: Time between calls in seconds
        description: Human-readable description for logging

    Returns:
        The parsed tool result that satisfied the predicate, or the last
        result if timeout was reached.
    """
    start_time = time.time()
    last_data: dict[str, Any] = {}

    logger.info(f"⏳ Waiting for {description} (timeout: {timeout}s)")

    while time.time() - start_time < timeout:
        try:
            result = await mcp_client.call_tool(tool_name, arguments)
            last_data = parse_mcp_result(result)

            if predicate(last_data):
                elapsed = time.time() - start_time
                logger.info(f"✅ {description} satisfied after {elapsed:.1f}s")
                return last_data
        except Exception as e:
            logger.debug(f"⚠️ Error polling {tool_name}: {e}")

        await asyncio.sleep(poll_interval)

    elapsed = time.time() - start_time
    logger.warning(f"⚠️ {description} not satisfied within {elapsed:.1f}s")
    return last_data


class WaitHelper:
    """
    Helper class for common waiting patterns with a specific MCP client.

    Usage:
        waiter = WaitHelper(mcp_client)
        await waiter.entity_state("light.bedroom", "on", timeout=15)
        await waiter.operation_completion(operation_id)
    """

    def __init__(self, mcp_client):
        self.client = mcp_client

    async def entity_state(
        self, entity_id: str, expected_state: str, timeout: int = 10
    ) -> bool:
        """Wait for entity state."""
        return await wait_for_entity_state(
            self.client, entity_id, expected_state, timeout
        )

    async def entity_attribute(
        self,
        entity_id: str,
        attribute_name: str,
        expected_value: Any,
        timeout: int = 10,
    ) -> bool:
        """Wait for entity attribute."""
        return await wait_for_entity_attribute(
            self.client, entity_id, attribute_name, expected_value, timeout
        )

    async def operation_completion(
        self, operation_id: str, timeout: int = 15
    ) -> dict[str, Any]:
        """Wait for operation completion."""
        return await wait_for_operation_completion(self.client, operation_id, timeout)

    async def bulk_operations(
        self, operation_ids: list[str], timeout: int = 30
    ) -> dict[str, dict[str, Any]]:
        """Wait for bulk operations."""
        return await wait_for_bulk_operations(self.client, operation_ids, timeout)

    async def logbook_entry(self, search_text: str, timeout: int = 30) -> bool:
        """Wait for logbook entry."""
        return await wait_for_logbook_entry(self.client, search_text, timeout)

    async def state_change(self, entity_id: str, timeout: int = 10) -> str | None:
        """Wait for any state change."""
        return await wait_for_state_change(self.client, entity_id, timeout)

    async def condition(
        self,
        condition_func: Callable[[], Any],
        timeout: int = 10,
        name: str = "condition",
    ) -> bool:
        """Wait for custom condition."""
        return await wait_for_condition(condition_func, timeout, condition_name=name)

    async def tool_result(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        predicate: Callable[[dict[str, Any]], bool],
        timeout: int = 15,
        description: str = "tool result",
    ) -> dict[str, Any]:
        """Wait for tool result to satisfy predicate."""
        return await wait_for_tool_result(
            self.client, tool_name, arguments, predicate, timeout=timeout, description=description
        )
