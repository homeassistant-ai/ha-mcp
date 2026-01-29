"""
Entity management tools for Home Assistant MCP server.

This module provides tools for managing entity lifecycle and properties
via the Home Assistant entity registry API.
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, log_tool_usage
from .tools_voice_assistant import KNOWN_ASSISTANTS
from .util_helpers import coerce_bool_param, parse_string_list_param

logger = logging.getLogger(__name__)


def register_entity_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register entity management tools with the MCP server."""

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "tags": ["entity"],
            "title": "Set Entity",
        }
    )
    @log_tool_usage
    async def ha_set_entity(
        entity_id: Annotated[
            str, Field(description="Entity ID to update (e.g., 'sensor.temperature')")
        ],
        area_id: Annotated[
            str | None,
            Field(
                description="Area/room ID to assign the entity to. Use empty string '' to unassign from current area.",
                default=None,
            ),
        ] = None,
        name: Annotated[
            str | None,
            Field(
                description="Display name for the entity. Use empty string '' to remove custom name and revert to default.",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Icon for the entity (e.g., 'mdi:thermometer'). Use empty string '' to remove custom icon.",
                default=None,
            ),
        ] = None,
        enabled: Annotated[
            bool | str | None,
            Field(
                description="True to enable the entity, False to disable it.",
                default=None,
            ),
        ] = None,
        hidden: Annotated[
            bool | str | None,
            Field(
                description="True to hide the entity from UI, False to show it.",
                default=None,
            ),
        ] = None,
        aliases: Annotated[
            str | list[str] | None,
            Field(
                description="List of voice assistant aliases for the entity (replaces existing aliases).",
                default=None,
            ),
        ] = None,
        labels: Annotated[
            str | list[str] | None,
            Field(
                description="List of label IDs to assign to the entity (replaces all existing labels). Use [] to clear all labels.",
                default=None,
            ),
        ] = None,
        expose_to: Annotated[
            str | dict[str, bool] | None,
            Field(
                description=(
                    "Control voice assistant exposure. Pass a dict mapping assistant IDs to booleans. "
                    "Valid assistants: 'conversation' (Assist), 'cloud.alexa', 'cloud.google_assistant'. "
                    "Example: {\"conversation\": true, \"cloud.alexa\": false}"
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Update entity properties in the entity registry.

        Allows modifying entity metadata such as area assignment, display name,
        icon, enabled/disabled state, visibility, aliases, labels, and voice
        assistant exposure in a single call.

        Use ha_search_entities() or ha_get_device() to find entity IDs.
        Use ha_config_get_label() to find available label IDs.

        PARAMETERS:
        - area_id: Assigns entity to an area/room. Use '' to remove from area.
        - name: Custom display name. Use '' to revert to default name.
        - icon: Custom icon (e.g., 'mdi:lightbulb'). Use '' to revert to default.
        - enabled: True to enable, False to disable.
        - hidden: True to hide from UI, False to show.
        - aliases: Voice assistant aliases (e.g., ["living room light", "main light"]).
        - labels: Label IDs to set (replaces all existing). Use [] to clear.
        - expose_to: Dict of assistant -> bool for voice assistant exposure.

        EXAMPLES:
        - Assign to area: ha_set_entity("sensor.temp", area_id="living_room")
        - Rename: ha_set_entity("sensor.temp", name="Living Room Temperature")
        - Change icon: ha_set_entity("sensor.temp", icon="mdi:thermometer")
        - Disable: ha_set_entity("sensor.temp", enabled=False)
        - Enable: ha_set_entity("sensor.temp", enabled=True)
        - Hide: ha_set_entity("sensor.temp", hidden=True)
        - Show: ha_set_entity("sensor.temp", hidden=False)
        - Set aliases: ha_set_entity("light.lamp", aliases=["bedroom light", "lamp"])
        - Set labels: ha_set_entity("light.lamp", labels=["outdoor", "smart"])
        - Clear labels: ha_set_entity("light.lamp", labels=[])
        - Expose to Alexa: ha_set_entity("light.lamp", expose_to={"cloud.alexa": True})
        - Hide from all: ha_set_entity("light.lamp", expose_to={"conversation": False, "cloud.alexa": False, "cloud.google_assistant": False})
        - Combined: ha_set_entity("light.lamp", name="Lamp", labels=["outdoor"], expose_to={"conversation": True})
        - Clear area: ha_set_entity("sensor.temp", area_id="")

        NOTE: To rename an entity_id (e.g., sensor.old -> sensor.new), use ha_rename_entity() instead.
        """
        try:
            # Parse list parameters if provided as strings
            parsed_aliases = None
            if aliases is not None:
                try:
                    parsed_aliases = parse_string_list_param(aliases, "aliases")
                except ValueError as e:
                    return create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid aliases parameter: {e}",
                    )

            parsed_labels = None
            if labels is not None:
                try:
                    parsed_labels = parse_string_list_param(labels, "labels")
                except ValueError as e:
                    return create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid labels parameter: {e}",
                    )

            # Parse and validate expose_to parameter
            parsed_expose_to: dict[str, bool] | None = None
            if expose_to is not None:
                if isinstance(expose_to, str):
                    import json as _json

                    try:
                        parsed_expose_to = _json.loads(expose_to)
                    except _json.JSONDecodeError:
                        return create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "expose_to must be a JSON dict mapping assistant IDs to booleans, "
                            'e.g. {"conversation": true, "cloud.alexa": false}',
                        )
                elif isinstance(expose_to, dict):
                    parsed_expose_to = expose_to
                else:
                    return create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"expose_to must be a dict, got {type(expose_to).__name__}",
                    )

                if not isinstance(parsed_expose_to, dict):
                    return create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "expose_to must be a dict mapping assistant IDs to booleans",
                    )

                # Validate assistant names
                invalid_assistants = [
                    a for a in parsed_expose_to if a not in KNOWN_ASSISTANTS
                ]
                if invalid_assistants:
                    return create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid assistant(s) in expose_to: {invalid_assistants}. "
                        f"Valid: {KNOWN_ASSISTANTS}",
                    )

                # Coerce values to bool
                for asst, val in parsed_expose_to.items():
                    coerced = coerce_bool_param(val, f"expose_to[{asst}]")
                    if coerced is None:
                        return create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"expose_to[{asst}] must be a boolean value",
                        )
                    parsed_expose_to[asst] = coerced

            # Build update message for entity registry
            message: dict[str, Any] = {
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
            }

            updates_made = []

            if area_id is not None:
                # Empty string means remove from area (set to None in API)
                message["area_id"] = area_id if area_id else None
                updates_made.append(
                    f"area_id='{area_id}'" if area_id else "area cleared"
                )

            if name is not None:
                # Empty string means remove custom name (set to None in API)
                message["name"] = name if name else None
                updates_made.append(f"name='{name}'" if name else "name cleared")

            if icon is not None:
                # Empty string means remove custom icon (set to None in API)
                message["icon"] = icon if icon else None
                updates_made.append(f"icon='{icon}'" if icon else "icon cleared")

            if enabled is not None:
                # Convert boolean to API format: True=enable (None), False=disable ("user")
                enabled_bool = coerce_bool_param(enabled, "enabled")
                message["disabled_by"] = None if enabled_bool else "user"
                updates_made.append("enabled" if enabled_bool else "disabled")

            if hidden is not None:
                # Convert boolean to API format: True=hide ("user"), False=show (None)
                hidden_bool = coerce_bool_param(hidden, "hidden")
                message["hidden_by"] = "user" if hidden_bool else None
                updates_made.append("hidden" if hidden_bool else "visible")

            if parsed_aliases is not None:
                message["aliases"] = parsed_aliases
                updates_made.append(f"aliases={parsed_aliases}")

            if parsed_labels is not None:
                message["labels"] = parsed_labels
                updates_made.append(f"labels={parsed_labels}")

            if parsed_expose_to is not None:
                updates_made.append(f"expose_to={parsed_expose_to}")

            if not updates_made:
                return {
                    "success": False,
                    "error": "No updates specified",
                    "suggestion": "Provide at least one of: area_id, name, icon, enabled, hidden, aliases, labels, or expose_to",
                }

            # Send entity registry update (covers all fields except expose_to)
            has_registry_updates = len(message) > 2  # more than just type + entity_id
            entity_entry = {}

            if has_registry_updates:
                logger.info(f"Updating entity {entity_id}: {', '.join(updates_made)}")
                result = await client.send_websocket_message(message)

                if not result.get("success"):
                    error = result.get("error", {})
                    error_msg = (
                        error.get("message", str(error))
                        if isinstance(error, dict)
                        else str(error)
                    )
                    return {
                        "success": False,
                        "error": f"Failed to update entity: {error_msg}",
                        "entity_id": entity_id,
                        "suggestions": [
                            "Verify the entity_id exists using ha_search_entities()",
                            "Check that area_id exists if specified",
                            "Some entities may not support all update options",
                        ],
                    }

                entity_entry = result.get("result", {}).get("entity_entry", {})

            # Handle expose_to via separate WebSocket API
            exposure_result = None
            if parsed_expose_to is not None:
                # Group by should_expose value for efficient API calls
                expose_true = [a for a, v in parsed_expose_to.items() if v]
                expose_false = [a for a, v in parsed_expose_to.items() if not v]

                for assistants, should_expose in [
                    (expose_true, True),
                    (expose_false, False),
                ]:
                    if not assistants:
                        continue

                    expose_msg: dict[str, Any] = {
                        "type": "homeassistant/expose_entity",
                        "assistants": assistants,
                        "entity_ids": [entity_id],
                        "should_expose": should_expose,
                    }

                    logger.info(
                        f"{'Exposing' if should_expose else 'Hiding'} {entity_id} "
                        f"{'to' if should_expose else 'from'} {assistants}"
                    )
                    expose_result = await client.send_websocket_message(expose_msg)

                    if not expose_result.get("success"):
                        error = expose_result.get("error", {})
                        error_msg = (
                            error.get("message", str(error))
                            if isinstance(error, dict)
                            else str(error)
                        )
                        # Return partial success if registry update succeeded
                        response: dict[str, Any] = {
                            "success": False,
                            "error": f"Entity registry updated but exposure failed: {error_msg}",
                            "entity_id": entity_id,
                        }
                        if has_registry_updates:
                            response["partial"] = True
                            response["entity_entry"] = {
                                "entity_id": entity_entry.get("entity_id"),
                                "name": entity_entry.get("name"),
                                "original_name": entity_entry.get("original_name"),
                                "icon": entity_entry.get("icon"),
                                "area_id": entity_entry.get("area_id"),
                                "disabled_by": entity_entry.get("disabled_by"),
                                "hidden_by": entity_entry.get("hidden_by"),
                                "aliases": entity_entry.get("aliases", []),
                                "labels": entity_entry.get("labels", []),
                            }
                        return response

                exposure_result = parsed_expose_to

            # If only expose_to was set (no registry updates), fetch current entity state
            if not has_registry_updates and parsed_expose_to is not None:
                get_msg: dict[str, Any] = {
                    "type": "config/entity_registry/get",
                    "entity_id": entity_id,
                }
                get_result = await client.send_websocket_message(get_msg)
                if get_result.get("success"):
                    entity_entry = get_result.get("result", {})

            response_data: dict[str, Any] = {
                "success": True,
                "entity_id": entity_id,
                "updates": updates_made,
                "entity_entry": {
                    "entity_id": entity_entry.get("entity_id"),
                    "name": entity_entry.get("name"),
                    "original_name": entity_entry.get("original_name"),
                    "icon": entity_entry.get("icon"),
                    "area_id": entity_entry.get("area_id"),
                    "disabled_by": entity_entry.get("disabled_by"),
                    "hidden_by": entity_entry.get("hidden_by"),
                    "aliases": entity_entry.get("aliases", []),
                    "labels": entity_entry.get("labels", []),
                },
                "message": f"Entity updated: {', '.join(updates_made)}",
            }

            if exposure_result is not None:
                response_data["exposure"] = exposure_result

            return response_data

        except Exception as e:
            logger.error(f"Error updating entity: {e}")
            return exception_to_structured_error(e, context={"entity_id": entity_id})

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "idempotentHint": True,
            "tags": ["entity"],
            "title": "Get Entity",
        }
    )
    @log_tool_usage
    async def ha_get_entity(
        entity_id: Annotated[
            str | list[str],
            Field(
                description="Entity ID or list of entity IDs to retrieve (e.g., 'sensor.temperature' or ['light.living_room', 'switch.porch'])"
            ),
        ],
    ) -> dict[str, Any]:
        """Get entity registry information for one or more entities.

        Returns detailed entity registry metadata including area assignment,
        custom name/icon, enabled/hidden state, aliases, labels, and more.

        RELATED TOOLS:
        - ha_set_entity(): Modify entity properties (area, name, icon, enabled, hidden, aliases)
        - ha_get_state(): Get current state/attributes (on/off, temperature, etc.)
        - ha_search_entities(): Find entities by name, domain, or area

        EXAMPLES:
        - Single entity: ha_get_entity("sensor.temperature")
        - Multiple entities: ha_get_entity(["light.living_room", "switch.porch"])

        RESPONSE FIELDS:
        - entity_id: Full entity identifier
        - name: Custom display name (null if using original_name)
        - original_name: Default name from integration
        - icon: Custom icon (null if using default)
        - area_id: Assigned area/room ID (null if unassigned)
        - disabled_by: Why disabled (null=enabled, "user"/"integration"/etc)
        - hidden_by: Why hidden (null=visible, "user"/"integration"/etc)
        - enabled: Boolean shorthand (True if disabled_by is null)
        - hidden: Boolean shorthand (True if hidden_by is not null)
        - aliases: Voice assistant aliases
        - labels: Assigned label IDs
        - platform: Integration platform (e.g., "hue", "zwave_js")
        - device_id: Associated device ID (null if standalone)
        - unique_id: Integration's unique identifier
        """
        try:
            # Validate and parse entity_id parameter
            entity_ids: list[str]
            is_bulk: bool

            if isinstance(entity_id, str):
                entity_ids = [entity_id]
                is_bulk = False
            elif isinstance(entity_id, list):
                if not entity_id:
                    return {
                        "success": True,
                        "entity_entries": [],
                        "count": 0,
                        "message": "No entities requested",
                    }
                if not all(isinstance(e, str) for e in entity_id):
                    return {
                        "success": False,
                        "error": "All entity_id values must be strings",
                    }
                entity_ids = entity_id
                is_bulk = True
            else:
                return {
                    "success": False,
                    "error": f"entity_id must be string or list of strings, got {type(entity_id).__name__}",
                }

            async def _fetch_entity(eid: str) -> dict[str, Any]:
                """Fetch a single entity from the registry."""
                message: dict[str, Any] = {
                    "type": "config/entity_registry/get",
                    "entity_id": eid,
                }
                result = await client.send_websocket_message(message)

                if not result.get("success"):
                    error = result.get("error", {})
                    error_msg = (
                        error.get("message", str(error))
                        if isinstance(error, dict)
                        else str(error)
                    )
                    return {
                        "success": False,
                        "entity_id": eid,
                        "error": error_msg,
                    }

                entry = result.get("result", {})
                return {
                    "success": True,
                    "entity_id": entry.get("entity_id"),
                    "name": entry.get("name"),
                    "original_name": entry.get("original_name"),
                    "icon": entry.get("icon"),
                    "area_id": entry.get("area_id"),
                    "disabled_by": entry.get("disabled_by"),
                    "hidden_by": entry.get("hidden_by"),
                    "enabled": entry.get("disabled_by") is None,
                    "hidden": entry.get("hidden_by") is not None,
                    "aliases": entry.get("aliases", []),
                    "labels": entry.get("labels", []),
                    "platform": entry.get("platform"),
                    "device_id": entry.get("device_id"),
                    "unique_id": entry.get("unique_id"),
                }

            # Single entity case
            if not is_bulk:
                eid = entity_ids[0]
                logger.info(f"Getting entity registry entry for {eid}")
                result = await _fetch_entity(eid)

                if result.get("success"):
                    return {
                        "success": True,
                        "entity_id": eid,
                        "entity_entry": {
                            k: v for k, v in result.items() if k not in ("success",)
                        },
                    }
                else:
                    return {
                        "success": False,
                        "entity_id": eid,
                        "error": f"Entity not found: {result.get('error', 'Unknown error')}",
                        "suggestions": [
                            "Use ha_search_entities() to find valid entity IDs",
                            "Check the entity_id spelling and format (e.g., 'sensor.temperature')",
                        ],
                    }

            # Bulk case - fetch all entities
            import asyncio

            logger.info(f"Getting entity registry entries for {len(entity_ids)} entities")
            results = await asyncio.gather(
                *[_fetch_entity(eid) for eid in entity_ids],
                return_exceptions=True,
            )

            entity_entries: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []

            for eid, fetch_result in zip(entity_ids, results, strict=True):
                if isinstance(fetch_result, BaseException):
                    errors.append({
                        "entity_id": eid,
                        "error": str(fetch_result),
                    })
                    continue
                if fetch_result.get("success"):
                    entity_entries.append(
                        {k: v for k, v in fetch_result.items() if k not in ("success",)}
                    )
                else:
                    errors.append({
                        "entity_id": eid,
                        "error": fetch_result.get("error", "Unknown error"),
                    })

            response: dict[str, Any] = {
                "success": True,
                "count": len(entity_entries),
                "entity_entries": entity_entries,
            }

            if errors:
                response["errors"] = errors
                response["suggestions"] = [
                    "Use ha_search_entities() to find valid entity IDs for failed lookups"
                ]

            return response

        except Exception as e:
            logger.error(f"Error getting entity: {e}")
            return exception_to_structured_error(
                e, context={"entity_id": entity_id if isinstance(entity_id, str) else entity_ids}
            )
