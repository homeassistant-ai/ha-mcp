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
    start_time = time.monotonic()

    logger.info(
        f"⏳ Waiting for {entity_id} to reach state '{expected_state}' (timeout: {timeout}s)"
    )

    while time.monotonic() - start_time < timeout:
        try:
            state_result = await mcp_client.call_tool(
                "ha_get_state", {"entity_id": entity_id}
            )
            state_data = parse_mcp_result(state_result)

            # Check if 'data' key exists (not 'success' key which doesn't exist in parse_mcp_result)
            if "data" in state_data and state_data["data"] is not None:
                current_state = state_data.get("data", {}).get("state")
                logger.debug(f"🔍 {entity_id} current state: {current_state}")

                if current_state == expected_state:
                    elapsed = time.monotonic() - start_time
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
    start_time = time.monotonic()

    logger.info(
        f"⏳ Waiting for logbook entry containing '{search_text}' (timeout: {timeout}s)"
    )

    while time.monotonic() - start_time < timeout:
        try:
            logbook_result = await mcp_client.call_tool(
                "ha_get_logs", {"hours_back": hours_back}
            )

            logbook_data = parse_mcp_result(logbook_result)

            # Check if 'data' key exists (not 'success' key which doesn't exist in parse_mcp_result)
            if "data" in logbook_data and logbook_data["data"] is not None:
                entries = logbook_data["data"].get("entries", [])

                for entry in entries:
                    entry_text = str(entry).lower()
                    if search_text.lower() in entry_text:
                        elapsed = time.monotonic() - start_time
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
    start_time = time.monotonic()

    logger.info(f"⏳ Waiting for {condition_name} (timeout: {timeout}s)")

    while time.monotonic() - start_time < timeout:
        try:
            if (
                await condition_func()
                if asyncio.iscoroutinefunction(condition_func)
                else condition_func()
            ):
                elapsed = time.monotonic() - start_time
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
        if "data" not in initial_data or initial_data["data"] is None:
            logger.warning(f"⚠️ Could not get initial state for {entity_id}")
            return None

        initial_state = initial_data.get("data", {}).get("state")
        logger.info(
            f"⏳ Waiting for {entity_id} to change from '{initial_state}' (timeout: {timeout}s)"
        )

    except Exception as e:
        logger.warning(f"⚠️ Error getting initial state for {entity_id}: {e}")
        return None

    start_time = time.monotonic()

    while time.monotonic() - start_time < timeout:
        try:
            state_result = await mcp_client.call_tool(
                "ha_get_state", {"entity_id": entity_id}
            )
            state_data = parse_mcp_result(state_result)

            # Check if 'data' key exists (not 'success' key which doesn't exist in parse_mcp_result)
            if "data" in state_data and state_data["data"] is not None:
                current_state = state_data.get("data", {}).get("state")

                if current_state != initial_state:
                    elapsed = time.monotonic() - start_time
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
    poll_interval: float = 0.5,
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
        The parsed tool result that satisfied the predicate.

    Raises:
        TimeoutError: If the predicate is not satisfied within the timeout.
    """
    start_time = time.monotonic()
    last_data: dict[str, Any] = {}

    logger.info(f"⏳ Waiting for {description} (timeout: {timeout}s)")

    while True:
        # Call the tool — catch tool/network errors to keep polling
        try:
            result = await mcp_client.call_tool(tool_name, arguments)
            last_data = parse_mcp_result(result)
        except Exception as e:
            logger.debug(f"⚠️ Error calling {tool_name}: {e}")
            if time.monotonic() - start_time >= timeout:
                raise TimeoutError(
                    f"{description}: timed out after {timeout}s (last error: {e})"
                ) from e
            await asyncio.sleep(poll_interval)
            continue

        # Skip MCP error responses — entity may not be registered yet
        if last_data.get("success") is False:
            logger.debug(
                f"⚠️ {tool_name} returned error: {last_data.get('error')}, retrying..."
            )
            if time.monotonic() - start_time >= timeout:
                raise TimeoutError(
                    f"{description}: timed out after {timeout}s "
                    f"(last MCP error: {last_data.get('error')})"
                )
            await asyncio.sleep(poll_interval)
            continue

        # Run predicate OUTSIDE try/except so bugs (TypeError, KeyError) propagate
        if predicate(last_data):
            elapsed = time.monotonic() - start_time
            logger.info(f"✅ {description} satisfied after {elapsed:.1f}s")
            return last_data

        if time.monotonic() - start_time >= timeout:
            raise TimeoutError(
                f"{description}: timed out after {timeout}s (predicate not satisfied)"
            )
        await asyncio.sleep(poll_interval)


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
        poll_interval: float = 0.5,
        description: str = "tool result",
    ) -> dict[str, Any]:
        """Wait for tool result to satisfy predicate."""
        return await wait_for_tool_result(
            self.client,
            tool_name,
            arguments,
            predicate,
            timeout=timeout,
            poll_interval=poll_interval,
            description=description,
        )
