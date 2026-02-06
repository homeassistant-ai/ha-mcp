"""
Entity management tools for Home Assistant MCP server.

This module provides tools for managing entity lifecycle and properties
via the Home Assistant entity registry API.
"""

import asyncio
import logging
from typing import Annotated, Any, Literal

from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, log_tool_usage
from .tools_voice_assistant import KNOWN_ASSISTANTS
from .util_helpers import coerce_bool_param, parse_json_param, parse_string_list_param

logger = logging.getLogger(__name__)


def _format_entity_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Format entity registry entry for API response."""
    return {
        "entity_id": entry.get("entity_id"),
        "name": entry.get("name"),
        "original_name": entry.get("original_name"),
        "icon": entry.get("icon"),
        "area_id": entry.get("area_id"),
        "disabled_by": entry.get("disabled_by"),
        "hidden_by": entry.get("hidden_by"),
        "aliases": entry.get("aliases", []),
        "labels": entry.get("labels", []),
    }


def register_entity_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register entity management tools with the MCP server."""

    async def _get_entity_labels(entity_id: str) -> tuple[list[str] | None, str | None]:
        """Fetch current labels for an entity. Returns (labels, error_msg)."""
        get_msg: dict[str, Any] = {
            "type": "config/entity_registry/get",
            "entity_id": entity_id,
        }
        result = await client.send_websocket_message(get_msg)
        if not result.get("success"):
            error = result.get("error", {})
            error_msg = (
                error.get("message", str(error))
                if isinstance(error, dict)
                else str(error)
            )
            return None, error_msg
        return result.get("result", {}).get("labels", []), None

    async def _update_single_entity(
        entity_id: str,
        area_id: str | None,
        name: str | None,
        icon: str | None,
        enabled: bool | str | None,
        hidden: bool | str | None,
        parsed_aliases: list[str] | None,
        parsed_labels: list[str] | None,
        label_operation: str,
        parsed_expose_to: dict[str, bool] | None,
    ) -> dict[str, Any]:
        """Update a single entity. Returns the response dict."""
        # For add/remove operations, we need to fetch current labels first
        final_labels = parsed_labels
        if parsed_labels is not None and label_operation in ("add", "remove"):
            current_labels, error_msg = await _get_entity_labels(entity_id)
            if current_labels is None:
                return {
                    "success": False,
                    "error": f"Failed to get current labels for {entity_id}: {error_msg}",
                    "entity_id": entity_id,
                }

            if label_operation == "add":
                # Add new labels without duplicates
                final_labels = list(set(current_labels) | set(parsed_labels))
            else:  # remove
                # Remove specified labels - use set for O(1) membership check
                labels_to_remove = set(parsed_labels)
                final_labels = [lbl for lbl in current_labels if lbl not in labels_to_remove]

        # Build update message for entity registry
        message: dict[str, Any] = {
            "type": "config/entity_registry/update",
            "entity_id": entity_id,
        }

        updates_made = []

        if area_id is not None:
            message["area_id"] = area_id if area_id else None
            updates_made.append(f"area_id='{area_id}'" if area_id else "area cleared")

        if name is not None:
            message["name"] = name if name else None
            updates_made.append(f"name='{name}'" if name else "name cleared")

        if icon is not None:
            message["icon"] = icon if icon else None
            updates_made.append(f"icon='{icon}'" if icon else "icon cleared")

        if enabled is not None:
            try:
                enabled_bool = coerce_bool_param(enabled, "enabled")
            except ValueError as e:
                return create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    str(e),
                )
            message["disabled_by"] = None if enabled_bool else "user"
            updates_made.append("enabled" if enabled_bool else "disabled")

        if hidden is not None:
            try:
                hidden_bool = coerce_bool_param(hidden, "hidden")
            except ValueError as e:
                return create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    str(e),
                )
            message["hidden_by"] = "user" if hidden_bool else None
            updates_made.append("hidden" if hidden_bool else "visible")

        if parsed_aliases is not None:
            message["aliases"] = parsed_aliases
            updates_made.append(f"aliases={parsed_aliases}")

        if final_labels is not None:
            message["labels"] = final_labels
            if label_operation == "set":
                updates_made.append(f"labels={final_labels}")
            elif label_operation == "add":
                updates_made.append(f"labels added: {parsed_labels} -> {final_labels}")
            else:  # remove
                updates_made.append(f"labels removed: {parsed_labels} -> {final_labels}")

        if parsed_expose_to is not None:
            updates_made.append(f"expose_to={parsed_expose_to}")

        if not updates_made:
            return {
                "success": False,
                "error": "No updates specified",
                "suggestions": [
                    "Provide at least one of: area_id, name, icon, enabled, hidden, aliases, labels, or expose_to"
                ],
            }

        # Send entity registry update (covers all fields except expose_to)
        has_registry_updates = len(message) > 2  # more than just type + entity_id
        entity_entry: dict[str, Any] = {}

        if has_registry_updates:
            registry_update_fields = [u for u in updates_made if not u.startswith("expose_to=")]
            logger.info(f"Updating entity registry for {entity_id}: {', '.join(registry_update_fields)}")
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
        exposure_result: dict[str, bool] | None = None
        if parsed_expose_to is not None:
            # Group by should_expose value for efficient API calls
            expose_true = [a for a, v in parsed_expose_to.items() if v]
            expose_false = [a for a, v in parsed_expose_to.items() if not v]

            succeeded: dict[str, bool] = {}

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
                    failed = dict.fromkeys(assistants, should_expose)
                    response: dict[str, Any] = {
                        "success": False,
                        "error": f"Exposure failed: {error_msg}",
                        "entity_id": entity_id,
                        "exposure_succeeded": succeeded,
                        "exposure_failed": failed,
                    }
                    if has_registry_updates:
                        response["partial"] = True
                        response["entity_entry"] = _format_entity_entry(entity_entry)
                    return response

                # Track successful exposures
                for a in assistants:
                    succeeded[a] = should_expose

            exposure_result = succeeded

        # If only expose_to was set (no registry updates), fetch current entity state
        if not has_registry_updates and parsed_expose_to is not None:
            get_msg: dict[str, Any] = {
                "type": "config/entity_registry/get",
                "entity_id": entity_id,
            }
            get_result = await client.send_websocket_message(get_msg)
            if get_result.get("success"):
                entity_entry = get_result.get("result", {})
            else:
                return {
                    "success": False,
                    "error": f"Entity '{entity_id}' not found in registry after applying exposure changes",
                    "entity_id": entity_id,
                    "suggestions": [
                        "Verify the entity_id exists using ha_search_entities()",
                        "The entity's exposure settings were likely changed, but its current state could not be confirmed.",
                    ],
                    "exposure_succeeded": exposure_result,
                }

        response_data: dict[str, Any] = {
            "success": True,
            "entity_id": entity_id,
            "updates": updates_made,
            "entity_entry": _format_entity_entry(entity_entry),
            "message": f"Entity updated: {', '.join(updates_made)}",
        }

        if exposure_result is not None:
            response_data["exposure"] = exposure_result

        return response_data

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
            str | list[str],
            Field(
                description="Entity ID or list of entity IDs to update. Bulk operations (list) only support labels and expose_to parameters."
            ),
        ],
        area_id: Annotated[
            str | None,
            Field(
                description="Area/room ID to assign the entity to. Use empty string '' to unassign from current area. Single entity only.",
                default=None,
            ),
        ] = None,
        name: Annotated[
            str | None,
            Field(
                description="Display name for the entity. Use empty string '' to remove custom name and revert to default. Single entity only.",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Icon for the entity (e.g., 'mdi:thermometer'). Use empty string '' to remove custom icon. Single entity only.",
                default=None,
            ),
        ] = None,
        enabled: Annotated[
            bool | str | None,
            Field(
                description="True to enable the entity, False to disable it. Single entity only.",
                default=None,
            ),
        ] = None,
        hidden: Annotated[
            bool | str | None,
            Field(
                description="True to hide the entity from UI, False to show it. Single entity only.",
                default=None,
            ),
        ] = None,
        aliases: Annotated[
            str | list[str] | None,
            Field(
                description="List of voice assistant aliases for the entity (replaces existing aliases). Single entity only.",
                default=None,
            ),
        ] = None,
        labels: Annotated[
            str | list[str] | None,
            Field(
                description="List of label IDs for the entity. Behavior depends on label_operation parameter. Supports bulk operations.",
                default=None,
            ),
        ] = None,
        label_operation: Annotated[
            Literal["set", "add", "remove"],
            Field(
                description="How to apply labels: 'set' replaces all labels, 'add' adds to existing, 'remove' removes specified labels.",
                default="set",
            ),
        ] = "set",
        expose_to: Annotated[
            str | dict[str, bool] | None,
            Field(
                description=(
                    "Control voice assistant exposure. Pass a dict mapping assistant IDs to booleans. "
                    "Valid assistants: 'conversation' (Assist), 'cloud.alexa', 'cloud.google_assistant'. "
                    "Example: {\"conversation\": true, \"cloud.alexa\": false}. Supports bulk operations."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Update entity properties in the entity registry.

        Allows modifying entity metadata such as area assignment, display name,
        icon, enabled/disabled state, visibility, aliases, labels, and voice
        assistant exposure in a single call.

        BULK OPERATIONS:
        When entity_id is a list, only labels and expose_to parameters are supported.
        Other parameters (area_id, name, icon, enabled, hidden, aliases) require single entity.

        LABEL OPERATIONS:
        - label_operation="set" (default): Replace all labels with the provided list. Use [] to clear.
        - label_operation="add": Add labels to existing ones without removing any.
        - label_operation="remove": Remove specified labels from the entity.

        Use ha_search_entities() or ha_get_device() to find entity IDs.
        Use ha_config_get_label() to find available label IDs.

        EXAMPLES:
        Single entity:
        - Assign to area: ha_set_entity("sensor.temp", area_id="living_room")
        - Rename: ha_set_entity("sensor.temp", name="Living Room Temperature")
        - Set labels: ha_set_entity("light.lamp", labels=["outdoor", "smart"])
        - Add labels: ha_set_entity("light.lamp", labels=["new_label"], label_operation="add")
        - Remove labels: ha_set_entity("light.lamp", labels=["old_label"], label_operation="remove")
        - Clear labels: ha_set_entity("light.lamp", labels=[])
        - Expose to Alexa: ha_set_entity("light.lamp", expose_to={"cloud.alexa": True})

        Bulk operations:
        - Set labels on multiple: ha_set_entity(["light.a", "light.b"], labels=["outdoor"])
        - Add labels to multiple: ha_set_entity(["light.a", "light.b"], labels=["new"], label_operation="add")
        - Expose multiple to Alexa: ha_set_entity(["light.a", "light.b"], expose_to={"cloud.alexa": True})

        NOTE: To rename an entity_id (e.g., sensor.old -> sensor.new), use ha_rename_entity() instead.
        """
        try:
            # Parse entity_id - determine if bulk operation
            entity_ids: list[str]
            is_bulk: bool

            if isinstance(entity_id, str):
                entity_ids = [entity_id]
                is_bulk = False
            elif isinstance(entity_id, list):
                if not entity_id:
                    return {
                        "success": False,
                        "error": "entity_id list cannot be empty",
                    }
                if not all(isinstance(e, str) for e in entity_id):
                    return {
                        "success": False,
                        "error": "All entity_id values must be strings",
                    }
                entity_ids = entity_id
                is_bulk = len(entity_ids) > 1
            else:
                return {
                    "success": False,
                    "error": f"entity_id must be string or list of strings, got {type(entity_id).__name__}",
                }

            # Validate: bulk operations only support labels and expose_to
            single_entity_params = {
                "area_id": area_id,
                "name": name,
                "icon": icon,
                "enabled": enabled,
                "hidden": hidden,
                "aliases": aliases,
            }
            non_null_single_params = [k for k, v in single_entity_params.items() if v is not None]

            if is_bulk and non_null_single_params:
                return {
                    "success": False,
                    "error": f"Bulk operations (multiple entity_ids) only support labels and expose_to. "
                             f"Single-entity parameters provided: {non_null_single_params}",
                    "suggestions": [
                        "Use a single entity_id for area_id, name, icon, enabled, hidden, or aliases",
                        "Or remove single-entity parameters to use bulk labels/expose_to",
                    ],
                }

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
                try:
                    parsed = parse_json_param(expose_to, "expose_to")
                except ValueError as e:
                    return create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        str(e),
                    )

                if not isinstance(parsed, dict):
                    return create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "expose_to must be a dict mapping assistant IDs to booleans, "
                        'e.g. {"conversation": true, "cloud.alexa": false}',
                    )
                parsed_expose_to = parsed

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
                    try:
                        coerced = coerce_bool_param(val, f"expose_to[{asst}]")
                    except ValueError as e:
                        return create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            str(e),
                        )
                    if coerced is None:
                        return create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"expose_to[{asst}] must be a boolean value",
                        )
                    parsed_expose_to[asst] = coerced

            # Single entity case - use existing logic
            if not is_bulk:
                return await _update_single_entity(
                    entity_ids[0],
                    area_id,
                    name,
                    icon,
                    enabled,
                    hidden,
                    parsed_aliases,
                    parsed_labels,
                    label_operation,
                    parsed_expose_to,
                )

            # Bulk case - process each entity
            logger.info(f"Bulk updating {len(entity_ids)} entities")

            results = await asyncio.gather(
                *[
                    _update_single_entity(
                        eid,
                        None,  # area_id not supported in bulk
                        None,  # name not supported in bulk
                        None,  # icon not supported in bulk
                        None,  # enabled not supported in bulk
                        None,  # hidden not supported in bulk
                        None,  # aliases not supported in bulk
                        parsed_labels,
                        label_operation,
                        parsed_expose_to,
                    )
                    for eid in entity_ids
                ],
                return_exceptions=True,
            )

            # Aggregate results
            succeeded: list[dict[str, Any]] = []
            failed: list[dict[str, Any]] = []

            for eid, result in zip(entity_ids, results, strict=True):
                if isinstance(result, BaseException):
                    failed.append({
                        "entity_id": eid,
                        "error": str(result),
                    })
                elif result.get("success"):
                    succeeded.append({
                        "entity_id": eid,
                        "entity_entry": result.get("entity_entry"),
                        "updates": result.get("updates"),
                    })
                else:
                    failed.append({
                        "entity_id": eid,
                        "error": result.get("error", "Unknown error"),
                    })

            response: dict[str, Any] = {
                "success": len(failed) == 0,
                "total": len(entity_ids),
                "succeeded_count": len(succeeded),
                "failed_count": len(failed),
                "succeeded": succeeded,
            }

            if failed:
                response["failed"] = failed
                response["partial"] = len(succeeded) > 0

            return response

        except Exception as e:
            logger.error(f"Error updating entity: {e}")
            eid_context = entity_id if isinstance(entity_id, str) else entity_ids
            return exception_to_structured_error(e, context={"entity_id": eid_context})

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
