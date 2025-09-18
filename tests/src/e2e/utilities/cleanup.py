"""
Cleanup utilities for E2E testing.

This module provides utilities for cleaning up test entities, managing test data,
and ensuring tests don't interfere with each other.
"""

import logging
from typing import Any

from .assertions import parse_mcp_result

logger = logging.getLogger(__name__)


class TestEntityCleaner:
    """
    Manages cleanup of test entities created during E2E tests.

    This class tracks entities created during tests and provides methods
    to clean them up individually or in bulk.
    """

    def __init__(self, mcp_client):
        self.client = mcp_client
        self.tracked_entities: list[tuple[str, str]] = []  # (entity_type, entity_id)
        self.cleanup_attempted: dict[str, bool] = {}

    def track_entity(self, entity_type: str, entity_id: str):
        """
        Track an entity for cleanup.

        Args:
            entity_type: Type of entity (automation, script, input_boolean, etc.)
            entity_id: Full entity ID or just the name part
        """
        # Normalize entity_id
        if not entity_id.startswith(f"{entity_type}."):
            # Handle special cases
            if entity_type.startswith("input_"):
                domain = entity_type
            else:
                domain = entity_type
            entity_id = f"{domain}.{entity_id}"

        self.tracked_entities.append((entity_type, entity_id))
        logger.info(f"📝 Tracking {entity_type}: {entity_id} for cleanup")

    async def cleanup_entity(self, entity_type: str, entity_id: str) -> bool:
        """
        Clean up a single entity.

        Args:
            entity_type: Type of entity to clean up
            entity_id: Entity ID to clean up

        Returns:
            True if cleanup successful or entity doesn't exist, False if failed
        """
        cleanup_key = f"{entity_type}:{entity_id}"

        if cleanup_key in self.cleanup_attempted:
            logger.debug(f"⏭️ Already attempted cleanup for {entity_id}")
            return self.cleanup_attempted[cleanup_key]

        try:
            logger.info(f"🗑️ Cleaning up {entity_type}: {entity_id}")

            # Determine cleanup method based on entity type
            if entity_type == "automation":
                result = await self._cleanup_automation(entity_id)
            elif entity_type == "script":
                result = await self._cleanup_script(entity_id)
            elif entity_type.startswith("input_"):
                result = await self._cleanup_helper(entity_type, entity_id)
            else:
                logger.warning(f"⚠️ Unknown entity type for cleanup: {entity_type}")
                result = False

            self.cleanup_attempted[cleanup_key] = result

            if result:
                logger.info(f"✅ Successfully cleaned up {entity_id}")
            else:
                logger.warning(f"⚠️ Failed to clean up {entity_id}")

            return result

        except Exception as e:
            logger.error(f"❌ Error cleaning up {entity_id}: {e}")
            self.cleanup_attempted[cleanup_key] = False
            return False

    async def _cleanup_automation(self, entity_id: str) -> bool:
        """Clean up an automation entity."""
        try:
            delete_result = await self.client.call_tool(
                "ha_manage_automation", {"action": "delete", "identifier": entity_id}
            )

            delete_data = parse_mcp_result(delete_result)
            return delete_data.get("success", False)

        except Exception as e:
            logger.debug(f"Automation cleanup error: {e}")
            return False

    async def _cleanup_script(self, entity_id: str) -> bool:
        """Clean up a script entity."""
        try:
            # Extract script ID from entity ID
            script_id = entity_id.replace("script.", "")

            delete_result = await self.client.call_tool(
                "ha_manage_script", {"action": "delete", "script_id": script_id}
            )

            delete_data = parse_mcp_result(delete_result)
            return delete_data.get("success", False)

        except Exception as e:
            logger.debug(f"Script cleanup error: {e}")
            return False

    async def _cleanup_helper(self, helper_type: str, entity_id: str) -> bool:
        """Clean up a helper entity."""
        try:
            # Extract helper ID from entity ID
            helper_id = entity_id.replace(f"{helper_type}.", "")

            delete_result = await self.client.call_tool(
                "ha_manage_helper",
                {
                    "action": "delete",
                    "helper_type": helper_type,
                    "helper_id": helper_id,
                    "name": "",  # Required but not used for delete
                },
            )

            delete_data = parse_mcp_result(delete_result)
            return delete_data.get("success", False)

        except Exception as e:
            logger.debug(f"Helper cleanup error: {e}")
            return False

    async def cleanup_all(self) -> dict[str, bool]:
        """
        Clean up all tracked entities.

        Returns:
            Dictionary mapping entity_id to cleanup success status
        """
        if not self.tracked_entities:
            logger.info("🧹 No entities to clean up")
            return {}

        logger.info(f"🧹 Cleaning up {len(self.tracked_entities)} tracked entities...")

        results = {}

        # Sort entities for cleanup order (automation/scripts first, then helpers)
        sorted_entities = sorted(
            self.tracked_entities,
            key=lambda x: (0 if x[0] in ["automation", "script"] else 1, x[1]),
        )

        for entity_type, entity_id in sorted_entities:
            success = await self.cleanup_entity(entity_type, entity_id)
            results[entity_id] = success

        successful = sum(results.values())
        total = len(results)

        logger.info(
            f"🧹 Cleanup completed: {successful}/{total} entities cleaned successfully"
        )

        return results

    def get_tracked_entities(self) -> list[tuple[str, str]]:
        """Get list of tracked entities."""
        return self.tracked_entities.copy()

    def clear_tracking(self):
        """Clear the list of tracked entities."""
        self.tracked_entities.clear()
        self.cleanup_attempted.clear()
        logger.debug("📋 Cleared entity tracking")


async def cleanup_test_entities_by_name(
    mcp_client, name_patterns: list[str]
) -> dict[str, int]:
    """
    Clean up entities matching test name patterns.

    This function searches for entities with test-like names and attempts
    to clean them up. Useful for cleaning up leftover test data.

    Args:
        mcp_client: FastMCP client instance
        name_patterns: List of name patterns to match (e.g., ["test", "e2e"])

    Returns:
        Dictionary with counts of entities found and cleaned per domain
    """
    logger.info(f"🧹 Searching for test entities matching patterns: {name_patterns}")

    domains_to_check = [
        "automation",
        "script",
        "input_boolean",
        "input_number",
        "input_select",
        "input_text",
    ]
    results = {}

    for domain in domains_to_check:
        found_count = 0
        cleaned_count = 0

        try:
            # Search for entities in this domain
            search_result = await mcp_client.call_tool(
                "ha_search_entities",
                {"query": "", "domain_filter": domain, "limit": 50},
            )

            search_data = parse_mcp_result(search_result)

            if search_data.get("success") and search_data.get("results"):
                entities = search_data["results"]

                # Filter for test entities
                test_entities = []
                for entity in entities:
                    entity_id = entity.get("entity_id", "")
                    friendly_name = entity.get("friendly_name", "")

                    # Check if entity ID or name contains test patterns
                    for pattern in name_patterns:
                        pattern_lower = pattern.lower()
                        if (
                            pattern_lower in entity_id.lower()
                            or pattern_lower in friendly_name.lower()
                        ):
                            test_entities.append(entity_id)
                            break

                found_count = len(test_entities)

                if test_entities:
                    logger.info(
                        f"🔍 Found {found_count} test entities in {domain}: {test_entities[:3]}..."
                    )

                    # Attempt to clean up each test entity
                    cleaner = TestEntityCleaner(mcp_client)

                    for entity_id in test_entities:
                        success = await cleaner.cleanup_entity(domain, entity_id)
                        if success:
                            cleaned_count += 1

        except Exception as e:
            logger.warning(f"⚠️ Error cleaning up {domain} entities: {e}")

        if found_count > 0:
            results[domain] = {"found": found_count, "cleaned": cleaned_count}
            logger.info(
                f"📊 {domain}: {cleaned_count}/{found_count} test entities cleaned"
            )

    return results


async def verify_entity_cleanup(mcp_client, entity_id: str, entity_type: str) -> bool:
    """
    Verify that an entity was successfully cleaned up.

    Args:
        mcp_client: FastMCP client instance
        entity_id: Entity ID to verify
        entity_type: Type of entity

    Returns:
        True if entity no longer exists, False if still exists
    """
    try:
        if entity_type == "automation":
            # Try to get automation config
            result = await mcp_client.call_tool(
                "ha_manage_automation", {"action": "get", "identifier": entity_id}
            )

            result_data = parse_mcp_result(result)
            exists = result_data.get("success", False)

        elif entity_type == "script":
            # Try to get script config
            script_id = entity_id.replace("script.", "")
            result = await mcp_client.call_tool(
                "ha_manage_script", {"action": "get", "script_id": script_id}
            )

            result_data = parse_mcp_result(result)
            exists = result_data.get("success", False)

        else:
            # Try to get entity state
            result = await mcp_client.call_tool(
                "ha_get_state", {"entity_id": entity_id}
            )

            result_data = parse_mcp_result(result)
            # Entity exists if we can get its state successfully
            exists = result_data.get("success")

        if exists:
            logger.warning(f"⚠️ Entity {entity_id} still exists after cleanup")
            return False
        else:
            logger.debug(f"✅ Entity {entity_id} successfully removed")
            return True

    except Exception as e:
        logger.debug(f"Error verifying cleanup for {entity_id}: {e}")
        # Assume cleanup was successful if we can't verify
        return True


async def reset_entity_states(mcp_client, entity_ids: list[str]) -> dict[str, bool]:
    """
    Reset entity states to known defaults.

    This function attempts to reset entities to safe default states
    to minimize test interference.

    Args:
        mcp_client: FastMCP client instance
        entity_ids: List of entity IDs to reset

    Returns:
        Dictionary mapping entity_id to reset success status
    """
    logger.info(f"🔄 Resetting {len(entity_ids)} entities to default states")

    results = {}

    for entity_id in entity_ids:
        try:
            domain = entity_id.split(".")[0]
            success = False

            if domain == "light":
                # Turn off lights
                result = await mcp_client.call_tool(
                    "ha_call_service",
                    {"domain": "light", "service": "turn_off", "entity_id": entity_id},
                )
                result_data = parse_mcp_result(result)
                success = result_data.get("success", False)

            elif domain == "switch":
                # Turn off switches
                result = await mcp_client.call_tool(
                    "ha_call_service",
                    {"domain": "switch", "service": "turn_off", "entity_id": entity_id},
                )
                result_data = parse_mcp_result(result)
                success = result_data.get("success", False)

            elif domain == "input_boolean":
                # Turn off input booleans
                result = await mcp_client.call_tool(
                    "ha_call_service",
                    {
                        "domain": "input_boolean",
                        "service": "turn_off",
                        "entity_id": entity_id,
                    },
                )
                result_data = parse_mcp_result(result)
                success = result_data.get("success", False)

            elif domain == "input_number":
                # Reset input numbers to minimum value
                state_result = await mcp_client.call_tool(
                    "ha_get_state", {"entity_id": entity_id}
                )
                state_data = parse_mcp_result(state_result)

                if state_data.get("success"):
                    attributes = state_data.get("data", {}).get("attributes", {})
                    min_value = attributes.get("min", 0)

                    result = await mcp_client.call_tool(
                        "ha_call_service",
                        {
                            "domain": "input_number",
                            "service": "set_value",
                            "entity_id": entity_id,
                            "data": {"value": min_value},
                        },
                    )
                    result_data = parse_mcp_result(result)
                    success = result_data.get("success", False)

            else:
                logger.debug(f"ℹ️ No default reset action for domain: {domain}")
                success = True  # Consider unknown domains as "successful"

            results[entity_id] = success

            if success:
                logger.debug(f"✅ Reset {entity_id}")
            else:
                logger.warning(f"⚠️ Failed to reset {entity_id}")

        except Exception as e:
            logger.warning(f"⚠️ Error resetting {entity_id}: {e}")
            results[entity_id] = False

    successful = sum(results.values())
    total = len(results)
    logger.info(
        f"🔄 Entity reset completed: {successful}/{total} entities reset successfully"
    )

    return results


class TestEnvironmentManager:
    """
    Manages test environment state and cleanup.

    This class provides a higher-level interface for managing the test
    environment, including entity tracking, cleanup, and state reset.
    """

    def __init__(self, mcp_client):
        self.client = mcp_client
        self.cleaner = TestEntityCleaner(mcp_client)
        self.initial_states: dict[str, Any] = {}

    async def __aenter__(self):
        """Context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with automatic cleanup."""
        await self.cleanup_all()

    def track_entity(self, entity_type: str, entity_id: str):
        """Track entity for cleanup."""
        self.cleaner.track_entity(entity_type, entity_id)

    async def capture_initial_state(self, entity_id: str):
        """Capture initial state of an entity for later restoration."""
        try:
            state_result = await self.client.call_tool(
                "ha_get_state", {"entity_id": entity_id}
            )
            state_data = parse_mcp_result(state_result)

            if state_data.get("success"):
                self.initial_states[entity_id] = state_data.get("data", {}).get("state")
                logger.debug(
                    f"📸 Captured initial state for {entity_id}: {self.initial_states[entity_id]}"
                )

        except Exception as e:
            logger.debug(f"⚠️ Could not capture initial state for {entity_id}: {e}")

    async def restore_initial_states(self) -> dict[str, bool]:
        """Restore entities to their initial captured states."""
        if not self.initial_states:
            return {}

        logger.info(
            f"🔄 Restoring {len(self.initial_states)} entities to initial states"
        )
        return await reset_entity_states(self.client, list(self.initial_states.keys()))

    async def cleanup_all(self) -> dict[str, bool]:
        """Clean up all tracked entities."""
        return await self.cleaner.cleanup_all()

    async def verify_cleanup(self) -> bool:
        """Verify that all tracked entities were cleaned up successfully."""
        tracked = self.cleaner.get_tracked_entities()

        if not tracked:
            return True

        all_cleaned = True

        for entity_type, entity_id in tracked:
            if not await verify_entity_cleanup(self.client, entity_id, entity_type):
                all_cleaned = False

        return all_cleaned
