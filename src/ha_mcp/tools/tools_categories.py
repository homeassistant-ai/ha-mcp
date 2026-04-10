"""
Category management tools for Home Assistant.

This module provides tools for listing, creating, updating, and deleting
Home Assistant categories. Categories are domain-scoped organizational groups
(e.g., for automations, scripts, scenes, helpers) introduced in Home Assistant 2024.4.

To assign categories to entities, use ha_set_entity(categories=...).
"""

import logging
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)

logger = logging.getLogger(__name__)


class CategoryTools:
    """Category management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_config_get_category",
        tags={"Labels & Categories"},
        annotations={"idempotentHint": True, "readOnlyHint": True, "title": "Get Category"},
    )
    @log_tool_usage
    async def ha_config_get_category(
        self,
        scope: Annotated[
            str,
            Field(
                description="Domain scope for categories (e.g., 'automation', 'script', 'scene', 'helpers').",
            ),
        ],
        category_id: Annotated[
            str | None,
            Field(
                description="ID of the category to retrieve. If omitted, lists all categories for the scope.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Get category info - list all categories for a scope or get a specific one by ID.

        Without a category_id: Lists all Home Assistant categories for the given scope.
        With a category_id: Returns configuration for that specific category.

        Categories are domain-scoped organizational groups for automations, scripts, scenes, and helpers.

        CATEGORY PROPERTIES:
        - ID (category_id), Name
        - Icon (optional)

        EXAMPLES:
        - List automation categories: ha_config_get_category("automation")
        - List script categories: ha_config_get_category("script")
        - List helper categories: ha_config_get_category("helpers")
        - Get specific category: ha_config_get_category("automation", category_id="my_category_id")

        Use ha_config_set_category() to create or update categories.
        Use ha_set_entity(categories={"automation": "category_id"}) to assign categories to entities.
        """
        try:
            message: dict[str, Any] = {
                "type": "config/category_registry/list",
                "scope": scope,
            }

            result = await self._client.send_websocket_message(message)

            if not result.get("success"):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        result.get("error", "Failed to get categories"),
                        context={"scope": scope, "category_id": category_id},
                    )
                )

            categories = result.get("result", [])

            # List mode - return all categories
            if category_id is None:
                return {
                    "success": True,
                    "count": len(categories),
                    "categories": categories,
                    "scope": scope,
                    "message": f"Found {len(categories)} category(ies) for scope '{scope}'",
                }

            # Get mode - find specific category
            category = next(
                (cat for cat in categories if cat.get("category_id") == category_id),
                None,
            )

            if category:
                return {
                    "success": True,
                    "category_id": category_id,
                    "category": category,
                    "scope": scope,
                    "message": f"Found category: {category.get('name', category_id)}",
                }
            else:
                available_ids = [cat.get("category_id") for cat in categories[:10]]
                raise_tool_error(
                    create_error_response(
                        ErrorCode.ENTITY_NOT_FOUND,
                        f"Category not found: {category_id}",
                        context={
                            "category_id": category_id,
                            "scope": scope,
                            "available_category_ids": available_ids,
                        },
                        suggestions=[
                            f"Use ha_config_get_category('{scope}') without category_id to see all categories"
                        ],
                    )
                )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting categories: {e}")
            exception_to_structured_error(
                e,
                context={"scope": scope, "category_id": category_id},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                    "Ensure scope is valid (e.g., 'automation', 'script', 'scene', 'helpers')",
                ],
            )

    @tool(
        name="ha_config_set_category",
        tags={"Labels & Categories"},
        annotations={"destructiveHint": True, "title": "Create or Update Category"},
    )
    @log_tool_usage
    async def ha_config_set_category(
        self,
        name: Annotated[str, Field(description="Display name for the category")],
        scope: Annotated[
            str,
            Field(
                description="Domain scope for the category (e.g., 'automation', 'script', 'scene', 'helpers').",
            ),
        ],
        category_id: Annotated[
            str | None,
            Field(
                description="Category ID for updates. If not provided, creates a new category.",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:tag', 'mdi:label')",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant category.

        Creates a new category if category_id is not provided, or updates an existing category if category_id is provided.

        Categories are domain-scoped organizational groups for automations, scripts,
        scenes, and helpers. Unlike labels (which are cross-domain), categories are
        specific to a single domain scope.

        EXAMPLES:
        - Create automation category: ha_config_set_category("Lighting", scope="automation")
        - Create with icon: ha_config_set_category("Security", scope="automation", icon="mdi:shield")
        - Update category: ha_config_set_category("Updated Name", scope="automation", category_id="my_category_id")

        After creating a category, use ha_set_entity(categories={"automation": "category_id"}) to assign it.
        """
        try:
            # Determine if this is a create or update
            action = "update" if category_id else "create"

            message: dict[str, Any] = {
                "type": f"config/category_registry/{action}",
                "scope": scope,
                "name": name,
            }

            if action == "update":
                message["category_id"] = category_id

            # Add optional fields only if they are explicitly provided (not None)
            if icon is not None:
                message["icon"] = icon

            result = await self._client.send_websocket_message(message)

            if result.get("success"):
                category_data = result.get("result", {})
                action_past = "created" if action == "create" else "updated"
                return {
                    "success": True,
                    "category_id": category_data.get("category_id"),
                    "category_data": category_data,
                    "scope": scope,
                    "message": f"Successfully {action_past} category: {name}",
                }
            else:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to {action} category: {result.get('error', 'Unknown error')}",
                        context={
                            "name": name,
                            "scope": scope,
                            "category_id": category_id,
                        },
                    )
                )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error setting category {name!r}: {e}")
            exception_to_structured_error(
                e,
                context={"name": name, "scope": scope, "category_id": category_id},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify the category name is valid",
                    "For updates, verify the category_id exists using ha_config_get_category()",
                ],
            )

    @tool(
        name="ha_config_remove_category",
        tags={"Labels & Categories"},
        annotations={"destructiveHint": True, "idempotentHint": True, "title": "Remove Category"},
    )
    @log_tool_usage
    async def ha_config_remove_category(
        self,
        scope: Annotated[
            str,
            Field(
                description="Domain scope for the category (e.g., 'automation', 'script', 'scene', 'helpers').",
            ),
        ],
        category_id: Annotated[
            str,
            Field(description="ID of the category to delete"),
        ],
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant category.

        Removes the category from the category registry for the given scope
        (e.g., 'automation', 'script', 'scene', 'helpers').
        This will also remove the category assignment from all entities in that scope.

        EXAMPLES:
        - Delete category: ha_config_remove_category("automation", "my_category_id")

        Use ha_config_get_category() to find category IDs.

        **WARNING:** Deleting a category will remove it from all assigned entities.
        This action cannot be undone.
        """
        try:
            message: dict[str, Any] = {
                "type": "config/category_registry/delete",
                "scope": scope,
                "category_id": category_id,
            }

            result = await self._client.send_websocket_message(message)

            if result.get("success"):
                return {
                    "success": True,
                    "category_id": category_id,
                    "scope": scope,
                    "message": f"Successfully deleted category: {category_id}",
                }
            else:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to delete category: {result.get('error', 'Unknown error')}",
                        context={"category_id": category_id, "scope": scope},
                    )
                )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error removing category {category_id!r}: {e}")
            exception_to_structured_error(
                e,
                context={"category_id": category_id, "scope": scope},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify the category_id exists using ha_config_get_category()",
                ],
            )


def register_category_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant category management tools."""
    register_tool_methods(mcp, CategoryTools(client))
