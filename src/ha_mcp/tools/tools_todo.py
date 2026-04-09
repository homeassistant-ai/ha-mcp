"""
Todo/Shopping List management tools for Home Assistant MCP server.

This module provides tools for managing Home Assistant todo lists including:
- Listing all todo list entities
- Getting items from a todo list
- Creating and updating todo items
- Removing items from a todo list
"""

import logging
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error

logger = logging.getLogger(__name__)


def register_todo_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant todo list management tools."""

    @mcp.tool(
        tags={"Todo Lists"},
        annotations={"idempotentHint": True, "readOnlyHint": True, "title": "Get Todo"},
    )
    @log_tool_usage
    async def ha_get_todo(
        entity_id: Annotated[
            str | None,
            Field(
                description="Todo list entity ID (e.g., 'todo.shopping_list'). "
                "If omitted, lists all todo list entities.",
                default=None,
            ),
        ] = None,
        status: Annotated[
            Literal["needs_action", "completed"] | None,
            Field(
                description="Filter items by status: 'needs_action' for incomplete, 'completed' for done. "
                "Only applies when entity_id is provided.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Get todo lists or items - list all todo lists or get items from a specific list.

        Without an entity_id: Lists all todo list entities in Home Assistant.
        With an entity_id: Gets items from that specific todo list, optionally filtered by status.

        **LISTING TODO LISTS (entity_id omitted):**
        Returns all entities in the 'todo' domain, including shopping lists
        and any other todo-type integrations.

        Each todo list includes:
        - entity_id: The unique identifier (e.g., 'todo.shopping_list')
        - friendly_name: Human-readable name
        - state: Number of incomplete items or current status

        **GETTING TODO ITEMS (entity_id provided):**
        Retrieves items from the specified todo list.

        Status filter values:
        - needs_action: Items that still need to be done
        - completed: Items that have been marked as done
        - None (default): Returns all items regardless of status

        Item properties:
        - uid: Unique identifier for the item
        - summary: The item text/description
        - status: Current status (needs_action or completed)
        - description: Optional detailed description
        - due: Optional due date (if supported)

        EXAMPLES:
        - List all todo lists: ha_get_todo()
        - Get all items: ha_get_todo("todo.shopping_list")
        - Get incomplete items: ha_get_todo("todo.shopping_list", status="needs_action")
        - Get completed items: ha_get_todo("todo.shopping_list", status="completed")

        USE CASES:
        - "What todo lists do I have?"
        - "Show me my shopping list"
        - "What's on my todo list?"
        - "Show completed items"
        """
        try:
            # List mode - no entity_id provided
            if entity_id is None:
                # Get all states and filter by todo domain
                states = await client.get_states()

                todo_lists = []
                for state in states:
                    eid = state.get("entity_id", "")
                    if eid.startswith("todo."):
                        todo_lists.append(
                            {
                                "entity_id": eid,
                                "friendly_name": state.get("attributes", {}).get(
                                    "friendly_name", eid
                                ),
                                "state": state.get("state"),
                                "icon": state.get("attributes", {}).get("icon"),
                                "supported_features": state.get("attributes", {}).get(
                                    "supported_features"
                                ),
                            }
                        )

                return {
                    "success": True,
                    "count": len(todo_lists),
                    "todo_lists": todo_lists,
                    "message": f"Found {len(todo_lists)} todo list(s)",
                }

            # Get items mode - entity_id provided
            # Validate entity_id format
            if not entity_id.startswith("todo."):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid entity_id: {entity_id}. Must start with 'todo.'",
                        context={"entity_id": entity_id},
                        suggestions=[
                            "Use ha_get_todo() without entity_id to find valid todo list entity IDs"
                        ],
                    )
                )

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
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        result.get("error", "Failed to get todo items"),
                        context={"entity_id": entity_id},
                        suggestions=[
                            "Verify the entity_id exists using ha_get_todo()",
                            "Check Home Assistant WebSocket connection",
                        ],
                    )
                )

        except ToolError:
            raise
        except Exception as e:
            context: dict[str, Any] = {}
            if entity_id:
                context["entity_id"] = entity_id
            suggestions = (
                [
                    "Check Home Assistant connection",
                    "Verify entity_id is correct",
                    "Use ha_get_todo() to find valid todo lists",
                ]
                if entity_id
                else [
                    "Check Home Assistant connection",
                    "Verify todo integration is enabled",
                ]
            )
            exception_to_structured_error(
                e, context=context or None, suggestions=suggestions
            )

    @mcp.tool(
        tags={"Todo Lists"},
        annotations={"destructiveHint": True, "title": "Set Todo Item"},
    )
    @log_tool_usage
    async def ha_set_todo_item(
        entity_id: Annotated[
            str,
            Field(description="Todo list entity ID (e.g., 'todo.shopping_list')"),
        ],
        summary: Annotated[
            str | None,
            Field(
                description="Item text/name. Required when creating a new item. "
                "Ignored in update mode — use 'rename' to change the item name.",
                default=None,
            ),
        ] = None,
        item: Annotated[
            str | None,
            Field(
                description="Existing item to update - can be the item UID or the exact item summary/name. "
                "When provided, operates in update mode. When omitted, creates a new item.",
                default=None,
            ),
        ] = None,
        status: Annotated[
            Literal["needs_action", "completed"] | None,
            Field(
                description="Item status: 'completed' to mark done, 'needs_action' to mark incomplete. "
                "Only used in update mode.",
                default=None,
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(
                description="Detailed description for the item",
                default=None,
            ),
        ] = None,
        due_date: Annotated[
            str | None,
            Field(
                description="Due date in YYYY-MM-DD format (e.g., '2024-12-25')",
                default=None,
            ),
        ] = None,
        due_datetime: Annotated[
            str | None,
            Field(
                description="Due datetime in ISO format (e.g., '2024-12-25T14:00:00'). "
                "Overrides due_date if both provided.",
                default=None,
            ),
        ] = None,
        rename: Annotated[
            str | None,
            Field(
                description="New name/summary for an existing item. Only used in update mode.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Create or update a todo item in Home Assistant.

        WITHOUT item parameter (create mode):
        Creates a new item. summary is required.

        WITH item parameter (update mode):
        Updates an existing item identified by UID or exact name.
        At least one update field (rename, status, description, due_date, due_datetime) is required.

        EXAMPLES:
        - Add item: ha_set_todo_item("todo.shopping_list", summary="Buy milk")
        - Add with description: ha_set_todo_item("todo.shopping_list", summary="Buy milk", description="2% organic")
        - Add with due date: ha_set_todo_item("todo.tasks", summary="Pay bills", due_date="2024-12-31")
        - Complete item: ha_set_todo_item("todo.shopping_list", item="Buy milk", status="completed")
        - Rename item: ha_set_todo_item("todo.tasks", item="Old task", rename="New task name")
        - Update due date: ha_set_todo_item("todo.tasks", item="Pay bills", due_date="2024-12-31")
        - Reopen item: ha_set_todo_item("todo.tasks", item="Task to redo", status="needs_action")

        NOTE: Not all todo integrations support all features (description, due dates).
        The Shopping List integration only supports summary.
        """
        # Validate entity_id format
        if not entity_id.startswith("todo."):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid entity_id: {entity_id}. Must start with 'todo.'",
                    context={"entity_id": entity_id},
                    suggestions=[
                        "Use ha_get_todo() to find valid todo list entity IDs"
                    ],
                )
            )

        # Route: create mode (no item) vs update mode (item provided)
        if item is None:
            # --- Create mode ---
            if rename is not None or status is not None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "rename and status are only valid when updating an existing item (provide 'item' parameter)",
                        context={"entity_id": entity_id},
                        suggestions=[
                            "To create a new item, provide only 'summary' (and optionally 'due_date', 'description')",
                            "To update an existing item, include the 'item' parameter with the item name",
                        ],
                    )
                )
            if not summary:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        "summary is required when creating a new item (no item parameter provided)",
                        context={"entity_id": entity_id},
                        suggestions=["Provide a summary for the new todo item"],
                    )
                )

            try:
                service_data: dict[str, Any] = {
                    "entity_id": entity_id,
                    "item": summary,
                }

                if description:
                    service_data["description"] = description
                if due_datetime:
                    service_data["due_datetime"] = due_datetime
                elif due_date:
                    service_data["due_date"] = due_date

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

            except ToolError:
                raise
            except Exception as e:
                exception_to_structured_error(
                    e,
                    context={"entity_id": entity_id, "item": summary},
                    suggestions=[
                        "Verify the entity_id exists using ha_get_todo()",
                        "Check if the todo list supports adding items",
                        "Some todo lists may not support description or due dates",
                    ],
                )
        else:
            # --- Update mode ---
            if not any([rename, status, description, due_date, due_datetime]):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        "At least one update field must be provided (rename, status, description, due_date, or due_datetime)",
                        context={"entity_id": entity_id, "item": item},
                        suggestions=[
                            "Specify what to update, e.g., status='completed' to mark item done"
                        ],
                    )
                )

            try:
                service_data = {
                    "entity_id": entity_id,
                    "item": item,
                }

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

                result = await client.call_service("todo", "update_item", service_data)

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

            except ToolError:
                raise
            except Exception as e:
                exception_to_structured_error(
                    e,
                    context={"entity_id": entity_id, "item": item},
                    suggestions=[
                        "Verify the item exists using ha_get_todo()",
                        "Check if you're using the correct item name or UID",
                        "Some todo lists may not support all update operations",
                    ],
                )

    @mcp.tool(
        tags={"Todo Lists"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "title": "Remove Todo Item",
        },
    )
    @log_tool_usage
    async def ha_remove_todo_item(
        entity_id: Annotated[
            str,
            Field(description="Todo list entity ID (e.g., 'todo.shopping_list')"),
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
        - Use the item's UID (from ha_get_todo)
        - Or use the exact item summary/name text

        EXAMPLES:
        - Remove by name: ha_remove_todo_item("todo.shopping_list", "Buy milk")
        - Remove by UID: ha_remove_todo_item("todo.shopping_list", "abc123-uid")

        USE CASES:
        - "Remove milk from my shopping list"
        - "Delete the eggs item"
        - "Clear 'call mom' from my todo"

        WARNING: This permanently removes the item. To mark as completed instead,
        use ha_set_todo_item() with status="completed".
        """
        # Validate entity_id format
        if not entity_id.startswith("todo."):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid entity_id: {entity_id}. Must start with 'todo.'",
                    context={"entity_id": entity_id},
                    suggestions=[
                        "Use ha_get_todo() to find valid todo list entity IDs"
                    ],
                )
            )

        try:
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

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"entity_id": entity_id, "item": item},
                suggestions=[
                    "Verify the item exists using ha_get_todo()",
                    "Check if you're using the correct item name or UID",
                    "Make sure the item hasn't already been removed",
                ],
            )
