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
    HomeAssistantAuthError,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .auto_backup import with_auto_backup
from .coercion import JSON_STRING_COERCION
from .helpers import (
    exception_to_structured_error,
    get_entity_state_or_none,
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

_OBJECT_ID_DESCRIPTION = (
    "Group identifier: a lowercase slug without the 'group.' prefix "
    "(e.g., 'living_room_lights')"
)


class GroupTools:
    """Entity group management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _call_group_service(
        self, service: str, service_data: dict[str, Any]
    ) -> None:
        """Call group.<service> over WebSocket, where Core's schema rejection keeps
        its text; the REST endpoint answers it with a bare 400."""
        object_id = service_data["object_id"]
        try:
            result = await self._client.send_websocket_message(
                {
                    "type": "call_service",
                    "domain": "group",
                    "service": service,
                    "service_data": service_data,
                }
            )
        except HomeAssistantCommandNotSent:
            raise
        except HomeAssistantConnectionError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"group.{service} got no answer, so whether it was applied is "
                    f"unknown: {e}",
                    context={"object_id": object_id, "write_outcome_unknown": True},
                    suggestions=["Check with ha_config_list_groups() before retrying"],
                )
            )
        if result.get("success"):
            return
        error = str(result.get("error", "Unknown error")).removeprefix(
            "Command failed: "
        )
        error_code = result.get("error_code")
        code = ws_failure_code(result)
        suggestions: list[str] | None = None
        if error_code == "not_found":
            # Core's call_service answers not_found only for an unknown service.
            code = ErrorCode.SERVICE_NOT_FOUND
            suggestions = ["The group integration is not loaded in Home Assistant"]
        elif error_code == "invalid_format" and object_id.startswith("group."):
            # Core's slug hint ("try group_x") names a different group.
            suggestions = [
                f"Drop the 'group.' prefix: object_id={object_id.removeprefix('group.')!r}"
            ]
        raise_tool_error(
            create_error_response(
                code,
                f"group.{service} failed: {error}",
                context={"object_id": object_id},
                suggestions=suggestions,
            )
        )

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
        full set. Each group carries Core's state fields and attributes, its
        members as `entity_ids`, and its object_id. `auto` is true for a group
        created by ha_config_set_group (group.set) and false for a YAML group.
        Core does not expose a group's all/any mode.

        EXAMPLE: ha_config_list_groups(offset=100)
        """
        try:
            # Get all entity states and filter for groups
            states = await self._client.get_states()

            groups = []
            for state in states:
                entity_id = state.get("entity_id", "")
                if entity_id.startswith("group."):
                    attributes = dict(state.get("attributes") or {})
                    members = attributes.pop("entity_id", [])
                    groups.append(
                        {
                            "friendly_name": None,
                            "icon": None,
                            # Core sets ``auto`` only on group.set groups.
                            "auto": False,
                            **attributes,
                            **{k: v for k, v in state.items() if k != "attributes"},
                            "object_id": entity_id.removeprefix("group."),
                            "entity_ids": members,
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
        object_id: Annotated[str, Field(description=_OBJECT_ID_DESCRIPTION)],
        entities: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description="List of entity IDs for the group. When updating, replaces all entities;"
                " an empty list empties the group, or creates it without members.",
                default=None,
            ),
        ] = None,
        name: Annotated[
            str | None,
            Field(
                description="Friendly display name; a new group without one is named after object_id",
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
                description="If True, all entities must be on for group to be on. Core does not"
                " expose the mode, so it cannot be read back and a backup does not restore it.",
                default=None,
            ),
        ] = None,
        add_entities: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description="Entities to add to the group's members",
                default=None,
            ),
        ] = None,
        remove_entities: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description="Entities to remove from the group's members",
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
        `ha_remove_helpers_integrations` will not find them. They survive group.reload but
        not a restart.

        Core creates group.<object_id> when it does not exist (members from entities or
        add_entities) and updates it otherwise. entities, add_entities, and remove_entities
        are mutually exclusive.

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

            optional_fields = {
                "name": name,
                "icon": icon,
                "all": all_on,
                "entities": entities,
                "add_entities": add_entities,
                "remove_entities": remove_entities,
            }
            passed_fields = {k: v for k, v in optional_fields.items() if v is not None}
            service_data: dict[str, Any] = {"object_id": object_id, **passed_fields}

            entity_id = f"group.{object_id}"
            # Read first: Core picks create or update by whether the group
            # exists, so the reply can say which.
            existing = await get_entity_state_or_none(self._client, entity_id)
            await self._call_group_service("set", service_data)

            action_word = "created" if existing is None else "updated"
            updated_fields = list(passed_fields)

            result: dict[str, Any] = {}
            if existing is None and remove_entities is not None:
                result["warnings"] = [
                    f"{entity_id} did not exist, so Core created it without "
                    "members; remove_entities had nothing to remove."
                ]
            if existing is not None and not existing.get("attributes", {}).get("auto"):
                result["warnings"] = [
                    f"{entity_id} is defined in YAML: group.reload and a restart "
                    "rebuild it from YAML and drop this change."
                ]
            if wait:
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
                "message": f"Successfully {action_word} group: {entity_id}",
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
        object_id: Annotated[str, Field(description=_OBJECT_ID_DESCRIPTION)],
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
        - Groups defined in YAML can be removed at runtime but come back on group.reload
          or a restart.
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
            existing = await get_entity_state_or_none(self._client, entity_id)
            if existing is None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.ENTITY_NOT_FOUND,
                        f"Group '{entity_id}' not found",
                        context={"entity_id": entity_id},
                        suggestions=[
                            "Use ha_config_list_groups() to find the object_id "
                            "(without the 'group.' prefix)"
                        ],
                    )
                )
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
                suggestions=["Check Home Assistant connection"],
            )
            return None  # unreachable: exception_to_structured_error always raises


def register_group_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant entity group management tools."""
    register_tool_methods(mcp, GroupTools(client))
