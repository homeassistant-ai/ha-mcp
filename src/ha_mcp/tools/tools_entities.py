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
from fastmcp.tools import tool
from pydantic import Field

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from .auto_backup import with_auto_backup
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import (
    exception_to_structured_error,
    extract_tool_error_message,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .tools_voice_assistant import KNOWN_ASSISTANTS
from .util_helpers import (
    JSON_STRING_COERCION,
    parse_json_param,
    parse_string_list_param,
)

logger = logging.getLogger(__name__)

# The ha_mcp_tools/entity_enrich WS command: resolved area/floor/label NAMES +
# aliases per entity_id, byte-shaped like the component's registry join. Named
# once so the routing helper and its tests stay in lockstep.
WS_ENTITY_ENRICH = "ha_mcp_tools/entity_enrich"

# Bounds the per-frame size of a bulk config/entity_registry/get_entries call
# (extended entries, ~1KB each) so a large id list can't produce an over-cap
# WebSocket frame. Matches the chunk size smart_search uses for the same
# command. Typical bulk calls fit in a single chunk (one WS message).
_GET_ENTRIES_CHUNK_SIZE = 500

# Max entity IDs accepted by the bulk-removal path of ha_remove_entity. Mirrors
# the cap the other bulk tools use (ha_get_state's _get_bulk_entity_states).
_MAX_BULK_REMOVE = 100


def _format_fetched_entity(entry: dict[str, Any]) -> dict[str, Any]:
    """Project a raw HA entity-registry extended dict into ha_get_entity's shape.

    Shared by the single-entity get, the bulk get_entries backend, and the
    unique_id resolver so every ha_get_entity response carries the same keys.
    """
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
        "config_entry_id": entry.get("config_entry_id"),
        "unique_id": entry.get("unique_id"),
    }


async def fetch_entity_enrichment_via_component(
    client: Any, entity_ids: list[str]
) -> dict[str, dict[str, Any]] | None:
    """One ``ha_mcp_tools/entity_enrich`` read; ``None`` ⇒ skip enrichment (legacy).

    Returns the component's ``{id: {area, floor, labels, aliases}}`` map (resolved
    area/floor/label NAMES the raw registry entry lacks — it carries ``area_id`` /
    label *ids*) or ``None`` when the component lacks the ``entity_enrich``
    capability, was downgraded (``unknown_command`` → invalidate the cached caps),
    or errored (logged). ``None`` means "no enrichment available", NOT "no such
    entity": ``ha_get_entity``'s registry read already served the base record, so a
    ``None`` here simply leaves the additive fields off — the enrichment is
    strictly additive, so its absence changes nothing else. A
    ``HomeAssistantConnectionError`` (WS down) is not caught here, so it propagates
    to the tool's own error handling. Follows the same caps-gate discipline as
    ``component_devices.fetch_device_via_component``.

    The ids are split into ``_GET_ENTRIES_CHUNK_SIZE`` chunks (the same bound the
    sibling ``config/entity_registry/get_entries`` read uses) so a large bulk
    request cannot produce an over-cap WebSocket frame or stall in the optional
    enrichment step. Enrichment stays all-or-nothing: any chunk failing or drifting
    in shape returns ``None`` (legacy shape), never a partially-enriched response.
    """
    if not entity_ids:
        return None
    caps = await get_component_caps(client)
    if not component_supports(caps, "entity_enrich"):
        return None
    ids = list(entity_ids)
    chunks = [
        ids[i : i + _GET_ENTRIES_CHUNK_SIZE]
        for i in range(0, len(ids), _GET_ENTRIES_CHUNK_SIZE)
    ]
    merged: dict[str, dict[str, Any]] = {}
    try:
        ws = await get_websocket_client(url=client.base_url, token=client.token)
        for chunk in chunks:
            raw = await ws.send_command(WS_ENTITY_ENRICH, entity_ids=chunk)
            result = raw.get("result")
            if not isinstance(result, dict):
                return None
            entities = result.get("entities")
            if not isinstance(entities, dict):
                return None
            merged.update(entities)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; skipped enrichment: %r", WS_ENTITY_ENRICH, exc)
        return None
    return merged


def _merge_entity_enrichment(
    record: dict[str, Any], enrichment: dict[str, Any] | None
) -> None:
    """Additively attach resolved area/floor/label NAMES to an entity record.

    The base record already carries ``area_id`` and label *ids* (``labels``); the
    component join adds the resolved, device-inherited NAMES under non-clobbering
    keys — ``area`` / ``floor`` / ``label_names`` — so nothing existing is
    overwritten. ``aliases`` is already on the base record (same set — the
    component sorts, so ordering may differ — and the base value is authoritative),
    so it is not re-added. A ``None`` / empty enrichment is a no-op: on a capability
    miss the fields are simply absent, leaving the legacy response shape unchanged.
    """
    if not enrichment:
        return
    record["area"] = enrichment.get("area")
    record["floor"] = enrichment.get("floor")
    record["label_names"] = enrichment.get("labels") or []


def _match_registry_by_unique_id(
    entries: list[dict[str, Any]],
    unique_id: str,
    domain: str | None,
    platform: str | None,
) -> list[dict[str, Any]]:
    """Return formatted registry entries matching unique_id (+ optional filters).

    ``domain`` is compared against the entity_id prefix (the registry's unique
    key uses the entity domain); ``platform`` against the entry's platform.
    """
    matches: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("unique_id") != unique_id:
            continue
        entry_domain = (entry.get("entity_id") or "").split(".")[0]
        if domain is not None and entry_domain != domain:
            continue
        if platform is not None and entry.get("platform") != platform:
            continue
        matches.append(_format_fetched_entity(entry))
    return matches


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


def _build_name_visibility_fields(
    message: dict[str, Any],
    updates_made: list[str],
    area_id: str | None,
    name: str | None,
    icon: str | None,
    device_class: str | None,
) -> None:
    """Add basic positioning/appearance fields to the update message."""
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


def _build_state_tag_fields(
    message: dict[str, Any],
    updates_made: list[str],
    enabled: bool | None,
    hidden: bool | None,
    parsed_aliases: list[str] | None,
    parsed_categories: dict[str, str | None] | None,
    final_labels: list[str] | None,
    label_operation: str,
    parsed_labels: list[str] | None,
) -> None:
    """Add enabled/hidden/alias/category/label fields to the update message."""
    if enabled is not None:
        message["disabled_by"] = None if enabled else "user"
        updates_made.append("enabled" if enabled else "disabled")
    if hidden is not None:
        message["hidden_by"] = "user" if hidden else None
        updates_made.append("hidden" if hidden else "visible")
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
            updates_made.append(f"labels removed: {parsed_labels} -> {final_labels}")


def _parse_set_entity_ids(
    entity_id: str | list[str],
) -> tuple[list[str], bool]:
    """Parse entity_id into (entity_ids, is_bulk). Raises on invalid input."""
    if isinstance(entity_id, str):
        return [entity_id], False
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
        return entity_id, len(entity_id) > 1
    else:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"entity_id must be string or list of strings, got {type(entity_id).__name__}",
            )
        )


def _parse_get_entity_ids(
    entity_id: str | list[str],
) -> tuple[list[str], bool, dict[str, Any] | None]:
    """Parse entity_id into (entity_ids, is_bulk, early_response). early_response is non-None for empty list."""
    if isinstance(entity_id, str):
        return [entity_id], False, None
    if not isinstance(entity_id, list):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"entity_id must be string or list of strings, got {type(entity_id).__name__}",
            )
        )
    if not entity_id:
        return (
            [],
            False,
            {
                "success": True,
                "entity_entries": [],
                "count": 0,
                "message": "No entities requested",
            },
        )
    if not all(isinstance(e, str) for e in entity_id):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "All entity_id values must be strings",
            )
        )
    return entity_id, True, None


def _validate_enabled_constraint(
    enabled: bool | None,
    entity_ids: list[str],
) -> None:
    """Block registry-disable on automation and script entities.

    Registry-disabling (enabled=False) removes the entity from the HA
    state machine entirely, making it invisible in the UI and
    unqueryable via state APIs until re-enabled AND the integration is
    reloaded.  For automations and scripts the correct way to
    "disable" them is via their domain services (automation.turn_off /
    script.turn_off) which simply prevent them from running while
    keeping them visible and manageable.
    """
    if enabled is False:
        blocked = [
            eid for eid in entity_ids if eid.split(".")[0] in ("automation", "script")
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


def _parse_string_list_field(
    value: str | list[str] | None,
    field_name: str,
) -> list[str] | None:
    """Parse and validate a string-list field (aliases, labels, etc.)."""
    if value is not None:
        try:
            return parse_string_list_param(value, field_name)
        except ValueError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid {field_name} parameter: {e}",
                )
            )
    return None


def _parse_categories_param(
    categories: dict[str, str | None] | None,
) -> dict[str, str | None] | None:
    """Parse and validate the categories parameter."""
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
        return parsed_cats
    return None


def _parse_options_param(
    options: dict[str, dict[str, Any]] | None,
) -> dict[str, dict[str, Any]] | None:
    """Parse and validate the options parameter."""
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
        return parsed_opts
    return None


def _parse_expose_to_param(
    expose_to: dict[str, bool] | None,
) -> dict[str, bool] | None:
    """Parse and validate the expose_to parameter."""
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
        # Validate assistant names
        invalid_assistants = [a for a in parsed if a not in KNOWN_ASSISTANTS]
        if invalid_assistants:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid assistant(s) in expose_to: {invalid_assistants}. "
                    f"Valid: {KNOWN_ASSISTANTS}",
                )
            )
        # Values are already bool (enforced by the dict[str, bool] annotation)
        return parsed
    return None


class EntityTools:
    """Entity registry tools: get, update, and remove entities."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _get_entity_labels(
        self, entity_id: str
    ) -> tuple[list[str] | None, str | None]:
        """Fetch current labels for an entity. Returns (labels, error_msg)."""
        get_msg: dict[str, Any] = {
            "type": "config/entity_registry/get",
            "entity_id": entity_id,
        }
        result = await self._client.send_websocket_message(get_msg)
        if not result.get("success"):
            return None, _extract_ws_error(result)
        return (result.get("result") or {}).get("labels") or [], None

    async def _resolve_final_labels(
        self,
        entity_id: str,
        parsed_labels: list[str] | None,
        label_operation: str,
    ) -> list[str] | None:
        """Merge or filter labels using the current registry state for add/remove ops."""
        if parsed_labels is None or label_operation not in ("add", "remove"):
            return parsed_labels
        current_labels, error_msg = await self._get_entity_labels(entity_id)
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
            return list(set(current_labels) | set(parsed_labels))
        # remove: use set for O(1) membership check
        labels_to_remove = set(parsed_labels)
        return [lbl for lbl in current_labels if lbl not in labels_to_remove]

    def _validate_entity_rename(
        self,
        entity_id: str,
        new_entity_id: str,
        message: dict[str, Any],
        updates_made: list[str],
    ) -> None:
        """Validate and apply a rename to the update message. Raises on invalid input."""
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
                    suggestions=[f"New entity_id must start with '{current_domain}.'"],
                    context={
                        "entity_id": entity_id,
                        "new_entity_id": new_entity_id,
                    },
                )
            )
        message["new_entity_id"] = new_entity_id
        updates_made.append(f"entity_id -> {new_entity_id}")

    async def _execute_registry_update(
        self,
        entity_id: str,
        message: dict[str, Any],
        updates_made: list[str],
        new_entity_id: str | None,
    ) -> tuple[str, dict[str, Any], bool]:
        """Send entity registry update. Returns (effective_entity_id, entity_entry, has_registry_updates)."""
        has_registry_updates = len(message) > 2  # more than just type + entity_id
        entity_entry: dict[str, Any] = {}

        if has_registry_updates:
            logger.info(
                f"Updating entity registry for {entity_id}: {', '.join(updates_made)}"
            )
            result = await self._client.send_websocket_message(message)

            if not result.get("success"):
                error_msg = _extract_ws_error(result)
                suggestions = ["Verify the entity_id exists using ha_search()"]
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

        return entity_id, entity_entry, has_registry_updates

    async def _apply_options_updates(
        self,
        entity_id: str,
        parsed_options: dict[str, dict[str, Any]] | None,
        entity_entry: dict[str, Any],
        has_registry_updates: bool,
        updates_made: list[str],
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        """Apply per-domain options updates. Returns (entity_entry, options_succeeded).

        Per-domain options updates: HA's WS schema requires `options_domain`
        and `options` to be sent paired one domain per call (the API takes a
        single domain's sub-dict). An agent-supplied {domain: {...}, ...} is
        therefore split into one registry update per domain.
        """
        options_succeeded: dict[str, dict[str, Any]] = {}
        if not parsed_options:
            return entity_entry, options_succeeded

        for opts_domain, opts_sub in parsed_options.items():
            opts_msg: dict[str, Any] = {
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
                "options_domain": opts_domain,
                "options": opts_sub,
            }
            opts_result = await self._client.send_websocket_message(opts_msg)
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
                # the partial-context logic in _build_expose_failure_context.
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
            entity_entry = (opts_result.get("result") or {}).get(
                "entity_entry", entity_entry
            )
            options_succeeded[opts_domain] = opts_sub
            updates_made.append(f"options[{opts_domain}]={opts_sub}")

        return entity_entry, options_succeeded

    async def _apply_device_rename(
        self,
        entity_id: str,
        entity_entry: dict[str, Any],
        new_device_name: str | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """Rename the associated device. Returns (device_rename_result, entity_entry).

        Handle new_device_name — rename the associated device.
        Normalize empty string to None (no-op, don't clear device name).
        """
        if new_device_name is not None and not new_device_name.strip():
            new_device_name = None
        device_rename_result: dict[str, Any] | None = None
        if new_device_name is None:
            return device_rename_result, entity_entry

        # If no registry update was sent, fetch entity_entry to get device_id
        if not entity_entry:
            device_lookup_msg: dict[str, Any] = {
                "type": "config/entity_registry/get",
                "entity_id": entity_id,
            }
            get_result = await self._client.send_websocket_message(device_lookup_msg)
            if get_result.get("success"):
                entity_entry = get_result.get("result") or {}
            else:
                logger.warning(
                    "Entity registry lookup failed for %s: %s",
                    entity_id,
                    _extract_ws_error(get_result),
                )
                device_rename_result = {
                    "warnings": [
                        "Entity registry lookup failed — could not determine device. Retry may succeed."
                    ],
                    "lookup_failed": True,
                }

        device_id = entity_entry.get("device_id") if not device_rename_result else None
        if not device_id:
            # Only fire the "no device" warning when the registry lookup
            # succeeded — otherwise the "lookup failed" warning set above
            # already carries the more accurate signal, and a second
            # "no associated device" claim would be unverified (we don't
            # actually know what the registry says when the lookup failed).
            if device_rename_result is None:
                device_rename_result = {
                    "warnings": [
                        "Entity has no associated device — device rename skipped"
                    ],
                }
            return device_rename_result, entity_entry

        device_msg: dict[str, Any] = {
            "type": "config/device_registry/update",
            "device_id": device_id,
            "name_by_user": new_device_name if new_device_name else None,
        }
        device_result = await self._client.send_websocket_message(device_msg)
        if device_result.get("success"):
            device_rename_result = {"success": True, "device_id": device_id}
        else:
            device_rename_result = {
                "warnings": [
                    f"Entity updated but device rename failed: {_extract_ws_error(device_result)}"
                ],
                "device_id": device_id,
            }
        return device_rename_result, entity_entry

    def _build_expose_failure_context(
        self,
        entity_id: str,
        entity_entry: dict[str, Any],
        succeeded: dict[str, bool],
        failed: dict[str, bool],
        options_succeeded: dict[str, dict[str, Any]],
        device_rename_result: dict[str, Any] | None,
        has_registry_updates: bool,
    ) -> dict[str, Any]:
        """Build error context for an expose_to failure.

        `partial` must reflect every prior mutation across the ha_set_entity
        pipeline: main registry update, per-domain options, device rename, and
        any expose_to batch (e.g. expose_true) that ran before this
        one (expose_false) failed. Anything truthy in those means
        the registry already moved.
        """
        prior_mutation = (
            has_registry_updates
            or bool(options_succeeded)
            or bool(succeeded)
            or bool(device_rename_result and device_rename_result.get("success"))
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
        return context

    async def _apply_expose_to(
        self,
        entity_id: str,
        parsed_expose_to: dict[str, bool] | None,
        entity_entry: dict[str, Any],
        has_registry_updates: bool,
        options_succeeded: dict[str, dict[str, Any]],
        device_rename_result: dict[str, Any] | None,
    ) -> tuple[dict[str, bool] | None, dict[str, Any]]:
        """Apply expose_to changes via separate WebSocket API. Returns (exposure_result, entity_entry)."""
        if not parsed_expose_to:
            return None, entity_entry

        # Group by should_expose value for efficient API calls
        expose_true = [a for a, v in parsed_expose_to.items() if v]
        expose_false = [a for a, v in parsed_expose_to.items() if not v]
        succeeded: dict[str, bool] = {}

        for assistants, should_expose in [(expose_true, True), (expose_false, False)]:
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
            expose_result = await self._client.send_websocket_message(expose_msg)

            if not expose_result.get("success"):
                error_msg = _extract_ws_error(expose_result)
                failed = dict.fromkeys(assistants, should_expose)
                context = self._build_expose_failure_context(
                    entity_id,
                    entity_entry,
                    succeeded,
                    failed,
                    options_succeeded,
                    device_rename_result,
                    has_registry_updates,
                )
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

        exposure_result: dict[str, bool] | None = succeeded if succeeded else None

        # Exposure mutates the registry entry's options, so refetch to return the
        # post-exposure state. This is unconditional: the method returns early on
        # an empty expose_to (line 695) and any exposure failure already raised
        # above, so reaching here always means at least one exposure was applied.
        get_msg: dict[str, Any] = {
            "type": "config/entity_registry/get",
            "entity_id": entity_id,
        }
        get_result = await self._client.send_websocket_message(get_msg)
        if get_result.get("success"):
            entity_entry = get_result.get("result") or {}
        elif entity_entry:
            # The exposure already committed; only the cosmetic post-exposure
            # refresh failed. Keep the pre-exposure snapshot and warn rather
            # than reporting the whole (successful) operation as a failure.
            logger.warning(
                f"Exposure applied to {entity_id} but its registry state could "
                f"not be refreshed: {_extract_ws_error(get_result)}"
            )
        else:
            # No prior-phase snapshot to fall back on -- surface the read
            # failure with the actual WebSocket error.
            raise_tool_error(
                create_error_response(
                    ErrorCode.ENTITY_NOT_FOUND,
                    f"Entity '{entity_id}' could not be read after applying "
                    f"exposure changes: {_extract_ws_error(get_result)}",
                    context={
                        "entity_id": entity_id,
                        "exposure_succeeded": exposure_result,
                        "has_registry_updates": has_registry_updates,
                        "options_succeeded": options_succeeded,
                    },
                    suggestions=[
                        "Verify the entity_id exists using ha_search()",
                        "The entity's exposure settings were likely changed, but its current state could not be confirmed.",
                    ],
                )
            )

        return exposure_result, entity_entry

    def _build_entity_response(
        self,
        entity_id: str,
        original_entity_id: str,
        entity_entry: dict[str, Any],
        updates_made: list[str],
        new_entity_id: str | None,
        exposure_result: dict[str, bool] | None,
        device_rename_result: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Build the final response dict for a single-entity update."""
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
            response_data.setdefault("warnings", []).append(
                "Remember to update any automations, scripts, or dashboards "
                "that reference the old entity_id"
            )

        if exposure_result is not None:
            response_data["exposure"] = exposure_result

        if device_rename_result is not None:
            response_data["device_rename"] = device_rename_result
            # Mark partial when a device rename was requested but didn't complete
            # for an operational reason: WS-call failure (device_id present + warnings)
            # or upstream registry lookup failure (lookup_failed marker). Not partial
            # when the entity simply has no device — that's a no-op, not an incomplete
            # operation.
            if device_rename_result.get("warnings") and (
                device_rename_result.get("device_id")
                or device_rename_result.get("lookup_failed")
            ):
                response_data["partial"] = True

        return response_data

    async def _update_single_entity(
        self,
        entity_id: str,
        area_id: str | None,
        name: str | None,
        icon: str | None,
        enabled: bool | None,
        hidden: bool | None,
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
        """Update a single entity. Orchestrates the phase pipeline."""
        # Phase 1: For add/remove label operations, fetch current labels first
        final_labels = await self._resolve_final_labels(
            entity_id, parsed_labels, label_operation
        )

        # Phase 2: Build update message for entity registry
        message: dict[str, Any] = {
            "type": "config/entity_registry/update",
            "entity_id": entity_id,
        }
        updates_made: list[str] = []
        _build_name_visibility_fields(
            message, updates_made, area_id, name, icon, device_class
        )
        _build_state_tag_fields(
            message,
            updates_made,
            enabled,
            hidden,
            parsed_aliases,
            parsed_categories,
            final_labels,
            label_operation,
            parsed_labels,
        )
        if new_entity_id is not None:
            self._validate_entity_rename(
                entity_id, new_entity_id, message, updates_made
            )
        # expose_to and device_name are appended to updates_made only after their
        # WS phases run (Phases 5-6), so the Phase-4 error context never claims
        # they were applied before they ran.
        has_deferred_work = parsed_expose_to is not None or new_device_name is not None
        if not updates_made and not parsed_options and not has_deferred_work:
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

        # Phase 3: Send entity registry update (covers all fields except expose_to)
        (
            entity_id,
            entity_entry,
            has_registry_updates,
        ) = await self._execute_registry_update(
            entity_id, message, updates_made, new_entity_id
        )

        # Phase 4: Per-domain options
        entity_entry, options_succeeded = await self._apply_options_updates(
            entity_id, parsed_options, entity_entry, has_registry_updates, updates_made
        )

        # Phase 5: Device rename
        device_rename_result, entity_entry = await self._apply_device_rename(
            entity_id, entity_entry, new_device_name
        )
        if new_device_name is not None:
            updates_made.append(f"device_name -> {new_device_name}")

        # Phase 6: Expose to assistants
        exposure_result, entity_entry = await self._apply_expose_to(
            entity_id,
            parsed_expose_to,
            entity_entry,
            has_registry_updates,
            options_succeeded,
            device_rename_result,
        )
        if parsed_expose_to is not None:
            updates_made.append(f"expose_to={parsed_expose_to}")

        # Phase 7: Build response
        return self._build_entity_response(
            entity_id,
            original_entity_id,
            entity_entry,
            updates_made,
            new_entity_id,
            exposure_result,
            device_rename_result,
        )

    async def _bulk_apply_expose(
        self,
        entity_ids: list[str],
        parsed_expose_to: dict[str, bool],
    ) -> str | None:
        """Expose/hide many entities in one homeassistant/expose_entity call per set.

        The command's schema accepts the full entity_ids list, so bulk exposure
        needs at most two WS calls (one for the assistants being turned on, one
        for those being turned off) regardless of entity count — not one per
        entity. Returns None on success, or the error message on the first
        failing set. A failure is batch-level: it applies to every id in the
        list, so the caller maps the returned message onto all of them.
        """
        expose_true = [a for a, v in parsed_expose_to.items() if v]
        expose_false = [a for a, v in parsed_expose_to.items() if not v]
        applied: list[str] = []
        for assistants, should_expose in [(expose_true, True), (expose_false, False)]:
            if not assistants:
                continue
            logger.info(
                f"{'Exposing' if should_expose else 'Hiding'} {len(entity_ids)} "
                f"entities {'to' if should_expose else 'from'} {assistants}"
            )
            result = await self._client.send_websocket_message(
                {
                    "type": "homeassistant/expose_entity",
                    "assistants": assistants,
                    "entity_ids": entity_ids,
                    "should_expose": should_expose,
                }
            )
            if not result.get("success"):
                error = _extract_ws_error(result)
                if applied:
                    # The earlier assistant group already changed — say so, or
                    # the all-failed report implies no exposure was touched.
                    error = (
                        f"{error} (note: expose changes for "
                        f"{', '.join(applied)} were already applied before "
                        "this failure)"
                    )
                return error
            applied.extend(assistants)
        return None

    async def _bulk_registry_phase(
        self,
        entity_ids: list[str],
        parsed_categories: dict[str, str | None] | None,
        parsed_labels: list[str] | None,
        label_operation: str,
    ) -> tuple[
        dict[str, dict[str, Any] | None],
        dict[str, list[str]],
        list[dict[str, Any]],
        list[str],
    ]:
        """Per-entity label/category updates (no expose).

        HA's entity_registry/update is single-entity, so this stays a
        per-entity fan-out. Returns (entry_by_id, updates_by_id, failed,
        eligible_ids); eligible_ids are the ids whose registry update
        succeeded (all ids when there is no registry work — expose-only bulk).
        """
        entry_by_id: dict[str, dict[str, Any] | None] = {}
        updates_by_id: dict[str, list[str]] = {}
        failed: list[dict[str, Any]] = []

        if parsed_labels is None and parsed_categories is None:
            for eid in entity_ids:
                updates_by_id[eid] = []
            return entry_by_id, updates_by_id, failed, list(entity_ids)

        eligible_ids: list[str] = []
        results = await asyncio.gather(
            *[
                self._update_single_entity(
                    eid,
                    None,  # area_id not supported in bulk
                    None,  # name not supported in bulk
                    None,  # icon not supported in bulk
                    None,  # enabled not supported in bulk
                    None,  # hidden not supported in bulk
                    None,  # aliases not supported in bulk
                    parsed_categories,
                    parsed_labels,
                    label_operation,
                    None,  # expose_to batched separately below
                )
                for eid in entity_ids
            ],
            return_exceptions=True,
        )
        for eid, result in zip(entity_ids, results, strict=True):
            if isinstance(result, BaseException) and not isinstance(result, Exception):
                # Never swallow cancellation/shutdown into per-entity errors.
                raise result
            if isinstance(result, BaseException):
                error_msg = (
                    extract_tool_error_message(result)
                    if isinstance(result, ToolError)
                    else str(result)
                )
                failed.append({"entity_id": eid, "error": error_msg})
            else:
                # _update_single_entity returns success-shape or raises
                # ToolError (caught above as BaseException).
                entry_by_id[eid] = result.get("entity_entry")
                updates_by_id[eid] = list(result.get("updates") or [])
                eligible_ids.append(eid)
        return entry_by_id, updates_by_id, failed, eligible_ids

    async def _bulk_expose_phase(
        self,
        eligible_ids: list[str],
        parsed_expose_to: dict[str, bool],
        entry_by_id: dict[str, dict[str, Any] | None],
        updates_by_id: dict[str, list[str]],
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Batch-expose eligible_ids, then refetch their post-exposure state.

        Returns (still_eligible_ids, newly_failed). A batched-expose failure
        is reported against every eligible id (the command acts on the whole
        list atomically) — matching the old per-entity path where each entity
        raised on its own expose failure — and clears eligibility. On success,
        options mutate, so a single get_entries refetch refreshes each
        entity_entry; a refetch failure keeps the pre-exposure snapshot since
        the exposure already committed.
        """
        expose_error = await self._bulk_apply_expose(eligible_ids, parsed_expose_to)
        if expose_error is not None:
            failed = [
                {"entity_id": eid, "error": f"Exposure failed: {expose_error}"}
                for eid in eligible_ids
            ]
            return [], failed

        for eid in eligible_ids:
            updates_by_id[eid].append(f"expose_to={parsed_expose_to}")
        refetched_raw, refetch_errors = await self._get_entries_raw(eligible_ids)
        for eid in eligible_ids:
            if eid in refetched_raw:
                entry_by_id[eid] = _format_entity_entry(refetched_raw[eid])
        # An id whose refetch failed AND that has no earlier registry snapshot
        # (expose-only bulk never populates one) would otherwise be reported
        # as a success row with entity_entry=None — e.g. a typo'd or deleted
        # entity. The old per-entity path raised for exactly this case.
        still_eligible: list[str] = []
        newly_failed: list[dict[str, Any]] = []
        for eid in eligible_ids:
            if eid in refetch_errors and entry_by_id.get(eid) is None:
                newly_failed.append(
                    {
                        "entity_id": eid,
                        "error": (
                            "Exposure command was sent, but the entity could "
                            "not be verified in the registry: "
                            f"{refetch_errors[eid]}"
                        ),
                    }
                )
            else:
                still_eligible.append(eid)
        if refetch_errors:
            logger.warning(
                "Bulk exposure applied but post-exposure refresh failed "
                "for %d id(s); %d had a pre-exposure snapshot to return",
                len(refetch_errors),
                len(refetch_errors) - len(newly_failed),
            )
        return still_eligible, newly_failed

    async def _bulk_update_entities(
        self,
        entity_ids: list[str],
        parsed_categories: dict[str, str | None] | None,
        parsed_labels: list[str] | None,
        label_operation: str,
        parsed_expose_to: dict[str, bool] | None,
    ) -> dict[str, Any]:
        """Apply bulk label/category/expose updates across many entities.

        Registry work runs per-entity (_bulk_registry_phase); only the
        expose_to phase is batched into one homeassistant/expose_entity call
        per assistant-set (_bulk_expose_phase).
        """
        logger.info(f"Bulk updating {len(entity_ids)} entities")
        if (
            parsed_labels is None
            and parsed_categories is None
            and parsed_expose_to is None
        ):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "No updates specified",
                    suggestions=[
                        "Provide at least one of: labels, categories, or expose_to"
                    ],
                )
            )

        (
            entry_by_id,
            updates_by_id,
            failed,
            eligible_ids,
        ) = await self._bulk_registry_phase(
            entity_ids, parsed_categories, parsed_labels, label_operation
        )

        if parsed_expose_to is not None and eligible_ids:
            eligible_ids, expose_failed = await self._bulk_expose_phase(
                eligible_ids, parsed_expose_to, entry_by_id, updates_by_id
            )
            failed.extend(expose_failed)

        succeeded_ids = set(eligible_ids)
        succeeded_list = [
            {
                "entity_id": eid,
                "entity_entry": entry_by_id.get(eid),
                "updates": updates_by_id.get(eid, []),
            }
            for eid in entity_ids
            if eid in succeeded_ids
        ]

        response: dict[str, Any] = {
            "success": len(failed) == 0,
            "total": len(entity_ids),
            "succeeded_count": len(succeeded_list),
            "failed_count": len(failed),
            "succeeded": succeeded_list,
        }
        if failed:
            response["failed"] = failed
            response["partial"] = len(succeeded_list) > 0
        return response

    async def _fetch_entity(self, eid: str) -> dict[str, Any]:
        """Fetch a single entity from the registry."""
        message: dict[str, Any] = {
            "type": "config/entity_registry/get",
            "entity_id": eid,
        }
        result = await self._client.send_websocket_message(message)

        if not result.get("success"):
            raise ValueError(_extract_ws_error(result))

        return _format_fetched_entity(result.get("result") or {})

    async def _get_entries_raw(
        self, entity_ids: list[str]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        """Bulk-fetch registry entries via chunked config/entity_registry/get_entries.

        Returns ``(raw_entries, errors)``: ``raw_entries`` maps a found
        entity_id to HA's raw extended registry dict; ``errors`` maps a failed
        entity_id to a message. A ``null`` in HA's entries map (id not in the
        registry) and a chunk-level WS failure both land in ``errors`` — this
        reproduces the per-id not-found contract of the old one-get-per-id
        fan-out, where every id got its own error. Callers project the raw
        entries through whichever formatter their response shape needs.
        """
        raw: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        if not entity_ids:
            return raw, errors

        chunks = [
            entity_ids[i : i + _GET_ENTRIES_CHUNK_SIZE]
            for i in range(0, len(entity_ids), _GET_ENTRIES_CHUNK_SIZE)
        ]
        responses = await asyncio.gather(
            *(
                self._client.send_websocket_message(
                    {
                        "type": "config/entity_registry/get_entries",
                        "entity_ids": chunk,
                    }
                )
                for chunk in chunks
            ),
            return_exceptions=True,
        )
        for chunk, resp in zip(chunks, responses, strict=True):
            if isinstance(resp, BaseException) and not isinstance(resp, Exception):
                # Never swallow cancellation/shutdown into per-entity errors.
                raise resp
            if isinstance(resp, dict) and resp.get("success"):
                result_map = resp.get("result") or {}
                for eid in chunk:
                    entry = result_map.get(eid)
                    if entry is None:
                        errors[eid] = "Entity not found"
                    else:
                        raw[eid] = entry
                continue
            # Whole-chunk failure (WS raised, or success=False): every id in
            # the chunk failed, mirroring the old per-id fan-out behaviour.
            msg = _extract_ws_error(resp) if isinstance(resp, dict) else str(resp)
            for eid in chunk:
                errors[eid] = msg
        return raw, errors

    async def _resolve_by_unique_id(
        self,
        unique_id: str,
        domain: str | None,
        platform: str | None,
    ) -> dict[str, Any]:
        """Resolve a stable unique_id to its entity_id(s) via one registry list.

        The registry's unique key is (domain, platform, unique_id), so the same
        unique_id can appear under multiple platforms — every match is returned
        with a ``matches`` count so callers can disambiguate. ``domain`` /
        ``platform`` narrow the match set when provided.
        """
        list_resp = await self._client.send_websocket_message(
            {"type": "config/entity_registry/list"}
        )
        if not list_resp.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to list entity registry: {_extract_ws_error(list_resp)}",
                    context={"unique_id": unique_id},
                    suggestions=["Check Home Assistant connection and retry"],
                )
            )

        matches = _match_registry_by_unique_id(
            list_resp.get("result") or [], unique_id, domain, platform
        )

        if not matches:
            filters = {"unique_id": unique_id}
            if domain is not None:
                filters["domain"] = domain
            if platform is not None:
                filters["platform"] = platform
            raise_tool_error(
                create_error_response(
                    ErrorCode.ENTITY_NOT_FOUND,
                    f"No entity found with unique_id '{unique_id}'"
                    + (
                        f" (domain={domain}, platform={platform})"
                        if domain is not None or platform is not None
                        else ""
                    ),
                    context=filters,
                    suggestions=[
                        "unique_id must match exactly — it is the integration's "
                        + "internal id, not the entity_id",
                        "Drop the domain/platform filters if you set them",
                        "Use ha_search() to browse entities and their platforms",
                    ],
                )
            )

        # config/entity_registry/list carries as_partial_dict, which omits
        # aliases and the device_class override — those come back as their
        # defaults ([] / None) in resolver mode. Callers needing them can
        # re-query ha_get_entity(entity_id=...) with a resolved id.
        response: dict[str, Any] = {
            "success": True,
            "unique_id": unique_id,
            "matches": len(matches),
            "entity_entries": matches,
        }
        if domain is not None:
            response["domain"] = domain
        if platform is not None:
            response["platform"] = platform
        return response

    @tool(
        name="ha_set_entity",
        tags={"Entity Registry"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "title": "Set Entity",
        },
    )
    @with_auto_backup(
        domain="entity",
        # Bulk calls (entity_id is a list) intentionally skip the
        # decorator path: we'd otherwise snapshot only the first entity
        # and silently leave the rest of the list un-protected. The
        # capture pipeline treats "" as "no entity" and no-ops.
        id_fn=lambda kw: (
            ""
            if isinstance(kw.get("entity_id"), list)
            else str(kw.get("entity_id") or "")
        ),
    )
    @log_tool_usage
    async def ha_set_entity(
        self,
        entity_id: Annotated[
            str | list[str],
            JSON_STRING_COERCION,
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
            dict[str, dict[str, Any]] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Per-domain entity registry options (e.g. sensor 'display_precision', "
                    "weather 'forecast_type'). Pass a dict mapping domain to a sub-dict, "
                    'e.g. {"sensor": {"display_precision": 2}}. '
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
            bool | None,
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
            bool | None,
            Field(
                description="True to hide the entity from UI, False to show it. Single entity only.",
                default=None,
            ),
        ] = None,
        aliases: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description="List of voice assistant aliases for the entity (replaces existing aliases). Single entity only.",
                default=None,
            ),
        ] = None,
        categories: Annotated[
            dict[str, str | None] | None,
            JSON_STRING_COERCION,
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
            JSON_STRING_COERCION,
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
            dict[str, bool] | None,
            JSON_STRING_COERCION,
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

        Use ha_search() or ha_get_device() to find entity IDs.
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
            entity_ids, is_bulk = _parse_set_entity_ids(entity_id)

            # Per-element empty/whitespace check — the list-empty check above
            # rejects ``[]`` but not ``[""]``; without this guard, an empty
            # entity_id would propagate to the entity-registry update WS call
            # and surface as a misleading HA "entity not found".
            for eid in entity_ids:
                validate_identifier_not_empty(eid, "entity_id")

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

            _validate_enabled_constraint(enabled, entity_ids)

            parsed_aliases = _parse_string_list_field(aliases, "aliases")
            parsed_categories = _parse_categories_param(categories)
            parsed_labels = _parse_string_list_field(labels, "labels")
            parsed_options = _parse_options_param(options)
            parsed_expose_to = _parse_expose_to_param(expose_to)

            # Single entity case
            if not is_bulk:
                return await self._update_single_entity(
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

            # Bulk case
            return await self._bulk_update_entities(
                entity_ids,
                parsed_categories,
                parsed_labels,
                label_operation,
                parsed_expose_to,
            )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error updating entity: {e}")
            exception_to_structured_error(e, context={"entity_id": entity_id})
            return None  # unreachable: exception_to_structured_error always raises

    @tool(
        name="ha_get_entity",
        tags={"Entity Registry"},
        annotations={
            "openWorldHint": False,
            "readOnlyHint": True,
            "idempotentHint": True,
            "title": "Get Entity",
        },
    )
    @log_tool_usage
    async def ha_get_entity(
        self,
        entity_id: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description="Entity ID or list of entity IDs to retrieve (e.g., 'sensor.temperature' or ['light.living_room', 'switch.porch']). Mutually exclusive with unique_id.",
                default=None,
            ),
        ] = None,
        unique_id: Annotated[
            str | None,
            Field(
                description=(
                    "Resolve a stable integration unique_id to its entity_id(s) "
                    "(entity_id is mutable, unique_id is not). Mutually exclusive "
                    "with entity_id. Optionally narrow with domain/platform."
                ),
                default=None,
            ),
        ] = None,
        domain: Annotated[
            str | None,
            Field(
                description="Resolver filter (unique_id mode only): restrict matches to this entity domain, e.g. 'sensor'.",
                default=None,
            ),
        ] = None,
        platform: Annotated[
            str | None,
            Field(
                description="Resolver filter (unique_id mode only): restrict matches to this integration platform, e.g. 'hue'.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get entity registry information for one or more entities.

        Returns detailed entity registry metadata including area assignment,
        custom name/icon, enabled/hidden state, aliases, labels, and more.

        RESOLVER MODE:
        Pass unique_id (instead of entity_id) to resolve a stable integration
        unique_id to its entity_id(s). Since the registry's unique key is
        (domain, platform, unique_id), the same unique_id can match multiple
        platforms — all matches are returned in entity_entries with a `matches`
        count. Narrow with domain/platform. Resolver reads as_partial_dict, so
        aliases and the device_class override come back as defaults ([]/null).

        RELATED TOOLS:
        - ha_set_entity(): Modify entity properties (area, name, icon, enabled, hidden, aliases)
        - ha_get_state(): Get current state/attributes (on/off, temperature, etc.)
        - ha_search(): Find entities by name, domain, or area

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
        - config_entry_id: Parent config entry's ID (null for YAML-only
          entities). When non-null — e.g. for UI-created template/group/
          utility_meter/derivative/... helpers — pass it to
          ``ha_get_integration(entry_id=..., include_options=True)`` to read the
          helper's current config (template body, group members, etc.) without
          scanning a domain list.
        - unique_id: Integration's unique identifier

        Resolved-name enrichment (present only when the ha_mcp_tools component
        advertises it; otherwise these keys are absent):
        - area: Assigned area NAME (device-inherited when the entity has none;
          resolves area_id above)
        - floor: Floor NAME of the assigned area
        - label_names: Assigned label NAMES (resolves the label ids in labels)
        Resolved label names live under label_names HERE (this tool's base
        `labels` already carries the label ids); ha_search result_fields and
        ha_get_entity_exposure instead emit the resolved names under `labels`.
        """
        try:
            # Resolver mode (unique_id) is mutually exclusive with entity_id.
            if unique_id is not None:
                if entity_id is not None:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "Provide exactly one of entity_id or unique_id, not both.",
                            suggestions=[
                                "Pass unique_id alone to resolve it to entity_id(s)",
                                "Pass entity_id alone to look an entity up directly",
                            ],
                        )
                    )
                logger.info(f"Resolving unique_id '{unique_id}' to entity_id(s)")
                return await self._resolve_by_unique_id(unique_id, domain, platform)

            if entity_id is None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        "Provide entity_id (an id or list) or unique_id.",
                        suggestions=[
                            "ha_get_entity('sensor.temperature')",
                            "ha_get_entity(unique_id='abc123') to resolve a unique_id",
                        ],
                    )
                )
            # domain/platform are resolver-only filters (unique_id is None here).
            if domain is not None or platform is not None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "domain/platform are resolver filters — they only apply "
                        "when unique_id is provided.",
                        suggestions=[
                            "Drop domain/platform, or pass unique_id to use them",
                        ],
                    )
                )

            return await self._get_by_entity_id(entity_id)

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting entity: {e}")
            exception_to_structured_error(
                e,
                context={"entity_id": entity_id},
            )
            return None  # unreachable: exception_to_structured_error always raises

    async def _get_single_entity(self, eid: str) -> dict[str, Any]:
        """Look up one entity registry entry, with additive enrichment."""
        logger.info(f"Getting entity registry entry for {eid}")
        try:
            result = await self._fetch_entity(eid)
        except ValueError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Entity not found: {e}",
                    context={"entity_id": eid},
                    suggestions=[
                        "Use ha_search() to find valid entity IDs",
                        "Check the entity_id spelling and format (e.g., 'sensor.temperature')",
                    ],
                )
            )
        # Additive area/floor/label-name enrichment via the component when it
        # advertises entity_enrich; a capability miss leaves the fields off
        # (legacy shape unchanged).
        enriched = await fetch_entity_enrichment_via_component(self._client, [eid])
        if enriched is not None:
            _merge_entity_enrichment(result, enriched.get(eid))
        return {
            "success": True,
            "entity_id": eid,
            "entity_entry": result,
        }

    async def _get_by_entity_id(self, entity_id: str | list[str]) -> dict[str, Any]:
        """Look up entities by entity_id (single or bulk list)."""
        entity_ids, is_bulk, early_response = _parse_get_entity_ids(entity_id)
        if early_response is not None:
            return early_response

        if not is_bulk:
            return await self._get_single_entity(entity_ids[0])

        # Bulk case - fetch all entities in one chunked get_entries call
        # (native HA bulk command) instead of one registry get per id.
        logger.info(f"Getting entity registry entries for {len(entity_ids)} entities")
        raw_map, error_map = await self._get_entries_raw(entity_ids)

        # Iterate the requested ids (not the map) to preserve request order and
        # duplicate handling; each id is classified found/error exactly as the
        # old per-id fan-out did.
        entity_entries: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for eid in entity_ids:
            raw = raw_map.get(eid)
            if raw is not None:
                entity_entries.append(_format_fetched_entity(raw))
            else:
                errors.append(
                    {
                        "entity_id": eid,
                        "error": error_map.get(eid, "Entity not found"),
                    }
                )

        # Additive area/floor/label-name enrichment for the found entities via the
        # component's entity_enrich capability; missing ⇒ fields simply absent.
        found_ids: list[str] = [
            e["entity_id"]
            for e in entity_entries
            if isinstance(e.get("entity_id"), str)
        ]
        enriched = await fetch_entity_enrichment_via_component(self._client, found_ids)
        if enriched is not None:
            for entry in entity_entries:
                entry_id = entry.get("entity_id")
                if isinstance(entry_id, str):
                    _merge_entity_enrichment(entry, enriched.get(entry_id))

        response: dict[str, Any] = {
            "success": True,
            "count": len(entity_entries),
            "entity_entries": entity_entries,
        }
        if errors:
            response["errors"] = errors
            response["suggestions"] = [
                "Use ha_search() to find valid entity IDs for failed lookups"
            ]
        return response

    async def _bulk_remove_entities(self, entity_ids: list[str]) -> dict[str, Any]:
        """Remove many entities sequentially, classifying each outcome.

        Registry writes are NOT parallelized (per the removal WS command's
        write semantics). Each id lands in exactly one bucket: ``removed``,
        ``skipped`` (not-found is idempotent success, not an error), or
        ``errors`` (any other failure, carrying code + message).
        """
        if not isinstance(entity_ids, list):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"entity_id must be a string or list of strings, got "
                    f"{type(entity_ids).__name__}",
                )
            )
        if not entity_ids:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "entity_id list cannot be empty",
                )
            )
        if not all(isinstance(e, str) for e in entity_ids):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "All entity_id values must be strings",
                )
            )
        if len(entity_ids) > _MAX_BULK_REMOVE:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Too many entity IDs: {len(entity_ids)} exceeds maximum "
                    f"of {_MAX_BULK_REMOVE}",
                )
            )
        for eid in entity_ids:
            validate_identifier_not_empty(
                eid,
                "entity_id",
                suggestions=["Use ha_search() to find valid entity IDs"],
            )

        logger.info(f"Bulk removing {len(entity_ids)} entities")
        removed: list[str] = []
        skipped: list[str] = []
        errors: list[dict[str, str]] = []
        for eid in entity_ids:
            result = await self._client.send_websocket_message(
                {"type": "config/entity_registry/remove", "entity_id": eid}
            )
            if result.get("success"):
                removed.append(eid)
                continue
            error_msg = _extract_ws_error(result)
            # Same not-found detection as the single path — HA surfaces the
            # error as a plain string; already-absent is idempotent success.
            if "not found" in error_msg.lower():
                skipped.append(eid)
            else:
                errors.append(
                    {
                        "entity_id": eid,
                        "code": ErrorCode.SERVICE_CALL_FAILED.value,
                        "message": error_msg,
                    }
                )

        return {
            "success": len(errors) == 0,
            "total": len(entity_ids),
            "removed": removed,
            "skipped": skipped,
            "errors": errors,
        }

    @tool(
        name="ha_remove_entity",
        tags={"Entity Registry"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "title": "Remove Entity",
        },
    )
    # Single-entity removal is snapshotted via id_param. A bulk (list) call
    # stringifies to a non-matching target, so its pre-write snapshot is a
    # best-effort no-op — bulk removal is not individually backed up (the
    # decorator captures one entity per call).
    @with_auto_backup(domain="entity", id_param="entity_id")
    @log_tool_usage
    async def ha_remove_entity(
        self,
        entity_id: Annotated[
            str | list[str],
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Entity ID, or a list of entity IDs, to remove from the "
                    "entity registry (e.g., 'sensor.old_temperature'). "
                    "Permanently removes the registration(s)."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Remove one or more entities from the Home Assistant entity registry.

        Permanently removes the entity registration from Home Assistant.
        The entity will no longer appear in the UI or be available to automations.

        WARNING: This permanently removes the entity registration.
        - Use only for orphaned or stale entity entries
        - If the underlying device or integration is still active, the entity
          may be re-added automatically on the next HA restart or reload
        - This action cannot be undone without restoring from backup

        BULK MODE:
        Pass a list of entity IDs to remove up to 100 at once — handy for
        clearing the restored=true orphans an integration leaves behind after
        its filters change. Removals run sequentially and return:
          {removed: [...], skipped: [...], errors: [{entity_id, code, message}]}
        where skipped = ids already absent (not-found is idempotent, not an
        error). Bulk mode is NOT auto-backed-up (the snapshot is single-entity);
        single-id removal still is.

        EXAMPLES:
        - Remove orphaned sensor: ha_remove_entity("sensor.old_temperature")
        - Remove stale helper entry: ha_remove_entity("input_boolean.deleted_helper")
        - Bulk cleanup: ha_remove_entity(["sensor.orphan_1", "sensor.orphan_2"])

        NOTE: For most use cases, consider disabling instead:
        ha_set_entity(entity_id="sensor.old", enabled=False)

        RELATED TOOLS:
        - ha_search: Find entities to verify the entity_id before removing
        - ha_get_entity: Check entity details before removal
        """
        try:
            # List mode: bulk removal with a per-id classification envelope.
            if not isinstance(entity_id, str):
                return await self._bulk_remove_entities(entity_id)

            # Empty/whitespace entity_id would reach the registry-remove WS
            # command and surface as a misleading HA "entity not found".
            validate_identifier_not_empty(
                entity_id,
                "entity_id",
                suggestions=[
                    "Use ha_search() to find valid entity IDs",
                ],
            )
            result = await self._client.send_websocket_message(
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
                                "Use ha_search() to find valid entity IDs",
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
            return None  # unreachable: exception_to_structured_error always raises


def register_entity_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register entity management tools with the MCP server."""
    register_tool_methods(mcp, EntityTools(client))
