"""
Label management tools for Home Assistant.

This module provides tools for listing, creating, updating, and deleting
Home Assistant labels. To assign labels to entities, use ha_set_entity(labels=...).
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)


def register_label_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant label management tools."""

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["label"], "title": "Get Label"})
    @log_tool_usage
    async def ha_config_get_label(
        label_id: Annotated[
            str | None,
            Field(
                description="ID of the label to retrieve. If omitted, lists all labels.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Get label info - list all labels or get a specific one by ID.

        Without a label_id: Lists all Home Assistant labels with their configurations.
        With a label_id: Returns configuration for that specific label.

        LABEL PROPERTIES:
        - ID (label_id), Name
        - Color (optional), Icon (optional), Description (optional)

        EXAMPLES:
        - List all labels: ha_config_get_label()
        - Get specific label: ha_config_get_label("my_label_id")

        Use ha_config_set_label() to create or update labels.
        Use ha_set_entity(labels=["label1", "label2"]) to assign labels to entities.
        """
        try:
            message: dict[str, Any] = {
                "type": "config/label_registry/list",
            }

            result = await client.send_websocket_message(message)

            if not result.get("success"):
                return {
                    "success": False,
                    "error": f"Failed to get labels: {result.get('error', 'Unknown error')}",
                    "label_id": label_id,
                }

            labels = result.get("result", [])

            # List mode - return all labels
            if label_id is None:
                return {
                    "success": True,
                    "count": len(labels),
                    "labels": labels,
                    "message": f"Found {len(labels)} label(s)",
                }

            # Get mode - find specific label
            label = next(
                (lbl for lbl in labels if lbl.get("label_id") == label_id), None
            )

            if label:
                return {
                    "success": True,
                    "label_id": label_id,
                    "label": label,
                    "message": f"Found label: {label.get('name', label_id)}",
                }
            else:
                available_ids = [lbl.get("label_id") for lbl in labels[:10]]
                return {
                    "success": False,
                    "error": f"Label not found: {label_id}",
                    "label_id": label_id,
                    "available_label_ids": available_ids,
                    "suggestion": "Use ha_config_get_label() without label_id to see all labels",
                }

        except Exception as e:
            logger.error(f"Error getting labels: {e}")
            return {
                "success": False,
                "error": f"Failed to get labels: {str(e)}",
                "label_id": label_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                ],
            }

    @mcp.tool(annotations={"destructiveHint": True, "tags": ["label"], "title": "Create or Update Label"})
    @log_tool_usage
    async def ha_config_set_label(
        name: Annotated[str, Field(description="Display name for the label")],
        label_id: Annotated[
            str | None,
            Field(
                description="Label ID for updates. If not provided, creates a new label.",
                default=None,
            ),
        ] = None,
        color: Annotated[
            str | None,
            Field(
                description="Color for the label (e.g., 'red', 'blue', 'green', or hex like '#FF5733')",
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
        description: Annotated[
            str | None,
            Field(
                description="Description of the label's purpose",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant label.

        Creates a new label if label_id is not provided, or updates an existing label if label_id is provided.

        Labels are a flexible tagging system that can be applied to entities,
        devices, and areas for organization and automation purposes.

        EXAMPLES:
        - Create simple label: ha_config_set_label("Critical")
        - Create colored label: ha_config_set_label("Outdoor", color="green")
        - Create label with icon: ha_config_set_label("Battery Powered", icon="mdi:battery")
        - Create full label: ha_config_set_label("Security", color="red", icon="mdi:shield", description="Security-related devices")
        - Update label: ha_config_set_label("Updated Name", label_id="my_label_id", color="blue")

        After creating a label, use ha_set_entity(labels=["label_id"]) to assign it to entities.
        """
        try:
            # Determine if this is a create or update
            action = "update" if label_id else "create"

            message: dict[str, Any] = {
                "type": f"config/label_registry/{action}",
                "name": name,
            }

            if action == "update":
                message["label_id"] = label_id
                # Note: name is always provided as it's a required parameter
                # The validation of at least one field is satisfied by name being required

            # Add optional fields only if they are explicitly provided (not None)
            if color is not None:
                message["color"] = color
            if icon is not None:
                message["icon"] = icon
            if description is not None:
                message["description"] = description

            result = await client.send_websocket_message(message)

            if result.get("success"):
                label_data = result.get("result", {})
                action_past = "created" if action == "create" else "updated"
                return {
                    "success": True,
                    "label_id": label_data.get("label_id"),
                    "label_data": label_data,
                    "message": f"Successfully {action_past} label: {name}",
                }
            else:
                return {
                    "success": False,
                    "error": f"Failed to {action} label: {result.get('error', 'Unknown error')}",
                    "name": name,
                }

        except Exception as e:
            logger.error(f"Error setting label: {e}")
            return {
                "success": False,
                "error": f"Failed to set label: {str(e)}",
                "name": name,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify the label name is valid",
                    "For updates, verify the label_id exists using ha_config_get_label()",
                ],
            }

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "tags": ["label"], "title": "Remove Label"})
    @log_tool_usage
    async def ha_config_remove_label(
        label_id: Annotated[
            str,
            Field(description="ID of the label to delete"),
        ],
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant label.

        Removes the label from the label registry. This will also remove the label
        from all entities, devices, and areas that have it assigned.

        EXAMPLES:
        - Delete label: ha_config_remove_label("my_label_id")

        Use ha_config_get_label() to find label IDs.

        **WARNING:** Deleting a label will remove it from all assigned entities.
        This action cannot be undone.
        """
        try:
            message: dict[str, Any] = {
                "type": "config/label_registry/delete",
                "label_id": label_id,
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                return {
                    "success": True,
                    "label_id": label_id,
                    "message": f"Successfully deleted label: {label_id}",
                }
            else:
                return {
                    "success": False,
                    "error": f"Failed to delete label: {result.get('error', 'Unknown error')}",
                    "label_id": label_id,
                }

        except Exception as e:
            logger.error(f"Error deleting label: {e}")
            return {
                "success": False,
                "error": f"Failed to delete label: {str(e)}",
                "label_id": label_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify the label_id exists using ha_config_get_label()",
                ],
            }

