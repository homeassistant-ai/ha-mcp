"""
Entity group management tools for Home Assistant.

This module provides tools for listing, creating/updating, and removing
Home Assistant entity groups (old-style groups created via group.set service).
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)


def register_group_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant entity group management tools."""

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["group"], "title": "List Groups"})
    @log_tool_usage
    async def ha_config_list_groups() -> dict[str, Any]:
        """
        List all Home Assistant entity groups with their member entities.

        Returns all groups created via group.set service or YAML configuration,
        including:
        - Entity ID (group.xxx)
        - Friendly name
        - State (on/off based on member states)
        - Member entities
        - Icon (if set)
        - All mode (if all entities must be on)

        EXAMPLES:
        - List all groups: ha_config_list_groups()

        **NOTE:** This returns old-style groups (created via group.set or YAML).
        Platform-specific groups (light groups, cover groups) are separate entities.
        """
        try:
            # Get all entity states and filter for groups
            states = await client.get_states()

            groups = []
            for state in states:
                entity_id = state.get("entity_id", "")
                if entity_id.startswith("group."):
                    attributes = state.get("attributes", {})
                    groups.append(
                        {
                            "entity_id": entity_id,
                            "object_id": entity_id.removeprefix("group."),
                            "state": state.get("state"),
                            "friendly_name": attributes.get("friendly_name"),
                            "icon": attributes.get("icon"),
                            "entity_ids": attributes.get("entity_id", []),
                            "all": attributes.get("all", False),
                            "order": attributes.get("order"),
                        }
                    )

            # Sort by friendly name or entity_id
            groups.sort(
                key=lambda g: (g.get("friendly_name") or g.get("entity_id", "")).lower()
            )

            return {
                "success": True,
                "count": len(groups),
                "groups": groups,
                "message": f"Found {len(groups)} group(s)",
            }

        except Exception as e:
            logger.error(f"Error listing groups: {e}")
            return {
                "success": False,
                "error": f"Failed to list groups: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify REST API is accessible",
                ],
            }

    @mcp.tool(annotations={"destructiveHint": True, "tags": ["group"], "title": "Create or Update Group"})
    @log_tool_usage
    async def ha_config_set_group(
        object_id: Annotated[
            str,
            Field(
                description="Group identifier without 'group.' prefix (e.g., 'living_room_lights')"
            ),
        ],
        entities: Annotated[
            list[str] | None,
            Field(
                description="List of entity IDs for the group. Required when creating new group. When updating, replaces all entities (mutually exclusive with add_entities/remove_entities).",
                default=None,
            ),
        ] = None,
        name: Annotated[
            str | None,
            Field(
                description="Friendly display name for the group",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:lightbulb-group')",
                default=None,
            ),
        ] = None,
        all_on: Annotated[
            bool | None,
            Field(
                description="If True, all entities must be on for group to be on (default: False)",
                default=None,
            ),
        ] = None,
        add_entities: Annotated[
            list[str] | None,
            Field(
                description="Add these entities to an existing group (mutually exclusive with entities)",
                default=None,
            ),
        ] = None,
        remove_entities: Annotated[
            list[str] | None,
            Field(
                description="Remove these entities from an existing group (mutually exclusive with entities)",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant entity group.

        Uses the group.set service to create a new group or update an existing one.
        Groups are useful for organizing entities and controlling them together.

        **For NEW groups:** Provide object_id and entities (required).
        **For EXISTING groups:** Provide object_id and any fields to update.

        EXAMPLES:
        - Create group: ha_config_set_group("bedroom_lights", entities=["light.lamp", "light.ceiling"])
        - Create with name: ha_config_set_group("sensors", entities=["sensor.temp"], name="All Sensors")
        - Update name: ha_config_set_group("lights", name="Living Room Lights")
        - Add entities: ha_config_set_group("lights", add_entities=["light.extra"])
        - Remove entities: ha_config_set_group("lights", remove_entities=["light.old"])
        - Replace all entities: ha_config_set_group("lights", entities=["light.new1", "light.new2"])

        **NOTE:** entities, add_entities, and remove_entities are mutually exclusive.
        """
        try:
            # Validate object_id doesn't contain invalid characters
            if "." in object_id:
                return {
                    "success": False,
                    "error": f"Invalid object_id: '{object_id}'. Do not include 'group.' prefix or dots.",
                    "object_id": object_id,
                }

            # Check mutual exclusivity of entity operations
            entity_ops = [
                ("entities", entities),
                ("add_entities", add_entities),
                ("remove_entities", remove_entities),
            ]
            provided_ops = [
                (op_name, val) for op_name, val in entity_ops if val is not None
            ]

            if len(provided_ops) > 1:
                op_names = [op_name for op_name, _ in provided_ops]
                return {
                    "success": False,
                    "error": f"Only one of entities, add_entities, or remove_entities can be provided. Got: {op_names}",
                    "object_id": object_id,
                }

            # Validate non-empty lists
            if entities is not None and not entities:
                return {
                    "success": False,
                    "error": "Entities list cannot be empty",
                    "object_id": object_id,
                }
            if add_entities is not None and not add_entities:
                return {
                    "success": False,
                    "error": "add_entities list cannot be empty",
                    "object_id": object_id,
                }

            # Build service data
            service_data: dict[str, Any] = {
                "object_id": object_id,
            }

            if name is not None:
                service_data["name"] = name
            if icon is not None:
                service_data["icon"] = icon
            if all_on is not None:
                service_data["all"] = all_on
            if entities is not None:
                service_data["entities"] = entities
            if add_entities is not None:
                service_data["add_entities"] = add_entities
            if remove_entities is not None:
                service_data["remove_entities"] = remove_entities

            # Call group.set service
            await client.call_service("group", "set", service_data)

            entity_id = f"group.{object_id}"
            updated_fields = [k for k in service_data if k != "object_id"]

            # Determine if this was a create or update based on fields provided
            is_create = entities is not None and name is None and add_entities is None and remove_entities is None

            return {
                "success": True,
                "entity_id": entity_id,
                "object_id": object_id,
                "updated_fields": updated_fields,
                "message": f"Successfully {'created' if is_create else 'updated'} group: {entity_id}",
            }

        except Exception as e:
            logger.error(f"Error setting group: {e}")
            return {
                "success": False,
                "error": f"Failed to set group: {str(e)}",
                "object_id": object_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify all entity IDs in the entities list exist",
                    "Ensure object_id is valid (no dots, no 'group.' prefix)",
                    "Use ha_config_list_groups() to see existing groups",
                ],
            }

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "tags": ["group"], "title": "Remove Group"})
    @log_tool_usage
    async def ha_config_remove_group(
        object_id: Annotated[
            str,
            Field(
                description="Group identifier without 'group.' prefix (e.g., 'living_room_lights')"
            ),
        ],
    ) -> dict[str, Any]:
        """
        Remove a Home Assistant entity group.

        Uses the group.remove service to delete the group.

        EXAMPLES:
        - Remove group: ha_config_remove_group("living_room_lights")

        Use ha_config_list_groups() to find existing groups.

        **WARNING:**
        - Removing a group used in automations may cause those automations to fail.
        - Groups defined in YAML can be removed at runtime but will reappear after restart.
        - This only removes old-style groups, not platform-specific groups.
        """
        try:
            # Validate object_id
            if "." in object_id:
                return {
                    "success": False,
                    "error": f"Invalid object_id: '{object_id}'. Do not include 'group.' prefix.",
                    "object_id": object_id,
                }

            # Call group.remove service
            service_data = {"object_id": object_id}
            await client.call_service("group", "remove", service_data)

            entity_id = f"group.{object_id}"

            return {
                "success": True,
                "entity_id": entity_id,
                "object_id": object_id,
                "message": f"Successfully removed group: {entity_id}",
            }

        except Exception as e:
            logger.error(f"Error removing group: {e}")
            return {
                "success": False,
                "error": f"Failed to remove group: {str(e)}",
                "object_id": object_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify the group exists using ha_config_list_groups()",
                    "Groups defined in YAML cannot be permanently removed",
                ],
            }
