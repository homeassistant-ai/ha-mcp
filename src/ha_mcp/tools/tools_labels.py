"""
Label management tools for Home Assistant.

This module provides tools for listing, creating, updating, and deleting
Home Assistant labels, as well as assigning labels to entities.
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from .helpers import log_tool_usage
from .util_helpers import parse_string_list_param

logger = logging.getLogger(__name__)


def register_label_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant label management tools."""

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_list_labels() -> dict[str, Any]:
        """
        List all Home Assistant labels with their configurations.

        Returns complete configuration for all labels including:
        - ID (label_id)
        - Name
        - Color (optional)
        - Icon (optional)
        - Description (optional)

        Labels are a flexible tagging system in Home Assistant that can be used
        to categorize and organize entities, devices, and areas.

        EXAMPLES:
        - List all labels: ha_list_labels()

        Use ha_create_label() to create new labels.
        Use ha_assign_label() to assign labels to entities.
        """
        try:
            message: dict[str, Any] = {
                "type": "config/label_registry/list",
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                labels = result.get("result", [])
                return {
                    "success": True,
                    "count": len(labels),
                    "labels": labels,
                    "message": f"Found {len(labels)} label(s)",
                }
            else:
                return {
                    "success": False,
                    "error": f"Failed to list labels: {result.get('error', 'Unknown error')}",
                }

        except Exception as e:
            logger.error(f"Error listing labels: {e}")
            return {
                "success": False,
                "error": f"Failed to list labels: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_create_label(
        name: Annotated[str, Field(description="Display name for the label")],
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
        Create a new Home Assistant label.

        Labels are a flexible tagging system that can be applied to entities,
        devices, and areas for organization and automation purposes.

        EXAMPLES:
        - Create simple label: ha_create_label("Critical")
        - Create colored label: ha_create_label("Outdoor", color="green")
        - Create label with icon: ha_create_label("Battery Powered", icon="mdi:battery")
        - Create full label: ha_create_label("Security", color="red", icon="mdi:shield", description="Security-related devices")

        After creating a label, use ha_assign_label() to assign it to entities.
        """
        try:
            message: dict[str, Any] = {
                "type": "config/label_registry/create",
                "name": name,
            }

            if color:
                message["color"] = color
            if icon:
                message["icon"] = icon
            if description:
                message["description"] = description

            result = await client.send_websocket_message(message)

            if result.get("success"):
                label_data = result.get("result", {})
                return {
                    "success": True,
                    "label_id": label_data.get("label_id"),
                    "label_data": label_data,
                    "message": f"Successfully created label: {name}",
                }
            else:
                return {
                    "success": False,
                    "error": f"Failed to create label: {result.get('error', 'Unknown error')}",
                    "name": name,
                }

        except Exception as e:
            logger.error(f"Error creating label: {e}")
            return {
                "success": False,
                "error": f"Failed to create label: {str(e)}",
                "name": name,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify the label name is valid",
                    "Check if a label with this name already exists",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_update_label(
        label_id: Annotated[
            str,
            Field(description="ID of the label to update"),
        ],
        name: Annotated[
            str | None,
            Field(
                description="New display name for the label",
                default=None,
            ),
        ] = None,
        color: Annotated[
            str | None,
            Field(
                description="New color for the label (e.g., 'red', 'blue', or hex like '#FF5733')",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="New Material Design Icon (e.g., 'mdi:tag', 'mdi:label')",
                default=None,
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(
                description="New description for the label",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Update an existing Home Assistant label.

        Updates the properties of a label. Only provided fields will be updated;
        fields not specified will retain their current values.

        EXAMPLES:
        - Update name: ha_update_label("my_label_id", name="New Name")
        - Update color: ha_update_label("my_label_id", color="blue")
        - Update multiple: ha_update_label("my_label_id", name="Updated", icon="mdi:star", color="gold")

        Use ha_list_labels() to find label IDs.
        """
        try:
            # Check if at least one field to update is provided
            if not any([name, color, icon, description]):
                return {
                    "success": False,
                    "error": "At least one field (name, color, icon, or description) must be provided for update",
                    "label_id": label_id,
                }

            message: dict[str, Any] = {
                "type": "config/label_registry/update",
                "label_id": label_id,
            }

            if name is not None:
                message["name"] = name
            if color is not None:
                message["color"] = color
            if icon is not None:
                message["icon"] = icon
            if description is not None:
                message["description"] = description

            result = await client.send_websocket_message(message)

            if result.get("success"):
                label_data = result.get("result", {})
                return {
                    "success": True,
                    "label_id": label_id,
                    "label_data": label_data,
                    "message": f"Successfully updated label: {label_id}",
                }
            else:
                return {
                    "success": False,
                    "error": f"Failed to update label: {result.get('error', 'Unknown error')}",
                    "label_id": label_id,
                }

        except Exception as e:
            logger.error(f"Error updating label: {e}")
            return {
                "success": False,
                "error": f"Failed to update label: {str(e)}",
                "label_id": label_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify the label_id exists using ha_list_labels()",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_delete_label(
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
        - Delete label: ha_delete_label("my_label_id")

        Use ha_list_labels() to find label IDs.

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
                    "Verify the label_id exists using ha_list_labels()",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_assign_label(
        entity_id: Annotated[
            str,
            Field(description="Entity ID to assign labels to (e.g., 'light.living_room')"),
        ],
        labels: Annotated[
            str | list[str],
            Field(
                description="Label ID(s) to assign. Can be a single label ID string, "
                "a list of label IDs, or a JSON array string (e.g., '[\"label1\", \"label2\"]')"
            ),
        ],
    ) -> dict[str, Any]:
        """
        Assign labels to an entity.

        Sets the labels for an entity. This replaces any existing labels on the entity
        with the provided list. To add to existing labels, first get the current labels
        and include them in the new list.

        EXAMPLES:
        - Assign single label: ha_assign_label("light.bedroom", "critical")
        - Assign multiple labels: ha_assign_label("light.bedroom", ["critical", "outdoor"])
        - Clear all labels: ha_assign_label("light.bedroom", [])

        Use ha_list_labels() to find available label IDs.
        Use ha_search_entities() to find entity IDs.

        **NOTE:** This sets the complete list of labels for the entity. Any labels
        not included in the list will be removed from the entity.
        """
        try:
            # Parse labels parameter - can be string, list, or JSON string
            parsed_labels = parse_string_list_param(labels, "labels")

            # Ensure we have a list
            if parsed_labels is None:
                parsed_labels = []
            elif isinstance(parsed_labels, str):
                parsed_labels = [parsed_labels]

            message: dict[str, Any] = {
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
                "labels": parsed_labels,
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                entity_entry = result.get("result", {}).get("entity_entry", {})
                return {
                    "success": True,
                    "entity_id": entity_id,
                    "labels": parsed_labels,
                    "entity_data": entity_entry,
                    "message": f"Successfully assigned {len(parsed_labels)} label(s) to {entity_id}",
                }
            else:
                return {
                    "success": False,
                    "error": f"Failed to assign labels: {result.get('error', 'Unknown error')}",
                    "entity_id": entity_id,
                    "labels": parsed_labels,
                }

        except ValueError as e:
            return {
                "success": False,
                "error": f"Invalid labels parameter: {e}",
                "entity_id": entity_id,
            }
        except Exception as e:
            logger.error(f"Error assigning labels: {e}")
            return {
                "success": False,
                "error": f"Failed to assign labels: {str(e)}",
                "entity_id": entity_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify the entity_id exists using ha_search_entities()",
                    "Verify the label IDs exist using ha_list_labels()",
                ],
            }
