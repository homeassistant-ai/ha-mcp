"""
Todo/Shopping List management tools for Home Assistant MCP server.

This module provides tools for managing Home Assistant todo lists including:
- Listing all todo list entities
- Getting items from a todo list
- Adding items to a todo list
- Updating/completing todo items
- Removing items from a todo list
"""

import logging
from typing import Annotated, Any, Literal

from pydantic import Field

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)


def register_todo_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant todo list management tools."""

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["todo"], "title": "List Todo Lists"})
    @log_tool_usage
    async def ha_list_todo_lists() -> dict[str, Any]:
        """
        List all todo list entities in Home Assistant.

        Returns all entities in the 'todo' domain, including shopping lists
        and any other todo-type integrations.

        Each todo list entity includes:
        - entity_id: The unique identifier (e.g., 'todo.shopping_list')
        - friendly_name: Human-readable name
        - state: Number of incomplete items or current status

        EXAMPLES:
        - List all todo lists: ha_list_todo_lists()

        USE CASES:
        - "What todo lists do I have?"
        - "Show me my shopping lists"
        - "Find my grocery list"
        """
        try:
            # Get all states and filter by todo domain
            states = await client.get_states()

            todo_lists = []
            for state in states:
                entity_id = state.get("entity_id", "")
                if entity_id.startswith("todo."):
                    todo_lists.append({
                        "entity_id": entity_id,
                        "friendly_name": state.get("attributes", {}).get(
                            "friendly_name", entity_id
                        ),
                        "state": state.get("state"),
                        "icon": state.get("attributes", {}).get("icon"),
                        "supported_features": state.get("attributes", {}).get(
                            "supported_features"
                        ),
                    })

            return {
                "success": True,
                "count": len(todo_lists),
                "todo_lists": todo_lists,
                "message": f"Found {len(todo_lists)} todo list(s)",
            }

        except Exception as e:
            logger.error(f"Error listing todo lists: {e}")
            return {
                "success": False,
                "error": f"Failed to list todo lists: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify todo integration is enabled",
                ],
            }

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["todo"], "title": "Get Todo Items"})
    @log_tool_usage
    async def ha_get_todo_items(
        entity_id: Annotated[
            str,
            Field(
                description="Todo list entity ID (e.g., 'todo.shopping_list')"
            ),
        ],
        status: Annotated[
            Literal["needs_action", "completed"] | None,
            Field(
                description="Filter by status: 'needs_action' for incomplete items, 'completed' for done items. None returns all items.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Get items from a Home Assistant todo list.

        Retrieves all items from the specified todo list, optionally filtered by status.

        STATUS VALUES:
        - needs_action: Items that still need to be done
        - completed: Items that have been marked as done
        - None (default): Returns all items regardless of status

        ITEM PROPERTIES:
        - uid: Unique identifier for the item
        - summary: The item text/description
        - status: Current status (needs_action or completed)
        - description: Optional detailed description
        - due: Optional due date (if supported)

        EXAMPLES:
        - Get all items: ha_get_todo_items("todo.shopping_list")
        - Get incomplete items: ha_get_todo_items("todo.shopping_list", status="needs_action")
        - Get completed items: ha_get_todo_items("todo.shopping_list", status="completed")

        USE CASES:
        - "Show me my shopping list"
        - "What's on my todo list?"
        - "Show completed items"
        """
        try:
            # Validate entity_id format
            if not entity_id.startswith("todo."):
                return {
                    "success": False,
                    "error": f"Invalid entity_id: {entity_id}. Must start with 'todo.'",
                    "suggestion": "Use ha_list_todo_lists() to find valid todo list entity IDs",
                }

            # Use WebSocket to get todo items
            message: dict[str, Any] = {
                "type": "todo/item/list",
                "entity_id": entity_id,
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                items = result.get("result", {}).get("items", [])

                # Filter by status if specified
                if status:
                    items = [item for item in items if item.get("status") == status]

                return {
                    "success": True,
                    "entity_id": entity_id,
                    "status_filter": status,
                    "count": len(items),
                    "items": items,
                    "message": f"Found {len(items)} item(s) in {entity_id}",
                }
            else:
                error = result.get("error", "Unknown error")
                return {
                    "success": False,
                    "error": f"Failed to get todo items: {error}",
                    "entity_id": entity_id,
                    "suggestions": [
                        "Verify the entity_id exists using ha_list_todo_lists()",
                        "Check Home Assistant WebSocket connection",
                    ],
                }

        except Exception as e:
            logger.error(f"Error getting todo items: {e}")
            return {
                "success": False,
                "error": f"Failed to get todo items: {str(e)}",
                "entity_id": entity_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify entity_id is correct",
                    "Use ha_list_todo_lists() to find valid todo lists",
                ],
            }

    @mcp.tool(annotations={"destructiveHint": True, "tags": ["todo"], "title": "Add Todo Item"})
    @log_tool_usage
    async def ha_add_todo_item(
        entity_id: Annotated[
            str,
            Field(
                description="Todo list entity ID (e.g., 'todo.shopping_list')"
            ),
        ],
        summary: Annotated[
            str,
            Field(description="Item text/name to add (e.g., 'Buy milk')"),
        ],
        description: Annotated[
            str | None,
            Field(
                description="Optional detailed description for the item",
                default=None,
            ),
        ] = None,
        due_date: Annotated[
            str | None,
            Field(
                description="Optional due date in YYYY-MM-DD format (e.g., '2024-12-25')",
                default=None,
            ),
        ] = None,
        due_datetime: Annotated[
            str | None,
            Field(
                description="Optional due datetime in ISO format (e.g., '2024-12-25T14:00:00')",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Add an item to a Home Assistant todo list.

        Creates a new item in the specified todo list with optional description and due date.

        PARAMETERS:
        - entity_id: The todo list to add to (e.g., 'todo.shopping_list')
        - summary: The item text (required)
        - description: Optional detailed description
        - due_date: Optional due date (YYYY-MM-DD format)
        - due_datetime: Optional due datetime (ISO format, overrides due_date if both provided)

        EXAMPLES:
        - Add simple item: ha_add_todo_item("todo.shopping_list", "Buy milk")
        - Add with description: ha_add_todo_item("todo.shopping_list", "Buy milk", description="2% organic")
        - Add with due date: ha_add_todo_item("todo.tasks", "Pay bills", due_date="2024-12-31")

        USE CASES:
        - "Add milk to my shopping list"
        - "Add 'call mom' to my todo list"
        - "Remind me to pay rent by the 1st"

        NOTE: Not all todo integrations support all features (description, due dates).
        The Shopping List integration only supports summary.
        """
        try:
            # Validate entity_id format
            if not entity_id.startswith("todo."):
                return {
                    "success": False,
                    "error": f"Invalid entity_id: {entity_id}. Must start with 'todo.'",
                    "suggestion": "Use ha_list_todo_lists() to find valid todo list entity IDs",
                }

            # Build service data
            service_data: dict[str, Any] = {
                "entity_id": entity_id,
                "item": summary,
            }

            # Add optional fields if provided
            if description:
                service_data["description"] = description
            if due_datetime:
                service_data["due_datetime"] = due_datetime
            elif due_date:
                service_data["due_date"] = due_date

            # Call the service
            result = await client.call_service("todo", "add_item", service_data)

            return {
                "success": True,
                "entity_id": entity_id,
                "item": summary,
                "description": description,
                "due_date": due_date,
                "due_datetime": due_datetime,
                "result": result,
                "message": f"Successfully added '{summary}' to {entity_id}",
            }

        except Exception as e:
            logger.error(f"Error adding todo item: {e}")
            return {
                "success": False,
                "error": f"Failed to add todo item: {str(e)}",
                "entity_id": entity_id,
                "item": summary,
                "suggestions": [
                    "Verify the entity_id exists using ha_list_todo_lists()",
                    "Check if the todo list supports adding items",
                    "Some todo lists may not support description or due dates",
                ],
            }

    @mcp.tool(annotations={"destructiveHint": True, "tags": ["todo"], "title": "Update Todo Item"})
    @log_tool_usage
    async def ha_update_todo_item(
        entity_id: Annotated[
            str,
            Field(
                description="Todo list entity ID (e.g., 'todo.shopping_list')"
            ),
        ],
        item: Annotated[
            str,
            Field(
                description="Item to update - can be the item UID or the exact item summary/name"
            ),
        ],
        rename: Annotated[
            str | None,
            Field(
                description="New name/summary for the item",
                default=None,
            ),
        ] = None,
        status: Annotated[
            Literal["needs_action", "completed"] | None,
            Field(
                description="New status: 'completed' to mark done, 'needs_action' to mark incomplete",
                default=None,
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(
                description="New description for the item",
                default=None,
            ),
        ] = None,
        due_date: Annotated[
            str | None,
            Field(
                description="New due date in YYYY-MM-DD format",
                default=None,
            ),
        ] = None,
        due_datetime: Annotated[
            str | None,
            Field(
                description="New due datetime in ISO format",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Update or complete a todo item in Home Assistant.

        Modifies an existing item in the specified todo list. Can be used to:
        - Mark items as completed or incomplete
        - Rename items
        - Update descriptions and due dates

        IDENTIFYING ITEMS:
        - Use the item's UID (from ha_get_todo_items)
        - Or use the exact item summary/name text

        STATUS VALUES:
        - completed: Mark the item as done
        - needs_action: Mark the item as not done (reopen)

        EXAMPLES:
        - Complete item: ha_update_todo_item("todo.shopping_list", "Buy milk", status="completed")
        - Rename item: ha_update_todo_item("todo.tasks", "Old task", rename="New task name")
        - Update due date: ha_update_todo_item("todo.tasks", "Pay bills", due_date="2024-12-31")
        - Reopen item: ha_update_todo_item("todo.tasks", "Task to redo", status="needs_action")

        USE CASES:
        - "Mark milk as bought"
        - "Complete the eggs on my shopping list"
        - "Change the due date for my task"
        - "Rename 'call mom' to 'video call with mom'"

        NOTE: At least one update field (rename, status, description, due_date, due_datetime) must be provided.
        """
        try:
            # Validate entity_id format
            if not entity_id.startswith("todo."):
                return {
                    "success": False,
                    "error": f"Invalid entity_id: {entity_id}. Must start with 'todo.'",
                    "suggestion": "Use ha_list_todo_lists() to find valid todo list entity IDs",
                }

            # Validate at least one update field is provided
            if not any([rename, status, description, due_date, due_datetime]):
                return {
                    "success": False,
                    "error": "At least one update field must be provided (rename, status, description, due_date, or due_datetime)",
                    "suggestion": "Specify what to update, e.g., status='completed' to mark item done",
                }

            # Build service data
            service_data: dict[str, Any] = {
                "entity_id": entity_id,
                "item": item,
            }

            # Add update fields if provided
            if rename:
                service_data["rename"] = rename
            if status:
                service_data["status"] = status
            if description:
                service_data["description"] = description
            if due_datetime:
                service_data["due_datetime"] = due_datetime
            elif due_date:
                service_data["due_date"] = due_date

            # Call the service
            result = await client.call_service("todo", "update_item", service_data)

            # Build response message
            updates = []
            if rename:
                updates.append(f"renamed to '{rename}'")
            if status:
                updates.append(f"status set to '{status}'")
            if description:
                updates.append("description updated")
            if due_date or due_datetime:
                updates.append("due date updated")

            update_msg = ", ".join(updates) if updates else "updated"

            return {
                "success": True,
                "entity_id": entity_id,
                "item": item,
                "updates": {
                    "rename": rename,
                    "status": status,
                    "description": description,
                    "due_date": due_date,
                    "due_datetime": due_datetime,
                },
                "result": result,
                "message": f"Successfully updated '{item}' in {entity_id}: {update_msg}",
            }

        except Exception as e:
            logger.error(f"Error updating todo item: {e}")
            return {
                "success": False,
                "error": f"Failed to update todo item: {str(e)}",
                "entity_id": entity_id,
                "item": item,
                "suggestions": [
                    "Verify the item exists using ha_get_todo_items()",
                    "Check if you're using the correct item name or UID",
                    "Some todo lists may not support all update operations",
                ],
            }

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "tags": ["todo"], "title": "Remove Todo Item"})
    @log_tool_usage
    async def ha_remove_todo_item(
        entity_id: Annotated[
            str,
            Field(
                description="Todo list entity ID (e.g., 'todo.shopping_list')"
            ),
        ],
        item: Annotated[
            str,
            Field(
                description="Item to remove - can be the item UID or the exact item summary/name"
            ),
        ],
    ) -> dict[str, Any]:
        """
        Remove an item from a Home Assistant todo list.

        Permanently deletes an item from the specified todo list.

        IDENTIFYING ITEMS:
        - Use the item's UID (from ha_get_todo_items)
        - Or use the exact item summary/name text

        EXAMPLES:
        - Remove by name: ha_remove_todo_item("todo.shopping_list", "Buy milk")
        - Remove by UID: ha_remove_todo_item("todo.shopping_list", "abc123-uid")

        USE CASES:
        - "Remove milk from my shopping list"
        - "Delete the eggs item"
        - "Clear 'call mom' from my todo"

        WARNING: This permanently removes the item. To mark as completed instead,
        use ha_update_todo_item() with status="completed".
        """
        try:
            # Validate entity_id format
            if not entity_id.startswith("todo."):
                return {
                    "success": False,
                    "error": f"Invalid entity_id: {entity_id}. Must start with 'todo.'",
                    "suggestion": "Use ha_list_todo_lists() to find valid todo list entity IDs",
                }

            # Build service data
            service_data: dict[str, Any] = {
                "entity_id": entity_id,
                "item": item,
            }

            # Call the service
            result = await client.call_service("todo", "remove_item", service_data)

            return {
                "success": True,
                "entity_id": entity_id,
                "item": item,
                "result": result,
                "message": f"Successfully removed '{item}' from {entity_id}",
            }

        except Exception as e:
            logger.error(f"Error removing todo item: {e}")
            return {
                "success": False,
                "error": f"Failed to remove todo item: {str(e)}",
                "entity_id": entity_id,
                "item": item,
                "suggestions": [
                    "Verify the item exists using ha_get_todo_items()",
                    "Check if you're using the correct item name or UID",
                    "Make sure the item hasn't already been removed",
                ],
            }
