"""
Label management tools for Home Assistant.

This module provides tools for listing, creating, updating, and deleting
Home Assistant labels, as well as assigning labels to entities.
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)


def register_label_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant label management tools."""

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["label"], "title": "List Labels"})
    @log_tool_usage
    async def ha_config_list_labels() -> dict[str, Any]:
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
        - List all labels: ha_config_list_labels()

        Use ha_config_set_label() to create or update labels.
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

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["label"], "title": "Get Label Details"})
    @log_tool_usage
    async def ha_config_get_label(
        label_id: Annotated[
            str,
            Field(description="ID of the label to retrieve"),
        ],
    ) -> dict[str, Any]:
        """
        Get a specific Home Assistant label by ID.

        Returns complete configuration for a single label including:
        - ID (label_id)
        - Name
        - Color (optional)
        - Icon (optional)
        - Description (optional)

        EXAMPLES:
        - Get label: ha_config_get_label("my_label_id")

        Use ha_config_list_labels() to find available label IDs.
        """
        try:
            # Get all labels and find the one we want
            message: dict[str, Any] = {
                "type": "config/label_registry/list",
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                labels = result.get("result", [])
                label = next(
                    (lbl for lbl in labels if lbl.get("label_id") == label_id), None
                )

                if label:
                    return {
                        "success": True,
                        "label": label,
                        "message": f"Found label: {label.get('name', label_id)}",
                    }
                else:
                    return {
                        "success": False,
                        "error": f"Label not found: {label_id}",
                        "label_id": label_id,
                    }
            else:
                return {
                    "success": False,
                    "error": f"Failed to get label: {result.get('error', 'Unknown error')}",
                    "label_id": label_id,
                }

        except Exception as e:
            logger.error(f"Error getting label: {e}")
            return {
                "success": False,
                "error": f"Failed to get label: {str(e)}",
                "label_id": label_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                    "Use ha_config_list_labels() to find valid label IDs",
                ],
            }

    @mcp.tool(annotations={"idempotentHint": True, "tags": ["label"], "title": "Create or Update Label"})
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

        After creating a label, use ha_assign_label() to assign it to entities.
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
                    "For updates, verify the label_id exists using ha_config_list_labels()",
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

        Use ha_config_list_labels() to find label IDs.

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
                    "Verify the label_id exists using ha_config_list_labels()",
                ],
            }

    @mcp.tool(annotations={"idempotentHint": True, "tags": ["label"], "title": "Assign Label to Entity"})
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

        Use ha_config_list_labels() to find available label IDs.
        Use ha_search_entities() to find entity IDs.

        **NOTE:** This sets the complete list of labels for the entity. Any labels
        not included in the list will be removed from the entity.
        """
        try:
            # Handle different input types for labels parameter
            parsed_labels: list[str]

            if isinstance(labels, list):
                # Already a list
                parsed_labels = labels
            elif isinstance(labels, str):
                # Could be a plain string label ID or a JSON array string
                try:
                    # Try to parse as JSON array first
                    import json
                    parsed = json.loads(labels)
                    if isinstance(parsed, list):
                        parsed_labels = parsed
                    else:
                        # JSON parsed but not a list, treat as single label ID
                        parsed_labels = [labels]
                except json.JSONDecodeError:
                    # Not valid JSON, treat as a single plain label ID
                    parsed_labels = [labels]
            else:
                # Unexpected type, return error
                return {
                    "success": False,
                    "error": f"Invalid labels parameter type: expected str or list[str], got {type(labels).__name__}",
                    "entity_id": entity_id,
                }

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

        except Exception as e:
            logger.error(f"Error assigning labels: {e}")
            return {
                "success": False,
                "error": f"Failed to assign labels: {str(e)}",
                "entity_id": entity_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify the entity_id exists using ha_search_entities()",
                    "Verify the label IDs exist using ha_config_list_labels()",
                ],
            }
