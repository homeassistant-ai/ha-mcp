"""
Entity group management tools for Home Assistant.

This module provides tools for listing, creating, updating, and deleting
Home Assistant entity groups (old-style groups created via group.set service).
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)


def register_group_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant entity group management tools."""

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_list_groups() -> dict[str, Any]:
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
        - List all groups: ha_list_groups()

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

    @mcp.tool
    @log_tool_usage
    async def ha_create_group(
        object_id: Annotated[
            str,
            Field(
                description="Group identifier without 'group.' prefix (e.g., 'living_room_lights')"
            ),
        ],
        entities: Annotated[
            list[str],
            Field(
                description="List of entity IDs to include in the group (e.g., ['light.lamp1', 'light.lamp2'])"
            ),
        ],
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
            bool,
            Field(
                description="If True, all entities must be on for group to be on (default: False)",
                default=False,
            ),
        ] = False,
    ) -> dict[str, Any]:
        """
        Create a new Home Assistant entity group.

        Creates an old-style group using the group.set service. Groups are useful
        for organizing entities and controlling them together.

        EXAMPLES:
        - Create light group: ha_create_group("bedroom_lights", ["light.lamp", "light.ceiling"])
        - Create named group: ha_create_group("all_sensors", ["sensor.temp", "sensor.humidity"], name="All Sensors")
        - Create with icon: ha_create_group("security", ["lock.front", "lock.back"], icon="mdi:shield")
        - Create with all mode: ha_create_group("all_lights", ["light.a", "light.b"], all_on=True)

        **NOTE:** This creates old-style groups. For platform-specific groups
        (light groups with combined brightness, cover groups), use the respective
        domain's integration configuration.
        """
        try:
            # Validate object_id doesn't contain invalid characters
            if "." in object_id:
                return {
                    "success": False,
                    "error": f"Invalid object_id: '{object_id}'. Do not include 'group.' prefix or dots.",
                    "object_id": object_id,
                }

            if not entities:
                return {
                    "success": False,
                    "error": "Entities list cannot be empty",
                    "object_id": object_id,
                }

            # Build service data
            service_data: dict[str, Any] = {
                "object_id": object_id,
                "entities": entities,
            }

            if name:
                service_data["name"] = name
            if icon:
                service_data["icon"] = icon
            if all_on:
                service_data["all"] = all_on

            # Call group.set service
            await client.call_service("group", "set", service_data)

            entity_id = f"group.{object_id}"

            return {
                "success": True,
                "entity_id": entity_id,
                "object_id": object_id,
                "name": name or object_id,
                "entities": entities,
                "message": f"Successfully created group: {entity_id}",
            }

        except Exception as e:
            logger.error(f"Error creating group: {e}")
            return {
                "success": False,
                "error": f"Failed to create group: {str(e)}",
                "object_id": object_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify all entity IDs in the entities list exist",
                    "Ensure object_id is valid (no dots, no 'group.' prefix)",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_update_group(
        object_id: Annotated[
            str,
            Field(
                description="Group identifier without 'group.' prefix (e.g., 'living_room_lights')"
            ),
        ],
        name: Annotated[
            str | None,
            Field(description="New friendly display name", default=None),
        ] = None,
        icon: Annotated[
            str | None,
            Field(description="New Material Design Icon", default=None),
        ] = None,
        all_on: Annotated[
            bool | None,
            Field(description="New 'all entities must be on' setting", default=None),
        ] = None,
        entities: Annotated[
            list[str] | None,
            Field(
                description="Replace all entities with this list (mutually exclusive with add_entities/remove_entities)",
                default=None,
            ),
        ] = None,
        add_entities: Annotated[
            list[str] | None,
            Field(
                description="Add these entities to the group (mutually exclusive with entities)",
                default=None,
            ),
        ] = None,
        remove_entities: Annotated[
            list[str] | None,
            Field(
                description="Remove these entities from the group (mutually exclusive with entities)",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Update an existing Home Assistant entity group.

        Updates group properties using the group.set service. You can:
        - Change the name, icon, or all_on setting
        - Replace all entities with a new list
        - Add entities to the group
        - Remove entities from the group

        **IMPORTANT:** entities, add_entities, and remove_entities are mutually exclusive.
        Only use one of these parameters per call.

        EXAMPLES:
        - Change name: ha_update_group("lights", name="Living Room Lights")
        - Change icon: ha_update_group("lights", icon="mdi:lamp")
        - Replace entities: ha_update_group("lights", entities=["light.new1", "light.new2"])
        - Add entities: ha_update_group("lights", add_entities=["light.extra"])
        - Remove entities: ha_update_group("lights", remove_entities=["light.old"])
        - Multiple changes: ha_update_group("lights", name="New Name", add_entities=["light.new"])

        Use ha_list_groups() to find existing groups.
        """
        try:
            # Validate object_id
            if "." in object_id:
                return {
                    "success": False,
                    "error": f"Invalid object_id: '{object_id}'. Do not include 'group.' prefix.",
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

            # Check that at least one field is being updated
            has_update = any(
                [
                    name is not None,
                    icon is not None,
                    all_on is not None,
                    entities is not None,
                    add_entities is not None,
                    remove_entities is not None,
                ]
            )

            if not has_update:
                return {
                    "success": False,
                    "error": "No fields to update. Provide at least one field to change.",
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
                if not entities:
                    return {
                        "success": False,
                        "error": "Entities list cannot be empty",
                        "object_id": object_id,
                    }
                service_data["entities"] = entities
            if add_entities is not None:
                if not add_entities:
                    return {
                        "success": False,
                        "error": "add_entities list cannot be empty",
                        "object_id": object_id,
                    }
                service_data["add_entities"] = add_entities
            if remove_entities is not None:
                service_data["remove_entities"] = remove_entities

            # Call group.set service
            await client.call_service("group", "set", service_data)

            entity_id = f"group.{object_id}"
            updated_fields = [k for k in service_data.keys() if k != "object_id"]

            return {
                "success": True,
                "entity_id": entity_id,
                "object_id": object_id,
                "updated_fields": updated_fields,
                "message": f"Successfully updated group: {entity_id}",
            }

        except Exception as e:
            logger.error(f"Error updating group: {e}")
            return {
                "success": False,
                "error": f"Failed to update group: {str(e)}",
                "object_id": object_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify the group exists using ha_list_groups()",
                    "For entity operations, verify entity IDs exist",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_delete_group(
        object_id: Annotated[
            str,
            Field(
                description="Group identifier without 'group.' prefix (e.g., 'living_room_lights')"
            ),
        ],
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant entity group.

        Removes the group using the group.remove service.

        EXAMPLES:
        - Delete group: ha_delete_group("living_room_lights")

        Use ha_list_groups() to find existing groups.

        **WARNING:**
        - Deleting a group that is used in automations may cause those automations to fail.
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
                "message": f"Successfully deleted group: {entity_id}",
            }

        except Exception as e:
            logger.error(f"Error deleting group: {e}")
            return {
                "success": False,
                "error": f"Failed to delete group: {str(e)}",
                "object_id": object_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify the group exists using ha_list_groups()",
                    "Groups defined in YAML cannot be permanently deleted",
                ],
            }
