"""
Entity management tools for Home Assistant MCP server.

This module provides tools for managing entity lifecycle and properties
via the Home Assistant entity registry API.
"""

import asyncio
import logging
import re
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
)
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
        "categories": entry.get("categories", {}),
        "device_class": entry.get("device_class"),
        "original_device_class": entry.get("original_device_class"),
        "options": entry.get("options", {}),
    }


def _extract_ws_error(result: dict[str, Any]) -> str:
    """Pull a user-readable message out of a failed WebSocket response.

    Falls back to a static placeholder + warning log when HA returns an
    empty or malformed error envelope, so the user-facing message never
    degrades to literal "{}".
    """
    error = result.get("error")
    if isinstance(error, dict):
        msg = error.get("message")
        if isinstance(msg, str) and msg:
            return msg
    elif isinstance(error, str) and error:
        return error
    logger.warning("HA WS response had no usable error detail: %r", result)
    return "no error detail returned by Home Assistant"


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
            return None, _extract_ws_error(result)
        return result.get("result", {}).get("labels", []), None

    async def _update_single_entity(
        entity_id: str,
        area_id: str | None,
        name: str | None,
        icon: str | None,
        enabled: bool | str | None,
        hidden: bool | str | None,
        parsed_aliases: list[str] | None,
        parsed_categories: dict[str, str | None] | None,
        parsed_labels: list[str] | None,
        label_operation: str,
        parsed_expose_to: dict[str, bool] | None,
        new_entity_id: str | None = None,
        new_device_name: str | None = None,
        device_class: str | None = None,
        parsed_options: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Update a single entity. Returns the response dict."""
        # For add/remove operations, we need to fetch current labels first
        final_labels = parsed_labels
        if parsed_labels is not None and label_operation in ("add", "remove"):
            current_labels, error_msg = await _get_entity_labels(entity_id)
            if current_labels is None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to get current labels for {entity_id}: {error_msg}",
                        context={"entity_id": entity_id},
                    )
                )

            if label_operation == "add":
                # Add new labels without duplicates
                final_labels = list(set(current_labels) | set(parsed_labels))
            else:  # remove
                # Remove specified labels - use set for O(1) membership check
                labels_to_remove = set(parsed_labels)
                final_labels = [
                    lbl for lbl in current_labels if lbl not in labels_to_remove
                ]

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

        if device_class is not None:
            # Treat whitespace-only as the documented "clear" sentinel so
            # accidental spaces don't reach HA as a literal validation error.
            normalized_device_class = device_class.strip() or None
            message["device_class"] = normalized_device_class
            updates_made.append(
                f"device_class='{normalized_device_class}'"
                if normalized_device_class
                else "device_class cleared"
            )

        if enabled is not None:
            try:
                enabled_bool = coerce_bool_param(enabled, "enabled")
            except ValueError as e:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        str(e),
                    )
                )
            message["disabled_by"] = None if enabled_bool else "user"
            updates_made.append("enabled" if enabled_bool else "disabled")

        if hidden is not None:
            try:
                hidden_bool = coerce_bool_param(hidden, "hidden")
            except ValueError as e:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        str(e),
                    )
                )
            message["hidden_by"] = "user" if hidden_bool else None
            updates_made.append("hidden" if hidden_bool else "visible")

        if parsed_aliases is not None:
            message["aliases"] = parsed_aliases
            updates_made.append(f"aliases={parsed_aliases}")

        if parsed_categories is not None:
            message["categories"] = parsed_categories
            updates_made.append(f"categories={parsed_categories}")

        if final_labels is not None:
            message["labels"] = final_labels
            if label_operation == "set":
                updates_made.append(f"labels={final_labels}")
            elif label_operation == "add":
                updates_made.append(f"labels added: {parsed_labels} -> {final_labels}")
            else:  # remove
                updates_made.append(
                    f"labels removed: {parsed_labels} -> {final_labels}"
                )

        if new_entity_id is not None:
            entity_pattern = r"^[a-z_]+\.[a-z0-9_]+$"
            if not re.match(entity_pattern, new_entity_id):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid new_entity_id format: {new_entity_id}",
                        suggestions=[
                            "Use format: domain.object_id (lowercase letters, numbers, underscores only)"
                        ],
                        context={"new_entity_id": new_entity_id},
                    )
                )
            current_domain = entity_id.split(".")[0]
            new_domain = new_entity_id.split(".")[0]
            if current_domain != new_domain:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Domain mismatch: cannot change from '{current_domain}' to '{new_domain}'",
                        suggestions=[
                            f"New entity_id must start with '{current_domain}.'"
                        ],
                        context={
                            "entity_id": entity_id,
                            "new_entity_id": new_entity_id,
                        },
                    )
                )
            message["new_entity_id"] = new_entity_id
            updates_made.append(f"entity_id -> {new_entity_id}")

        if parsed_expose_to is not None:
            updates_made.append(f"expose_to={parsed_expose_to}")

        if new_device_name is not None:
            updates_made.append(f"device_name -> {new_device_name}")

        # parsed_options entries are appended to updates_made AFTER each per-domain
        # WS call succeeds, so the response never falsely claims an unwritten domain
        # was updated. Empty-input check below treats them as "pending" updates.
        if not updates_made and not parsed_options:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "No updates specified",
                    suggestions=[
                        "Provide at least one of: area_id, name, icon, device_class, enabled, hidden, aliases, categories, labels, options, expose_to, new_entity_id, or new_device_name"
                    ],
                )
            )

        # Save original entity_id before potential rename
        original_entity_id = entity_id

        # Send entity registry update (covers all fields except expose_to)
        has_registry_updates = len(message) > 2  # more than just type + entity_id
        entity_entry: dict[str, Any] = {}

        if has_registry_updates:
            registry_update_fields = [
                u for u in updates_made if not u.startswith("expose_to=")
            ]
            logger.info(
                f"Updating entity registry for {entity_id}: {', '.join(registry_update_fields)}"
            )
            result = await client.send_websocket_message(message)

            if not result.get("success"):
                error_msg = _extract_ws_error(result)
                suggestions = [
                    "Verify the entity_id exists using ha_search_entities()",
                ]
                if new_entity_id is not None:
                    suggestions.extend(
                        [
                            "Check that the new entity_id doesn't already exist",
                            "Ensure the entity has a unique_id (some legacy entities cannot be renamed)",
                        ]
                    )
                else:
                    suggestions.extend(
                        [
                            "Check that area_id exists if specified",
                            "Some entities may not support all update options",
                        ]
                    )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to update entity: {error_msg}",
                        context={"entity_id": entity_id},
                        suggestions=suggestions,
                    )
                )

            entity_entry = result.get("result", {}).get("entity_entry", {})

            # If entity was renamed, update entity_id for subsequent operations
            if new_entity_id:
                entity_id = new_entity_id

        # Per-domain options updates: HA's WS schema requires `options_domain`
        # and `options` to be sent paired one domain per call (the API takes a
        # single domain's sub-dict). An agent-supplied {domain: {...}, ...} is
        # therefore split into one registry update per domain.
        options_succeeded: dict[str, dict[str, Any]] = {}
        if parsed_options:
            for opts_domain, opts_sub in parsed_options.items():
                opts_msg: dict[str, Any] = {
                    "type": "config/entity_registry/update",
                    "entity_id": entity_id,
                    "options_domain": opts_domain,
                    "options": opts_sub,
                }
                opts_result = await client.send_websocket_message(opts_msg)
                if not opts_result.get("success"):
                    err_msg = _extract_ws_error(opts_result)
                    partial = bool(options_succeeded) or has_registry_updates
                    msg_prefix = (
                        "Partially updated entity; failed updating options for"
                        if partial
                        else "Failed to update options for"
                    )
                    # `options_succeeded` is the structured retriable form
                    # (agent can re-feed it minus the failing domain).
                    # `updates_applied` is the human-readable prose list
                    # including non-options updates (name=, icon=, etc.).
                    # Both are surfaced — they serve different consumers.
                    options_failure_context: dict[str, Any] = {
                        "entity_id": entity_id,
                        "options_domain": opts_domain,
                        "partial": partial,
                        "options_succeeded": options_succeeded,
                        "updates_applied": list(updates_made),
                    }
                    # Only include entity_entry when something actually mutated;
                    # _format_entity_entry({}) returns an all-None stub that's
                    # indistinguishable from "entity has nothing set". Mirrors
                    # the expose_to failure path below.
                    if partial:
                        options_failure_context["entity_entry"] = _format_entity_entry(
                            entity_entry
                        )
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.SERVICE_CALL_FAILED,
                            f"{msg_prefix} domain '{opts_domain}': {err_msg}",
                            context=options_failure_context,
                        )
                    )
                # HA returns the cumulative entity_entry on each per-domain
                # call, so last-call-wins reassignment leaves the final loop
                # iteration carrying the full state.
                entity_entry = opts_result.get("result", {}).get(
                    "entity_entry", entity_entry
                )
                options_succeeded[opts_domain] = opts_sub
                updates_made.append(f"options[{opts_domain}]={opts_sub}")

        # Handle new_device_name — rename the associated device
        # Normalize empty string to None (no-op, don't clear device name)
        if new_device_name is not None and not new_device_name.strip():
            new_device_name = None
        device_rename_result: dict[str, Any] | None = None
        if new_device_name is not None:
            # If no registry update was sent, fetch entity_entry to get device_id
            if not entity_entry:
                device_lookup_msg: dict[str, Any] = {
                    "type": "config/entity_registry/get",
                    "entity_id": entity_id,
                }
                get_result = await client.send_websocket_message(device_lookup_msg)
                if get_result.get("success"):
                    entity_entry = get_result.get("result", {})
                else:
                    logger.warning(
                        "Entity registry lookup failed for %s: %s",
                        entity_id,
                        _extract_ws_error(get_result),
                    )
                    device_rename_result = {
                        "warning": "Entity registry lookup failed — could not determine device. Retry may succeed.",
                    }

            device_id = (
                entity_entry.get("device_id") if not device_rename_result else None
            )
            if not device_id:
                device_rename_result = {
                    "warning": "Entity has no associated device — device rename skipped",
                }
            else:
                device_msg: dict[str, Any] = {
                    "type": "config/device_registry/update",
                    "device_id": device_id,
                    "name_by_user": new_device_name if new_device_name else None,
                }
                device_result = await client.send_websocket_message(device_msg)
                if device_result.get("success"):
                    device_rename_result = {"success": True, "device_id": device_id}
                else:
                    device_rename_result = {
                        "warning": f"Entity updated but device rename failed: {_extract_ws_error(device_result)}",
                        "device_id": device_id,
                    }

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
                    error_msg = _extract_ws_error(expose_result)
                    failed = dict.fromkeys(assistants, should_expose)
                    # `partial` must reflect every prior mutation in the function:
                    # main registry update, per-domain options, device rename, and
                    # any expose_to batch (e.g. expose_true) that ran before this
                    # one (expose_false) failed. Anything truthy in those means
                    # the registry already moved.
                    prior_mutation = (
                        has_registry_updates
                        or bool(options_succeeded)
                        or bool(succeeded)
                        or bool(
                            device_rename_result and device_rename_result.get("success")
                        )
                    )
                    context: dict[str, Any] = {
                        "entity_id": entity_id,
                        "exposure_succeeded": succeeded,
                        "exposure_failed": failed,
                    }
                    if prior_mutation:
                        context["partial"] = True
                        context["entity_entry"] = _format_entity_entry(entity_entry)
                        if options_succeeded:
                            context["options_succeeded"] = options_succeeded
                        if device_rename_result and device_rename_result.get("success"):
                            context["device_rename_succeeded"] = True
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.SERVICE_CALL_FAILED,
                            f"Exposure failed: {error_msg}",
                            context=context,
                            suggestions=[
                                "Check Home Assistant connection and entity availability"
                            ],
                        )
                    )

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
                raise_tool_error(
                    create_error_response(
                        ErrorCode.ENTITY_NOT_FOUND,
                        f"Entity '{entity_id}' not found in registry after applying exposure changes",
                        context={
                            "entity_id": entity_id,
                            "exposure_succeeded": exposure_result,
                        },
                        suggestions=[
                            "Verify the entity_id exists using ha_search_entities()",
                            "The entity's exposure settings were likely changed, but its current state could not be confirmed.",
                        ],
                    )
                )

        response_data: dict[str, Any] = {
            "success": True,
            "entity_id": entity_id,
            "updates": updates_made,
            "entity_entry": _format_entity_entry(entity_entry),
            "message": f"Entity updated: {', '.join(updates_made)}",
        }

        # Include old_entity_id and rename warning when a rename was performed
        if new_entity_id is not None:
            response_data["old_entity_id"] = original_entity_id
            response_data["warning"] = (
                "Remember to update any automations, scripts, or dashboards "
                "that reference the old entity_id"
            )

        if exposure_result is not None:
            response_data["exposure"] = exposure_result

        if device_rename_result is not None:
            response_data["device_rename"] = device_rename_result
            # Only mark partial when device rename was attempted and failed
            # (not when entity simply has no device)
            if "warning" in device_rename_result and device_rename_result.get(
                "device_id"
            ):
                response_data["partial"] = True

        return response_data

    @mcp.tool(
        tags={"Entity Registry"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "title": "Set Entity",
        },
    )
    @log_tool_usage
    async def ha_set_entity(
        entity_id: Annotated[
            str | list[str],
            Field(
                description="Entity ID or list of entity IDs to update. Bulk operations (list) only support labels, expose_to, and categories parameters."
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
        device_class: Annotated[
            str | None,
            Field(
                description=(
                    "Override the entity's display device class — what the HA UI's "
                    "'Show As' dropdown writes. Use empty string '' to clear the "
                    "override and fall back to the integration default. None (the "
                    "default) means 'no change' — pass an explicit '' to clear. "
                    "Single entity only. Examples: 'window', 'door', 'motion' for "
                    "binary_sensor; 'temperature', 'humidity' for sensor."
                ),
                default=None,
            ),
        ] = None,
        options: Annotated[
            str | dict[str, dict[str, Any]] | None,
            Field(
                description=(
                    "Per-domain entity registry options (e.g. sensor 'display_precision', "
                    "weather 'forecast_type'). Pass a dict mapping domain to a sub-dict, "
                    'e.g. {"sensor": {"display_precision": 2}}. JSON-string form also accepted. '
                    "Multiple domains are sent as separate registry updates. "
                    "For 'Show As' use the dedicated `device_class` parameter — that is "
                    "what the HA UI Show As dropdown writes. Voice-assistant exposure is "
                    "stored under `options.<assistant>.should_expose` but must be managed "
                    "via the dedicated `expose_to` parameter, not this options dict. "
                    "Single entity only."
                ),
                default=None,
            ),
        ] = None,
        enabled: Annotated[
            bool | str | None,
            Field(
                description=(
                    "True to enable the entity, False to disable it. Single entity only. "
                    "WARNING: Setting enabled=False is a registry-level disable — it completely "
                    "removes the entity from the state machine and hides it from the UI. "
                    "A reload or restart is required to restore it after re-enabling. "
                    "NOT allowed for automation or script entities — use automation.turn_off / "
                    "script.turn_off via ha_call_service() instead."
                ),
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
        categories: Annotated[
            str | dict[str, str | None] | None,
            Field(
                description=(
                    "Category assignment as a dict mapping scope to category_id. "
                    'Example: {"automation": "category_id_here"}. '
                    'Use null value to clear: {"automation": null}. '
                    "Single entity only."
                ),
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
                    'Example: {"conversation": true, "cloud.alexa": false}. Supports bulk operations.'
                ),
                default=None,
            ),
        ] = None,
        new_entity_id: Annotated[
            str | None,
            Field(
                description=(
                    "New entity ID to rename to (e.g., 'light.new_name'). "
                    "Domain must match the original. Single entity only."
                ),
                default=None,
            ),
        ] = None,
        new_device_name: Annotated[
            str | None,
            Field(
                description=(
                    "New display name for the associated device. "
                    "If provided, both entity and device are updated in one operation. Single entity only."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Update entity properties in the entity registry.

        Allows modifying entity metadata such as area assignment, display name,
        icon, "Show As" device class override, per-domain registry options,
        enabled/disabled state, visibility, aliases, labels, voice assistant
        exposure, and entity_id rename in a single call.

        BULK OPERATIONS:
        When entity_id is a list, only labels, expose_to, and categories parameters are supported.
        Other parameters (area_id, name, icon, device_class, options, enabled, hidden, aliases, new_entity_id, new_device_name) require single entity.

        LABEL OPERATIONS:
        - label_operation="set" (default): Replace all labels with the provided list. Use [] to clear.
        - label_operation="add": Add labels to existing ones without removing any.
        - label_operation="remove": Remove specified labels from the entity.

        SHOW AS / DEVICE CLASS:
        device_class overrides the entity's display device class — equivalent to the
        HA UI's "Show As" dropdown. Use empty string '' to clear. Applies instantly,
        no reload needed.

        REGISTRY OPTIONS:
        options carries per-domain registry options (sensor display_precision,
        weather forecast_type, etc). Pass {domain: {key: value}}; multi-domain
        dicts are sent as separate registry updates because HA's WS schema
        requires options_domain + options to be paired one domain at a time.

        ENTITY ID RENAME:
        Use new_entity_id to change an entity's ID (e.g., sensor.old -> sensor.new).
        Domain must match. Voice exposure settings are preserved automatically.

        WARNING: Renaming an entity_id does NOT update references in automations,
        scripts, templates, or dashboards. All consumers of the old entity_id must
        be updated manually — HA does not propagate the rename automatically.

        Rename limitations:
        - Entity history is preserved (HA 2022.4+)
        - Entities without unique IDs cannot be renamed
        - Entities disabled by their integration cannot be renamed

        DEVICE RENAME:
        Use new_device_name to rename the associated device. Can be combined with
        new_entity_id to rename both in one call. The device is looked up automatically.

        Use ha_search_entities() or ha_get_device() to find entity IDs.
        Use ha_config_get_label() to find available label IDs.

        EXAMPLES:
        Single entity:
        - Assign to area: ha_set_entity("sensor.temp", area_id="living_room")
        - Rename display name: ha_set_entity("sensor.temp", name="Living Room Temperature")
        - Set Show As: ha_set_entity("binary_sensor.zone_10", device_class="window")
        - Clear Show As: ha_set_entity("binary_sensor.zone_10", device_class="")
        - Set sensor precision: ha_set_entity("sensor.power", options={"sensor": {"display_precision": 2}})
        - Rename entity_id: ha_set_entity("light.old_name", new_entity_id="light.new_name")
        - Rename entity and device: ha_set_entity("light.old", new_entity_id="light.new", new_device_name="New Lamp")
        - Rename entity_id with friendly name: ha_set_entity("sensor.old", new_entity_id="sensor.new", name="New Name")
        - Set labels: ha_set_entity("light.lamp", labels=["outdoor", "smart"])
        - Add labels: ha_set_entity("light.lamp", labels=["new_label"], label_operation="add")
        - Remove labels: ha_set_entity("light.lamp", labels=["old_label"], label_operation="remove")
        - Clear labels: ha_set_entity("light.lamp", labels=[])
        - Expose to Alexa: ha_set_entity("light.lamp", expose_to={"cloud.alexa": True})

        Bulk operations:
        - Set labels on multiple: ha_set_entity(["light.a", "light.b"], labels=["outdoor"])
        - Add labels to multiple: ha_set_entity(["light.a", "light.b"], labels=["new"], label_operation="add")
        - Expose multiple to Alexa: ha_set_entity(["light.a", "light.b"], expose_to={"cloud.alexa": True})

        ENABLED/DISABLED WARNING:
        Setting enabled=False performs a **registry-level disable** — the entity is completely
        removed from the Home Assistant state machine and hidden from the UI. It will NOT appear
        in state queries, dashboards, or automations until re-enabled AND the integration is
        reloaded. This is NOT the same as "turning off" an entity.

        For automations and scripts, enabled=False is blocked. Use these instead:
        - ha_call_service("automation", "turn_off", entity_id="automation.xxx")
        - ha_call_service("script", "turn_off", entity_id="script.xxx")
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
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "entity_id list cannot be empty",
                        )
                    )
                if not all(isinstance(e, str) for e in entity_id):
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "All entity_id values must be strings",
                        )
                    )
                entity_ids = entity_id
                is_bulk = len(entity_ids) > 1
            else:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"entity_id must be string or list of strings, got {type(entity_id).__name__}",
                    )
                )

            # Validate: bulk operations only support categories, labels, and expose_to
            single_entity_params = {
                "area_id": area_id,
                "name": name,
                "icon": icon,
                "device_class": device_class,
                "options": options,
                "enabled": enabled,
                "hidden": hidden,
                "aliases": aliases,
                "new_entity_id": new_entity_id,
                "new_device_name": new_device_name,
            }
            non_null_single_params = [
                k for k, v in single_entity_params.items() if v is not None
            ]

            if is_bulk and non_null_single_params:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Bulk operations (multiple entity_ids) only support categories, labels, and expose_to. "
                        f"Single-entity parameters provided: {non_null_single_params}",
                        suggestions=[
                            "Use a single entity_id for area_id, name, icon, device_class, options, enabled, hidden, or aliases",
                            "Or remove single-entity parameters to use bulk categories/labels/expose_to",
                        ],
                    )
                )

            # Block registry-disable on automation and script entities.
            # Registry-disabling (enabled=False) removes the entity from the HA
            # state machine entirely, making it invisible in the UI and
            # unqueryable via state APIs until re-enabled AND the integration is
            # reloaded.  For automations and scripts the correct way to
            # "disable" them is via their domain services (automation.turn_off /
            # script.turn_off) which simply prevent them from running while
            # keeping them visible and manageable.
            if enabled is not None:
                try:
                    _enabled_check = coerce_bool_param(enabled, "enabled")
                except ValueError:
                    _enabled_check = None  # will be caught by _update_single_entity

                if _enabled_check is False:
                    blocked = [
                        eid
                        for eid in entity_ids
                        if eid.split(".")[0] in ("automation", "script")
                    ]
                    if blocked:
                        _domain = blocked[0].split(".")[0]
                        _service_hint = f"{_domain}.turn_off"
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.VALIDATION_INVALID_PARAMETER,
                                f"Cannot registry-disable {_domain} entities with ha_set_entity(enabled=False). "
                                f"This removes the entity from the state machine and hides it from the UI "
                                f"until it is re-enabled AND the {_domain}s are reloaded. "
                                f"Use ha_call_service('{_domain}', 'turn_off', entity_id='{blocked[0]}') instead "
                                f"to disable it without removing it.",
                                suggestions=[
                                    f"Use {_service_hint} to disable the {_domain} (keeps it visible and manageable)",
                                    f"Use {_domain}.turn_on to re-enable it later",
                                    "ha_set_entity(enabled=False) is for registry-level disable — it fully hides the entity",
                                ],
                            )
                        )

            # Parse list parameters if provided as strings
            parsed_aliases = None
            if aliases is not None:
                try:
                    parsed_aliases = parse_string_list_param(aliases, "aliases")
                except ValueError as e:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"Invalid aliases parameter: {e}",
                        )
                    )

            parsed_categories: dict[str, str | None] | None = None
            if categories is not None:
                try:
                    parsed_cats = parse_json_param(categories, "categories")
                except ValueError as e:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"Invalid categories parameter: {e}",
                        )
                    )

                if not isinstance(parsed_cats, dict):
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "categories must be a dict mapping scope to category_id, "
                            'e.g. {"automation": "my_category_id"}',
                        )
                    )
                parsed_categories = parsed_cats

            parsed_labels = None
            if labels is not None:
                try:
                    parsed_labels = parse_string_list_param(labels, "labels")
                except ValueError as e:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"Invalid labels parameter: {e}",
                        )
                    )

            parsed_options: dict[str, dict[str, Any]] | None = None
            if options is not None:
                try:
                    parsed_opts = parse_json_param(options, "options")
                except ValueError as e:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"Invalid options parameter: {e}",
                        )
                    )

                if not isinstance(parsed_opts, dict):
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"options must be a dict mapping domain to a sub-dict "
                            f"(got {type(parsed_opts).__name__}), "
                            'e.g. {"sensor": {"display_precision": 2}}',
                        )
                    )
                if not parsed_opts:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "options cannot be an empty dict — pass at least one "
                            'domain entry, e.g. {"sensor": {"display_precision": 2}}, '
                            "or omit the parameter entirely.",
                        )
                    )
                bad_subs = [
                    f"{k!r}: {type(v).__name__}"
                    for k, v in parsed_opts.items()
                    if not isinstance(v, dict)
                ]
                if bad_subs:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "options sub-values must be dicts, got non-dict for: "
                            f"{', '.join(bad_subs)}",
                        )
                    )
                parsed_options = parsed_opts

            # Parse and validate expose_to parameter
            parsed_expose_to: dict[str, bool] | None = None
            if expose_to is not None:
                try:
                    parsed = parse_json_param(expose_to, "expose_to")
                except ValueError as e:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            str(e),
                        )
                    )

                if not isinstance(parsed, dict):
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "expose_to must be a dict mapping assistant IDs to booleans, "
                            'e.g. {"conversation": true, "cloud.alexa": false}',
                        )
                    )
                parsed_expose_to = parsed

                # Validate assistant names
                invalid_assistants = [
                    a for a in parsed_expose_to if a not in KNOWN_ASSISTANTS
                ]
                if invalid_assistants:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            f"Invalid assistant(s) in expose_to: {invalid_assistants}. "
                            f"Valid: {KNOWN_ASSISTANTS}",
                        )
                    )

                # Coerce values to bool
                for asst, val in parsed_expose_to.items():
                    try:
                        coerced = coerce_bool_param(val, f"expose_to[{asst}]")
                    except ValueError as e:
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.VALIDATION_INVALID_PARAMETER,
                                str(e),
                            )
                        )
                    if coerced is None:
                        raise_tool_error(
                            create_error_response(
                                ErrorCode.VALIDATION_INVALID_PARAMETER,
                                f"expose_to[{asst}] must be a boolean value",
                            )
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
                    parsed_categories,
                    parsed_labels,
                    label_operation,
                    parsed_expose_to,
                    new_entity_id=new_entity_id,
                    new_device_name=new_device_name,
                    device_class=device_class,
                    parsed_options=parsed_options,
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
                        None,  # categories not supported in bulk
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
                    failed.append(
                        {
                            "entity_id": eid,
                            "error": str(result),
                        }
                    )
                else:
                    # _update_single_entity always returns success-shape or
                    # raises ToolError (caught above as BaseException), so the
                    # `result.get("success") is False` branch is unreachable.
                    succeeded.append(
                        {
                            "entity_id": eid,
                            "entity_entry": result.get("entity_entry"),
                            "updates": result.get("updates"),
                        }
                    )

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

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error updating entity: {e}")
            eid_context = entity_id if isinstance(entity_id, str) else entity_ids
            exception_to_structured_error(e, context={"entity_id": eid_context})

    @mcp.tool(
        tags={"Entity Registry"},
        annotations={
            "readOnlyHint": True,
            "idempotentHint": True,
            "title": "Get Entity",
        },
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
        - categories: Category assignments (dict mapping scope to category_id)
        - device_class: User "Show As" override (null = use original_device_class)
        - original_device_class: Default device class from the integration
        - options: Per-domain registry options (e.g. sensor display_precision).
          Voice-assistant exposure is also stored here but should be set/cleared
          via the ha_set_entity(expose_to=...) parameter, not the options dict.
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
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "All entity_id values must be strings",
                        )
                    )
                entity_ids = entity_id
                is_bulk = True
            else:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"entity_id must be string or list of strings, got {type(entity_id).__name__}",
                    )
                )

            async def _fetch_entity(eid: str) -> dict[str, Any]:
                """Fetch a single entity from the registry."""
                message: dict[str, Any] = {
                    "type": "config/entity_registry/get",
                    "entity_id": eid,
                }
                result = await client.send_websocket_message(message)

                if not result.get("success"):
                    raise ValueError(_extract_ws_error(result))

                entry = result.get("result", {})
                return {
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
                    "categories": entry.get("categories", {}),
                    "device_class": entry.get("device_class"),
                    "original_device_class": entry.get("original_device_class"),
                    "options": entry.get("options", {}),
                    "platform": entry.get("platform"),
                    "device_id": entry.get("device_id"),
                    "unique_id": entry.get("unique_id"),
                }

            # Single entity case
            if not is_bulk:
                eid = entity_ids[0]
                logger.info(f"Getting entity registry entry for {eid}")
                try:
                    result = await _fetch_entity(eid)
                except ValueError as e:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.SERVICE_CALL_FAILED,
                            f"Entity not found: {e}",
                            context={"entity_id": eid},
                            suggestions=[
                                "Use ha_search_entities() to find valid entity IDs",
                                "Check the entity_id spelling and format (e.g., 'sensor.temperature')",
                            ],
                        )
                    )
                return {
                    "success": True,
                    "entity_id": eid,
                    "entity_entry": result,
                }

            # Bulk case - fetch all entities
            logger.info(
                f"Getting entity registry entries for {len(entity_ids)} entities"
            )
            results = await asyncio.gather(
                *[_fetch_entity(eid) for eid in entity_ids],
                return_exceptions=True,
            )

            entity_entries: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []

            for eid, fetch_result in zip(entity_ids, results, strict=True):
                if isinstance(fetch_result, BaseException):
                    errors.append(
                        {
                            "entity_id": eid,
                            "error": str(fetch_result),
                        }
                    )
                else:
                    entity_entries.append(fetch_result)

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

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting entity: {e}")
            exception_to_structured_error(
                e,
                context={
                    "entity_id": entity_id if isinstance(entity_id, str) else entity_ids
                },
            )

    @mcp.tool(
        tags={"Entity Registry"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "title": "Remove Entity",
        },
    )
    @log_tool_usage
    async def ha_remove_entity(
        entity_id: Annotated[
            str,
            Field(
                description=(
                    "Entity ID to remove from the entity registry "
                    "(e.g., 'sensor.old_temperature'). "
                    "This permanently removes the entity registration."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Remove an entity from the Home Assistant entity registry.

        Permanently removes the entity registration from Home Assistant.
        The entity will no longer appear in the UI or be available to automations.

        WARNING: This permanently removes the entity registration.
        - Use only for orphaned or stale entity entries
        - If the underlying device or integration is still active, the entity
          may be re-added automatically on the next HA restart or reload
        - This action cannot be undone without restoring from backup

        EXAMPLES:
        - Remove orphaned sensor: ha_remove_entity("sensor.old_temperature")
        - Remove stale helper entry: ha_remove_entity("input_boolean.deleted_helper")

        NOTE: For most use cases, consider disabling instead:
        ha_set_entity(entity_id="sensor.old", enabled=False)

        RELATED TOOLS:
        - ha_search_entities: Find entities to verify the entity_id before removing
        - ha_get_entity: Check entity details before removal
        """
        try:
            result = await client.send_websocket_message(
                {"type": "config/entity_registry/remove", "entity_id": entity_id}
            )

            if not result.get("success"):
                error_msg = _extract_ws_error(result)
                if "not found" in error_msg.lower():
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.ENTITY_NOT_FOUND,
                            f"Entity '{entity_id}' not found in registry",
                            context={"entity_id": entity_id},
                            suggestions=[
                                "Use ha_search_entities() to find valid entity IDs",
                                "The entity may have already been removed",
                            ],
                        )
                    )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to remove entity '{entity_id}': {error_msg}",
                        context={"entity_id": entity_id},
                        suggestions=[
                            "Check HA logs for details on why the removal was rejected",
                        ],
                    )
                )

            return {"success": True, "entity_id": entity_id}

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error removing entity '{entity_id}': {e}")
            exception_to_structured_error(
                e,
                context={"entity_id": entity_id},
            )
