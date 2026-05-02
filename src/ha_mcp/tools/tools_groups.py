"""
Entity group management tools for Home Assistant.

This module provides tools for listing, creating/updating, and removing
Home Assistant entity groups (old-style groups created via group.set service).
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
from .util_helpers import (
    coerce_bool_param,
    wait_for_entity_registered,
    wait_for_entity_removed,
)

logger = logging.getLogger(__name__)


class GroupTools:
    """Entity group management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def _validate_group_params(
        object_id: str,
        entities: list[str] | None,
        add_entities: list[str] | None,
        remove_entities: list[str] | None,
    ) -> None:
        """Validate group parameters: object_id format, mutual exclusivity, and non-empty lists."""
        # Validate object_id doesn't contain invalid characters
        if "." in object_id:
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid object_id: '{object_id}'. Do not include 'group.' prefix or dots.",
                context={"object_id": object_id},
                suggestions=["Provide object_id without 'group.' prefix or dots"],
            ))

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
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Only one of entities, add_entities, or remove_entities can be provided. Got: {op_names}",
                context={"object_id": object_id, "provided_ops": op_names},
                suggestions=["Use only one of: entities, add_entities, or remove_entities"],
            ))

        # Validate non-empty lists
        if entities is not None and not entities:
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Entities list cannot be empty",
                context={"object_id": object_id},
                suggestions=["Provide at least one entity ID in the entities list"],
            ))
        if add_entities is not None and not add_entities:
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "add_entities list cannot be empty",
                context={"object_id": object_id},
                suggestions=["Provide at least one entity ID in the add_entities list"],
            ))

    @staticmethod
    def _build_group_service_data(
        object_id: str,
        name: str | None,
        icon: str | None,
        all_on: bool | None,
        entities: list[str] | None,
        add_entities: list[str] | None,
        remove_entities: list[str] | None,
    ) -> dict[str, Any]:
        """Build service data dict for group.set service call."""
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
        return service_data

    @tool(
        name="ha_config_list_groups",
        tags={"Groups"},
        annotations={"idempotentHint": True, "readOnlyHint": True, "title": "List Groups"},
    )
    @log_tool_usage
    async def ha_config_list_groups(self) -> dict[str, Any]:
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
            states = await self._client.get_states()

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
            exception_to_structured_error(e, context={"operation": "list_groups"}, suggestions=[
                "Check Home Assistant connection",
                "Verify REST API is accessible",
            ])

    @tool(
        name="ha_config_set_group",
        tags={"Groups"},
        annotations={"destructiveHint": True, "title": "Create or Update Group"},
    )
    @log_tool_usage
    async def ha_config_set_group(
        self,
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
        wait: Annotated[
            bool | str,
            Field(
                description="Wait for group to be queryable before returning. Default: True. Set to False for bulk operations.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Create or update a service-based Home Assistant entity group via the group.set service.

        **When NOT to use:** for typical "combine these entities into one controllable group"
        requests, prefer `ha_config_set_helper(helper_type="group", ...)`. Config-entry-backed
        groups are registered in the entity registry, so `ha_set_entity` can assign them to
        areas and they are deletable via `ha_delete_helpers_integrations`.

        **When to use:** compatibility with existing groups already configured via group.set
        or YAML, or the rare case where entity-registry membership is explicitly unwanted.
        Groups created here are only removable via `ha_config_remove_group` —
        `ha_delete_helpers_integrations` will not find them.

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
            self._validate_group_params(object_id, entities, add_entities, remove_entities)

            service_data = self._build_group_service_data(
                object_id, name, icon, all_on, entities, add_entities, remove_entities,
            )

            # Call group.set service
            await self._client.call_service("group", "set", service_data)

            entity_id = f"group.{object_id}"
            updated_fields = [k for k in service_data if k != "object_id"]

            # Determine if this was a create or update based on fields provided
            is_create = entities is not None and name is None and add_entities is None and remove_entities is None

            # Verify entity is queryable after creation/update
            wait_bool = coerce_bool_param(wait, "wait", default=True)
            result: dict[str, Any] = {}
            if wait_bool:
                try:
                    registered = await wait_for_entity_registered(self._client, entity_id)
                    if not registered:
                        result["warning"] = f"Group created but {entity_id} not yet queryable. It may take a moment to become available."
                except Exception as e:
                    result["warning"] = f"Group created but verification failed: {e}"

            return {
                "success": True,
                "entity_id": entity_id,
                "object_id": object_id,
                "updated_fields": updated_fields,
                "message": f"Successfully {'created' if is_create else 'updated'} group: {entity_id}",
                **result,
            }

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error setting group {object_id!r}: {e}")
            exception_to_structured_error(e, context={"object_id": object_id}, suggestions=[
                "Check Home Assistant connection",
                "Verify all entity IDs in the entities list exist",
                "Ensure object_id is valid (no dots, no 'group.' prefix)",
                "Use ha_config_list_groups() to see existing groups",
            ])

    @tool(
        name="ha_config_remove_group",
        tags={"Groups"},
        annotations={"destructiveHint": True, "idempotentHint": True, "title": "Remove Group"},
    )
    @log_tool_usage
    async def ha_config_remove_group(
        self,
        object_id: Annotated[
            str,
            Field(
                description="Group identifier without 'group.' prefix (e.g., 'living_room_lights')"
            ),
        ],
        wait: Annotated[
            bool | str,
            Field(
                description="Wait for group to be fully removed before returning. Default: True.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Remove a service-based Home Assistant entity group via the group.remove service.

        **When NOT to use:** for groups created through `ha_config_set_helper(helper_type="group", ...)`,
        use `ha_delete_helpers_integrations`. Those config-entry-backed groups are not reachable via the
        group.remove service.

        **When to use:** removing groups created with `ha_config_set_group` or defined in YAML
        via `group:` configuration. Config-entry-backed deletion tools cannot find these.

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
                raise_tool_error(create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid object_id: '{object_id}'. Do not include 'group.' prefix.",
                    context={"object_id": object_id},
                    suggestions=["Provide object_id without 'group.' prefix or dots"],
                ))

            # Call group.remove service
            service_data = {"object_id": object_id}
            await self._client.call_service("group", "remove", service_data)

            entity_id = f"group.{object_id}"

            # Verify entity is removed
            wait_bool = coerce_bool_param(wait, "wait", default=True)
            result: dict[str, Any] = {}
            if wait_bool:
                try:
                    removed = await wait_for_entity_removed(self._client, entity_id)
                    if not removed:
                        result["warning"] = f"Deletion confirmed by API but {entity_id} may still appear briefly."
                except Exception as e:
                    result["warning"] = f"Deletion confirmed but removal verification failed: {e}"

            return {
                "success": True,
                "entity_id": entity_id,
                "object_id": object_id,
                "message": f"Successfully removed group: {entity_id}",
                **result,
            }

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error removing group {object_id!r}: {e}")
            exception_to_structured_error(e, context={"object_id": object_id}, suggestions=[
                "Check Home Assistant connection",
                "Verify the group exists using ha_config_list_groups()",
                "Groups defined in YAML cannot be permanently removed",
            ])


def register_group_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant entity group management tools."""
    register_tool_methods(mcp, GroupTools(client))
