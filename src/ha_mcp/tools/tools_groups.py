"""
Entity group management tools for Home Assistant.

This module provides tools for listing, creating/updating, and removing
Home Assistant entity groups (old-style groups created via group.set service).
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import create_entity_not_found_error, create_error_response
from .auto_backup import with_auto_backup
from .coercion import JSON_STRING_COERCION
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
    ws_failure_code,
)
from .response_helpers import build_pagination_metadata
from .tool_hints import read_only_hints, write_hints
from .ws_waiters import verify_entity_removed, wait_for_entity_registered

logger = logging.getLogger(__name__)


class GroupTools:
    """Entity group management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _read_group_state(self, entity_id: str) -> dict[str, Any] | None:
        """The group's current state, or None when Core has no such group."""
        try:
            state: dict[str, Any] = await self._client.get_entity_state(entity_id)
        except HomeAssistantAPIError as e:
            if e.status_code != 404:
                raise
            return None
        return state

    async def _call_group_service(
        self, service: str, service_data: dict[str, Any]
    ) -> None:
        """Call group.<service> over WebSocket, where Core's schema rejection
        keeps its text (the REST endpoint answers it with a bare 400)."""
        result = await self._client.send_websocket_message(
            {
                "type": "call_service",
                "domain": "group",
                "service": service,
                "service_data": service_data,
            }
        )
        if not result.get("success"):
            raise_tool_error(
                create_error_response(
                    ws_failure_code(result),
                    f"group.{service} failed: {result.get('error', 'Unknown error')}",
                    context={"object_id": service_data["object_id"]},
                )
            )

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
        annotations=read_only_hints("List Groups", open_world=False),
    )
    @log_tool_usage
    async def ha_config_list_groups(
        self,
        limit: Annotated[
            int,
            Field(
                default=100,
                ge=1,
                le=500,
                description="Max groups to return per page",
            ),
        ] = 100,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of groups to skip for pagination",
            ),
        ] = 0,
    ) -> dict[str, Any]:
        """List Home Assistant entity groups with their member entities.

        Returns one page of old-style groups created via the group.set service
        or YAML configuration (platform-specific groups such as light or cover
        groups are separate entities); `total_count` and `has_more` report the
        full set. Each group includes its entity_id, friendly name, state,
        member entities, icon, order and `auto`: true for a group created by
        group.set, false for a YAML group, which group.reload and a restart
        rebuild from YAML. Core does not expose a group's all/any mode.

        EXAMPLE: ha_config_list_groups(offset=100)
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
                            "order": attributes.get("order"),
                            "auto": attributes.get("auto", False),
                        }
                    )

            # Sort by friendly name, then entity_id: the unique tiebreaker keeps
            # groups with the same name in a stable order, so a page boundary
            # cannot duplicate or skip one when get_states() order shifts.
            groups.sort(
                key=lambda g: (
                    (g.get("friendly_name") or g.get("entity_id", "")).lower(),
                    g.get("entity_id", ""),
                )
            )

            total_count = len(groups)
            paginated_groups = groups[offset : offset + limit]

            return {
                "success": True,
                "groups": paginated_groups,
                **build_pagination_metadata(
                    total_count, offset, limit, len(paginated_groups)
                ),
                "message": f"Found {total_count} group(s)",
            }

        except Exception as e:  # noqa: BLE001
            logger.error(f"Error listing groups: {e}")
            exception_to_structured_error(
                e,
                context={"operation": "list_groups"},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify REST API is accessible",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    @tool(
        name="ha_config_set_group",
        tags={"Groups"},
        annotations=write_hints(
            "Create or Update Group",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )
    @with_auto_backup(domain="group", id_param="object_id")
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
            JSON_STRING_COERCION,
            Field(
                description="List of entity IDs for the group. When updating, replaces all entities.",
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
                description="If True, all entities must be on for group to be on",
                default=None,
            ),
        ] = None,
        add_entities: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description="Add these entities to an existing group",
                default=None,
            ),
        ] = None,
        remove_entities: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description="Remove these entities from an existing group",
                default=None,
            ),
        ] = None,
        wait: Annotated[
            bool,
            Field(
                description="Wait for group to be queryable before returning. Set to False for bulk"
                " operations.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Create or update a service-based Home Assistant entity group via the group.set service.

        **When NOT to use:** for typical "combine these entities into one controllable group"
        requests, prefer `ha_config_set_helper(helper_type="group", ...)`. Config-entry-backed
        groups are registered in the entity registry, so `ha_set_entity` can assign them to
        areas and they are deletable via `ha_remove_helpers_integrations`.

        **When to use:** compatibility with existing groups already configured via group.set
        or YAML, or the rare case where entity-registry membership is explicitly unwanted.
        Groups created here are only removable via `ha_config_remove_group` —
        `ha_remove_helpers_integrations` will not find them.

        Core creates the group when group.<object_id> does not exist yet (entities are
        optional and the name defaults to object_id) and updates it otherwise.
        entities, add_entities, and remove_entities are mutually exclusive.

        EXAMPLES:
        - Create: ha_config_set_group("bedroom_lights", entities=["light.lamp", "light.ceiling"], name="Bedroom Lights")
        - Add entities: ha_config_set_group("lights", add_entities=["light.extra"])
        """
        try:
            # Core's cv.slug accepts "", and group.set then creates a group under
            # a generated entity_id (group.unnamed_device without a name).
            validate_identifier_not_empty(
                object_id,
                "object_id",
                suggestions=[
                    "Use ha_config_list_groups() to find existing group object_ids",
                    "Or provide a fresh object_id (without 'group.' prefix) to create a new group",
                ],
                context={"operation": "set_group"},
            )

            service_data = self._build_group_service_data(
                object_id,
                name,
                icon,
                all_on,
                entities,
                add_entities,
                remove_entities,
            )

            entity_id = f"group.{object_id}"
            # Core creates or updates by whether the group exists.
            existing = await self._read_group_state(entity_id)
            await self._call_group_service("set", service_data)

            is_create = existing is None
            updated_fields = [k for k in service_data if k != "object_id"]

            result: dict[str, Any] = {}
            if existing is not None and not existing.get("attributes", {}).get("auto"):
                result["warnings"] = [
                    f"{entity_id} is defined in YAML: group.reload and a restart "
                    "rebuild it from YAML and drop this change."
                ]
            if wait:
                action_word = "created" if is_create else "updated"
                try:
                    registered = await wait_for_entity_registered(
                        self._client, entity_id
                    )
                    if not registered:
                        result.setdefault("warnings", []).append(
                            f"Group {action_word} but {entity_id} not yet queryable. "
                            "It may take a moment to become available."
                        )
                except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
                    result.setdefault("warnings", []).append(
                        f"Group {action_word} but verification failed: {e}"
                    )

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
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error setting group {object_id!r}: {e}")
            exception_to_structured_error(
                e,
                context={"object_id": object_id},
                suggestions=[
                    "Check Home Assistant connection",
                    "Use ha_config_list_groups() to see existing groups",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    @tool(
        name="ha_config_remove_group",
        tags={"Groups"},
        annotations=write_hints(
            "Remove Group", destructive=True, idempotent=True, open_world=False
        ),
    )
    @with_auto_backup(domain="group", id_param="object_id")
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
            bool,
            Field(
                description="Wait for group to be fully removed before returning.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Remove a service-based Home Assistant entity group via the group.remove service.

        **When NOT to use:** for groups created through `ha_config_set_helper(helper_type="group", ...)`,
        use `ha_remove_helpers_integrations`. Those config-entry-backed groups are not reachable via the
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
            validate_identifier_not_empty(
                object_id,
                "object_id",
                suggestions=[
                    "Use ha_config_list_groups() to find existing group object_ids"
                ],
                context={"operation": "remove_group"},
            )

            entity_id = f"group.{object_id}"
            # Core's group.remove only logs a warning for a missing group.
            existing = await self._read_group_state(entity_id)
            if existing is None:
                raise_tool_error(create_entity_not_found_error(entity_id))
            await self._call_group_service("remove", {"object_id": object_id})

            result: dict[str, Any] = {}
            if not existing.get("attributes", {}).get("auto"):
                result["warnings"] = [
                    f"{entity_id} is defined in YAML: group.reload and a restart "
                    "bring it back."
                ]
            if wait:
                await verify_entity_removed(self._client, entity_id, result)

            return {
                "success": True,
                "entity_id": entity_id,
                "object_id": object_id,
                "message": f"Successfully removed group: {entity_id}",
                **result,
            }

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error removing group {object_id!r}: {e}")
            exception_to_structured_error(
                e,
                context={"object_id": object_id},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify the group exists using ha_config_list_groups()",
                    "Groups defined in YAML cannot be permanently removed",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises


def register_group_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant entity group management tools."""
    register_tool_methods(mcp, GroupTools(client))
