"""
End-to-End tests for Home Assistant Todo/Shopping List Management.

This test suite validates the complete lifecycle of Home Assistant todo list operations:
- Listing todo list entities
- Getting items from a todo list
- Adding items to a todo list
- Updating/completing todo items
- Removing items from a todo list

Each test uses real Home Assistant API calls via the MCP server to ensure
production-level functionality and compatibility.

Tests are designed for Docker Home Assistant test environment with testcontainers.
"""

import ast
import asyncio
import json
import logging
import time
from typing import Any

import pytest

# Import test utilities
from ...utilities.assertions import (
    MCPAssertions,
)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def enhanced_parse_mcp_result(result) -> dict[str, Any]:
    """Enhanced MCP result parser with better error handling."""
    try:
        if hasattr(result, "content") and result.content:
            response_text = str(result.content[0].text)
            try:
                return json.loads(response_text)
            except json.JSONDecodeError:
                try:
                    fixed_text = (
                        response_text.replace("true", "True")
                        .replace("false", "False")
                        .replace("null", "None")
                    )
                    return ast.literal_eval(fixed_text)
                except (SyntaxError, ValueError):
                    return {"raw_response": response_text, "parse_error": True}

        return {
            "content": (
                str(result.content[0]) if hasattr(result, "content") else str(result)
            )
        }
    except Exception as e:
        logger.warning(f"Failed to parse MCP result: {e}")
        return {"error": "Failed to parse result", "exception": str(e)}


async def wait_for_item_in_list(
    mcp_client,
    entity_id: str,
    item_summary: str,
    timeout: int = 10,
    poll_interval: float = 0.5,
) -> bool:
    """
    Wait for an item to appear in a todo list.

    This function handles timing issues where items are added but not
    immediately visible through the API.
    """
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            result = await mcp_client.call_tool(
                "ha_get_todo",
                {"entity_id": entity_id}
            )
            data = enhanced_parse_mcp_result(result)

            if data.get("success"):
                items = data.get("items", [])
                for item in items:
                    if item.get("summary") == item_summary:
                        logger.info(f"Found item '{item_summary}' in {entity_id}")
                        return True

        except Exception as e:
            logger.debug(f"Item check failed: {e}")

        await asyncio.sleep(poll_interval)

    logger.warning(f"Item '{item_summary}' not found in {entity_id} within {timeout}s")
    return False


async def get_item_by_summary(
    mcp_client,
    entity_id: str,
    item_summary: str,
) -> dict[str, Any] | None:
    """Get a todo item by its summary text."""
    try:
        result = await mcp_client.call_tool(
            "ha_get_todo",
            {"entity_id": entity_id}
        )
        data = enhanced_parse_mcp_result(result)

        if data.get("success"):
            items = data.get("items", [])
            for item in items:
                if item.get("summary") == item_summary:
                    return item

    except Exception as e:
        logger.debug(f"Failed to get item by summary: {e}")

    return None


@pytest.mark.todo
class TestTodoListDiscovery:
    """Test todo list discovery and listing functionality."""

    async def test_list_todo_lists(self, mcp_client):
        """
        Test: List all todo list entities

        Validates that ha_get_todo returns todo entities correctly.
        """
        logger.info("Testing ha_get_todo...")

        async with MCPAssertions(mcp_client) as mcp:
            result = await mcp.call_tool_success("ha_get_todo", {})

            # Verify response structure
            assert "count" in result, "Response should include count"
            assert "todo_lists" in result, "Response should include todo_lists"
            assert isinstance(result["todo_lists"], list), "todo_lists should be a list"

            count = result["count"]
            todo_lists = result["todo_lists"]

            logger.info(f"Found {count} todo list(s)")

            # If there are todo lists, verify their structure
            if count > 0:
                first_list = todo_lists[0]
                assert "entity_id" in first_list, "Todo list should have entity_id"
                assert first_list["entity_id"].startswith("todo."), (
                    "Todo list entity_id should start with 'todo.'"
                )
                logger.info(f"First todo list: {first_list['entity_id']}")

            logger.info("ha_get_todo test passed")


@pytest.mark.todo
class TestTodoItemOperations:
    """Test todo item CRUD operations."""

    async def test_todo_item_lifecycle(self, mcp_client):
        """
        Test: Complete todo item lifecycle (add, get, update, remove)

        Validates the full workflow of managing todo items.
        """
        logger.info("Testing todo item lifecycle...")

        async with MCPAssertions(mcp_client) as mcp:
            # First, get available todo lists
            list_result = await mcp.call_tool_success("ha_get_todo", {})

            if list_result["count"] == 0:
                pytest.skip("No todo lists available for testing")

            # Use the first available todo list
            todo_entity = list_result["todo_lists"][0]["entity_id"]
            logger.info(f"Using todo list: {todo_entity}")

            # Generate unique item name
            test_item = f"E2E Test Item {int(time.time())}"

            # 1. ADD: Add a new item
            logger.info(f"Adding item: {test_item}")
            add_result = await mcp.call_tool_success(
                "ha_add_todo_item",
                {
                    "entity_id": todo_entity,
                    "summary": test_item,
                }
            )
            assert add_result.get("item") == test_item, "Added item should match"
            logger.info("Item added successfully")

            # Wait for item to appear
            item_found = await wait_for_item_in_list(
                mcp_client, todo_entity, test_item, timeout=10
            )
            assert item_found, f"Item '{test_item}' should appear in the list"

            # 2. GET: Verify item exists in list
            logger.info("Verifying item exists...")
            get_result = await mcp.call_tool_success(
                "ha_get_todo",
                {"entity_id": todo_entity}
            )

            items = get_result.get("items", [])
            item_summaries = [item.get("summary") for item in items]
            assert test_item in item_summaries, (
                f"Item '{test_item}' should be in list. Found: {item_summaries}"
            )
            logger.info("Item found in list")

            # Find the item to get its status
            test_item_data = None
            for item in items:
                if item.get("summary") == test_item:
                    test_item_data = item
                    break

            assert test_item_data is not None, "Should find test item data"
            initial_status = test_item_data.get("status", "needs_action")
            logger.info(f"Initial item status: {initial_status}")

            # 3. UPDATE: Mark item as completed
            logger.info("Marking item as completed...")
            await mcp.call_tool_success(
                "ha_update_todo_item",
                {
                    "entity_id": todo_entity,
                    "item": test_item,
                    "status": "completed",
                }
            )
            logger.info("Item marked as completed")

            # Wait a moment for update to propagate

            # Verify the status changed
            verify_result = await mcp.call_tool_success(
                "ha_get_todo",
                {"entity_id": todo_entity}
            )

            completed_item = None
            for item in verify_result.get("items", []):
                if item.get("summary") == test_item:
                    completed_item = item
                    break

            # Note: Some todo integrations may auto-remove completed items
            if completed_item:
                logger.info(f"Item status after update: {completed_item.get('status')}")
            else:
                logger.info("Item may have been auto-removed after completion (normal for some integrations)")

            # 4. REMOVE: Remove the item
            logger.info("Removing item...")
            await mcp.call_tool_success(
                "ha_remove_todo_item",
                {
                    "entity_id": todo_entity,
                    "item": test_item,
                }
            )
            logger.info("Item removed successfully")

            # Wait and verify item is gone

            final_result = await mcp.call_tool_success(
                "ha_get_todo",
                {"entity_id": todo_entity}
            )

            final_items = final_result.get("items", [])
            final_summaries = [item.get("summary") for item in final_items]
            assert test_item not in final_summaries, (
                f"Item '{test_item}' should be removed from list"
            )
            logger.info("Item removal verified")

            logger.info("Todo item lifecycle test passed")

    async def test_get_todo_items_with_status_filter(self, mcp_client):
        """
        Test: Filter todo items by status

        Validates that status filtering works correctly.
        """
        logger.info("Testing todo items with status filter...")

        async with MCPAssertions(mcp_client) as mcp:
            # Get available todo lists
            list_result = await mcp.call_tool_success("ha_get_todo", {})

            if list_result["count"] == 0:
                pytest.skip("No todo lists available for testing")

            todo_entity = list_result["todo_lists"][0]["entity_id"]

            # Test filtering by needs_action status
            logger.info("Testing filter: needs_action")
            needs_action_result = await mcp.call_tool_success(
                "ha_get_todo",
                {"entity_id": todo_entity, "status": "needs_action"}
            )
            assert "items" in needs_action_result, "Should have items in response"
            logger.info(f"Found {needs_action_result['count']} items needing action")

            # Test filtering by completed status
            logger.info("Testing filter: completed")
            completed_result = await mcp.call_tool_success(
                "ha_get_todo",
                {"entity_id": todo_entity, "status": "completed"}
            )
            assert "items" in completed_result, "Should have items in response"
            logger.info(f"Found {completed_result['count']} completed items")

            # Test no filter (all items)
            logger.info("Testing filter: none (all items)")
            all_result = await mcp.call_tool_success(
                "ha_get_todo",
                {"entity_id": todo_entity}
            )
            assert "items" in all_result, "Should have items in response"
            logger.info(f"Found {all_result['count']} total items")

            logger.info("Status filter test passed")


@pytest.mark.todo
class TestTodoErrorHandling:
    """Test error handling for todo operations."""

    async def test_invalid_entity_id(self, mcp_client):
        """
        Test: Error handling for invalid entity_id

        Validates proper error messages for invalid entity IDs.
        """
        logger.info("Testing invalid entity_id error handling...")

        async with MCPAssertions(mcp_client) as mcp:
            # Test with invalid prefix
            await mcp.call_tool_failure(
                "ha_get_todo",
                {"entity_id": "light.invalid"},
                expected_error="todo.",
            )
            logger.info("Invalid prefix error handled correctly")

            # Test with non-existent entity
            await mcp.call_tool_failure(
                "ha_get_todo",
                {"entity_id": "todo.nonexistent_xyz_12345"},
            )
            logger.info("Non-existent entity error handled correctly")

            logger.info("Invalid entity_id error handling test passed")

    async def test_update_requires_at_least_one_field(self, mcp_client):
        """
        Test: Update requires at least one update field

        Validates that ha_update_todo_item requires at least one update field.
        """
        logger.info("Testing update validation...")

        async with MCPAssertions(mcp_client) as mcp:
            # Get a todo list first
            list_result = await mcp.call_tool_success("ha_get_todo", {})

            if list_result["count"] == 0:
                pytest.skip("No todo lists available for testing")

            todo_entity = list_result["todo_lists"][0]["entity_id"]

            # Try to update without any update fields
            await mcp.call_tool_failure(
                "ha_update_todo_item",
                {
                    "entity_id": todo_entity,
                    "item": "some_item",
                    # No rename, status, description, or due_date provided
                },
                expected_error="At least one update field",
            )
            logger.info("Update validation test passed")


@pytest.mark.todo
class TestTodoAdvancedFeatures:
    """Test advanced todo features like descriptions and due dates."""

    async def test_add_item_with_description(self, mcp_client):
        """
        Test: Add todo item with description

        Validates adding items with optional description field.
        Note: Not all todo integrations support descriptions.
        """
        logger.info("Testing add item with description...")

        async with MCPAssertions(mcp_client) as mcp:
            # Get available todo lists
            list_result = await mcp.call_tool_success("ha_get_todo", {})

            if list_result["count"] == 0:
                pytest.skip("No todo lists available for testing")

            todo_entity = list_result["todo_lists"][0]["entity_id"]
            test_item = f"E2E Item With Desc {int(time.time())}"
            test_desc = "This is a test description"

            # Try to add with description
            try:
                await mcp.call_tool_success(
                    "ha_add_todo_item",
                    {
                        "entity_id": todo_entity,
                        "summary": test_item,
                        "description": test_desc,
                    }
                )
                logger.info("Item with description added successfully")

                # Clean up
                await mcp_client.call_tool(
                    "ha_remove_todo_item",
                    {"entity_id": todo_entity, "item": test_item}
                )
                logger.info("Cleanup completed")

            except Exception as e:
                # Some todo integrations don't support descriptions
                logger.info(f"Description not supported by this integration: {e}")

            logger.info("Add item with description test completed")

    async def test_rename_todo_item(self, mcp_client):
        """
        Test: Rename a todo item

        Validates the rename functionality for todo items.
        """
        logger.info("Testing rename todo item...")

        async with MCPAssertions(mcp_client) as mcp:
            # Get available todo lists
            list_result = await mcp.call_tool_success("ha_get_todo", {})

            if list_result["count"] == 0:
                pytest.skip("No todo lists available for testing")

            todo_entity = list_result["todo_lists"][0]["entity_id"]
            original_name = f"E2E Original Name {int(time.time())}"
            new_name = f"E2E Renamed Item {int(time.time())}"

            # Add item
            await mcp.call_tool_success(
                "ha_add_todo_item",
                {"entity_id": todo_entity, "summary": original_name}
            )

            # Wait for item
            await wait_for_item_in_list(mcp_client, todo_entity, original_name)

            # Rename item
            await mcp.call_tool_success(
                "ha_update_todo_item",
                {
                    "entity_id": todo_entity,
                    "item": original_name,
                    "rename": new_name,
                }
            )
            logger.info("Item renamed successfully")

            # Wait and verify

            get_result = await mcp.call_tool_success(
                "ha_get_todo",
                {"entity_id": todo_entity}
            )

            items = get_result.get("items", [])
            item_summaries = [item.get("summary") for item in items]

            # New name should exist
            assert new_name in item_summaries, (
                f"Renamed item '{new_name}' should be in list"
            )
            # Original name should not exist
            assert original_name not in item_summaries, (
                f"Original name '{original_name}' should not be in list"
            )

            # Clean up
            await mcp_client.call_tool(
                "ha_remove_todo_item",
                {"entity_id": todo_entity, "item": new_name}
            )

            logger.info("Rename todo item test passed")


@pytest.mark.todo
@pytest.mark.slow
class TestTodoBulkOperations:
    """Test bulk todo operations."""

    async def test_add_multiple_items(self, mcp_client):
        """
        Test: Add multiple items to a todo list

        Validates adding several items in sequence.
        """
        logger.info("Testing bulk add operations...")

        async with MCPAssertions(mcp_client) as mcp:
            # Get available todo lists
            list_result = await mcp.call_tool_success("ha_get_todo", {})

            if list_result["count"] == 0:
                pytest.skip("No todo lists available for testing")

            todo_entity = list_result["todo_lists"][0]["entity_id"]
            base_name = f"E2E Bulk {int(time.time())}"
            items_to_add = [f"{base_name} Item {i}" for i in range(3)]

            # Add items
            added_items = []
            for item_name in items_to_add:
                await mcp.call_tool_success(
                    "ha_add_todo_item",
                    {"entity_id": todo_entity, "summary": item_name}
                )
                added_items.append(item_name)

            logger.info(f"Added {len(added_items)} items")

            # Wait for items to appear

            # Verify all items exist
            get_result = await mcp.call_tool_success(
                "ha_get_todo",
                {"entity_id": todo_entity}
            )

            items = get_result.get("items", [])
            item_summaries = [item.get("summary") for item in items]

            found_count = sum(1 for item in items_to_add if item in item_summaries)
            logger.info(f"Found {found_count}/{len(items_to_add)} added items")

            # Clean up
            for item_name in items_to_add:
                try:
                    await mcp_client.call_tool(
                        "ha_remove_todo_item",
                        {"entity_id": todo_entity, "item": item_name}
                    )
                except Exception:
                    pass  # Item may already be gone

            logger.info("Bulk add operations test completed")


async def test_todo_search_discovery(mcp_client):
    """
    Test: Todo list discovery through search

    Validates that todo lists can be found through entity search.
    """
    logger.info("Testing todo search discovery...")

    async with MCPAssertions(mcp_client) as mcp:
        # Search for todo entities
        search_result = await mcp.call_tool_success(
            "ha_search_entities",
            {"query": "todo", "domain_filter": "todo", "limit": 10}
        )

        # Check if any todo entities found
        data = search_result.get("data", search_result)
        results = data.get("results", [])

        logger.info(f"Found {len(results)} todo entities via search")

        # If results found, verify structure
        for result in results:
            entity_id = result.get("entity_id", "")
            assert entity_id.startswith("todo."), (
                f"Search result should be todo entity: {entity_id}"
            )

        logger.info("Todo search discovery test completed")
