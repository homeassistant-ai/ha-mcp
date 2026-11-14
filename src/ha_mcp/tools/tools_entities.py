"""
Entity management tools for Home Assistant MCP server.

This module provides tools for managing entity lifecycle and properties
via the Home Assistant entity registry API.
"""

import asyncio
import logging
from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..client.rest_client import (
    _NO_ANSWER_ERRORS,
    HomeAssistantCommandError,
    HomeAssistantError,
)
from ..client.websocket_client import get_websocket_client
from ..errors import (
    ErrorCode,
    create_entity_not_found_error,
    create_error_response,
)
from ..utils.registry_update_lock import registry_update_locks
from .auto_backup import with_auto_backup
from .coercion import JSON_STRING_COERCION
from .component_api import (
    COMPONENT_VIA_HACS,
    DEVICE_REGISTRY_CHILD_SEMANTICS,
    ENTITY_ENRICH_FULL_NAME,
    ComponentDiscoveryError,
    component_serves,
    component_supports,
    component_unasked_warning,
    component_unusable_warning,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .config_helpers.registry import validate_registry_ids
from .entity_expose_batch import (
    apply_bulk_expose,
    changed_entity_ids,
    expose_targets,
    refresh_warning,
    reject_unknown_assistants,
)
from .entity_exposure import (
    get_entries_or_unregistered,
    unregistered_lookup,
    unregistered_row,
)
from .entity_param_parsing import (
    _parse_aliases_param,
    _parse_categories_param,
    _parse_expose_to_param,
    _parse_options_param,
    _parse_string_list_field,
)
from .entity_registry_reads import (
    GET_ENTRIES_CHUNK_SIZE,
    NotInRegistry,
    _extract_ws_error,
    get_entries_raw,
    resolve_by_unique_id,
)
from .entity_registry_wide import (
    get_registry_wide,
    is_registry_wide_call,
    set_registry_wide,
)
from .entity_removal import remove_registry_entries, remove_registry_entry
from .entity_update_fields import (
    build_name_visibility_fields,
    build_state_tag_fields,
    ensure_use_device_name_allowed,
    reject_name_with_use_device_name,
)
from .helpers import (
    WHITESPACE_CLEARS_NOTE,
    clearable_value,
    exception_to_structured_error,
    extract_tool_error_message,
    log_tool_usage,
    raise_core_refusal,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .tool_hints import read_only_hints, write_hints

logger = logging.getLogger(__name__)

# The ha_mcp_tools/entity_enrich WS command: resolved area/floor/label NAMES,
# aliases and the composed name per entity_id, byte-shaped like the component's
# registry join. Named once so the routing helper and its tests stay in lockstep.
WS_ENTITY_ENRICH = "ha_mcp_tools/entity_enrich"


# Max entity IDs accepted by the bulk-removal path of ha_remove_entity. Mirrors
# the cap the other bulk tools use (ha_get_state's _get_bulk_entity_states).
_MAX_BULK_REMOVE = 100

# What ha_get_entity's rows lack without the component's entity_enrich.
_WITHOUT_ENRICHMENT = "the entries come without area, floor, label_names and full_name"

_CLEAR_DEVICE_NAME_HINT = (
    "new_device_name cannot clear the device name; use ha_set_device(name='') "
    "instead, or name=' ' if your client cannot send an empty string"
)


async def fetch_entity_enrichment_via_component(
    client: Any, entity_ids: list[str]
) -> dict[str, dict[str, Any]] | None:
    """The component's ``ha_mcp_tools/entity_enrich`` map for the ids.

    Returns ``{id: {area, floor, labels, aliases, full_name}}``: resolved
    area/floor/label NAMES the raw registry entry lacks (it carries
    ``area_id`` / label *ids*) and, with ``entity_enrich_full_name``, the name
    Home Assistant shows. ``None`` when the component does not advertise the
    command or no longer knows it (``unknown_command`` → invalidate the cached
    caps), or when the command fails, gets no answer, loses its connection or
    answers with a malformed result: the rows then stay as Core serves them.

    The ids are split into ``GET_ENTRIES_CHUNK_SIZE`` chunks (the same bound the
    sibling ``config/entity_registry/get_entries`` read uses) so a large bulk
    request cannot produce an over-cap WebSocket frame.
    """
    caps = await get_component_caps(client)
    if not (
        component_supports(caps, "entity_enrich")
        and component_supports(caps, DEVICE_REGISTRY_CHILD_SEMANTICS)
    ):
        return None
    merged: dict[str, dict[str, Any]] = {}
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        for i in range(0, len(entity_ids), GET_ENTRIES_CHUNK_SIZE):
            chunk = entity_ids[i : i + GET_ENTRIES_CHUNK_SIZE]
            raw = await ws.send_command(WS_ENTITY_ENRICH, entity_ids=chunk)
            result = raw.get("result")
            entities = result.get("entities") if isinstance(result, dict) else None
            if not isinstance(entities, dict):
                return None
            merged.update(entities)
    except (HomeAssistantCommandError, *_NO_ANSWER_ERRORS) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; skipped enrichment: %r", WS_ENTITY_ENRICH, exc)
        return None
    return merged


def _merge_entity_enrichment(
    record: dict[str, Any], enrichment: dict[str, Any] | None, with_full_name: bool
) -> None:
    """Attach the component's resolved names without overwriting a base key.

    Adds ``area`` / ``floor`` / ``label_names`` (device-inherited NAMES; the base
    record carries ``area_id`` and label ids) and, with ``with_full_name``,
    ``full_name``, the name Home Assistant shows. ``aliases`` is not re-added:
    the base record carries the same set (the component sorts it) and is
    authoritative. A ``None`` / empty enrichment is a no-op.
    """
    if not enrichment:
        return
    record["area"] = enrichment.get("area")
    record["floor"] = enrichment.get("floor")
    record["label_names"] = enrichment.get("labels") or []
    if with_full_name:
        record["full_name"] = enrichment["full_name"]


def _single_entity_target(kw: dict[str, Any]) -> str:
    """The entity a call backs up; a bulk call (a longer list) backs up none.

    Snapshotting only the first entity of a list would leave the rest without
    a backup while looking covered. The capture treats "" as "no entity".
    """
    entity_id = kw.get("entity_id")
    if isinstance(entity_id, list):
        return str(entity_id[0]) if len(entity_id) == 1 else ""
    return str(entity_id or "")


def _expose_update(parsed_expose_to: dict[str, bool], unchanged: bool) -> str:
    """The updates entry for expose_to, saying so when Core changed nothing."""
    if unchanged:
        return f"expose_to={parsed_expose_to} (already set, nothing changed)"
    return f"expose_to={parsed_expose_to}"


def _parse_set_entity_ids(
    entity_id: str | list[str] | None,
) -> tuple[list[str], bool]:
    """Parse entity_id into (entity_ids, is_bulk). Raises on invalid input."""
    if isinstance(entity_id, str):
        return [entity_id], False
    if not isinstance(entity_id, list):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"entity_id must be string or list of strings, got {type(entity_id).__name__}",
            )
        )
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

    For automations, use ha_config_set_automation(entity_id=..., enabled=...)
    to control their runtime enabled state.  For scripts,
    ha_config_set_script(script_id=..., run="stop") only stops a currently
    running execution; it does not disable the script, and Home Assistant
    has no script runtime enable/disable service.
    """
    if enabled is False:
        blocked = [
            eid for eid in entity_ids if eid.split(".")[0] in ("automation", "script")
        ]
        if blocked:
            _domain = blocked[0].split(".")[0]
            if _domain == "script":
                message = (
                    "Cannot registry-disable script entities with "
                    "ha_set_entity(enabled=False). Use ha_config_set_script("
                    f"script_id='{blocked[0]}', run='stop') only to stop a "
                    "currently running execution; it does not disable the script. "
                    "Home Assistant has no script runtime enable/disable service."
                )
                suggestions = [
                    "Use ha_config_set_script(script_id=..., run='stop') only to "
                    + "stop a currently running execution; it does not disable the script",
                    "Home Assistant has no script runtime enable/disable service",
                ]
            else:
                message = (
                    f"Cannot registry-disable {_domain} entities with "
                    "ha_set_entity(enabled=False). Use "
                    f"ha_config_set_automation(entity_id='{blocked[0]}', "
                    "enabled=False) instead."
                )
                suggestions = [
                    "Use ha_config_set_automation(entity_id=..., enabled=...) to "
                    + "disable the automation",
                    "Use ha_config_set_automation(entity_id=..., enabled=True) to "
                    + "re-enable it later",
                ]
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message,
                    suggestions=suggestions,
                )
            )


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

    async def _get_entity_aliases(self, entity_id: str) -> list[str | None]:
        """Fetch the entity's current registry aliases (``null`` = own name)."""
        get_msg: dict[str, Any] = {
            "type": "config/entity_registry/get",
            "entity_id": entity_id,
        }
        result = await self._client.send_websocket_message(get_msg)
        if not result.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to get current aliases for {entity_id}: "
                    f"{_extract_ws_error(result)}",
                    context={"entity_id": entity_id},
                )
            )
        aliases: list[str | None] = (result.get("result") or {}).get("aliases") or []
        return aliases

    async def _resolve_final_aliases(
        self,
        entity_id: str,
        parsed_aliases: list[str | None] | None,
        use_entity_name_alias: bool | None,
    ) -> tuple[list[str | None] | None, str | None]:
        """Apply the own-name (``null``) alias switch; returns (aliases, note).

        With no explicit switch, the registry's existing ``null`` survives a
        string-only write — HA's Voice settings dialog does the same (#2495).
        """
        if use_entity_name_alias is None:
            if parsed_aliases is None or None in parsed_aliases:
                return parsed_aliases, None
            if None in await self._get_entity_aliases(entity_id):
                return [None, *parsed_aliases], "entity-name alias kept"
            return parsed_aliases, None
        source = (
            parsed_aliases
            if parsed_aliases is not None
            else await self._get_entity_aliases(entity_id)
        )
        strings: list[str | None] = [a for a in source if a is not None]
        if use_entity_name_alias:
            return [None, *strings], "entity-name alias on"
        return strings, "entity-name alias off"

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

    async def _automatic_entity_id(self, entity_id: str) -> str:
        """The entity_id Core would generate for the entry now (the UI's restore)."""
        result = await self._client.send_websocket_message(
            {
                "type": "config/entity_registry/get_automatic_entity_ids",
                "entity_ids": [entity_id],
            }
        )
        what = f"No automatic entity_id for {entity_id}"
        context = {"entity_id": entity_id}
        if not result.get("success"):
            raise_core_refusal(what, result, context)
        # Core lowercases the ids it is sent and keys its reply so.
        automatic = (result.get("result") or {}).get(entity_id.lower())
        if not automatic:
            raise_tool_error(
                create_error_response(
                    ErrorCode.ENTITY_NOT_FOUND,
                    f"{what}: not in the entity registry",
                    context=context,
                    suggestions=["Use ha_search() to find valid entity IDs"],
                )
            )
        return str(automatic)

    async def _execute_registry_update(
        self,
        entity_id: str,
        message: dict[str, Any],
        updates_made: list[str],
        new_entity_id: str | None,
    ) -> tuple[str, dict[str, Any], bool]:
        """Send entity registry update. Returns (effective_entity_id, core_result, has_registry_updates).

        ``core_result`` is Core's reply: ``entity_entry`` plus, when the update
        enabled the entity, ``require_restart`` or ``reload_delay``.
        """
        has_registry_updates = len(message) > 2  # more than just type + entity_id
        core_result: dict[str, Any] = {}

        if has_registry_updates:
            logger.info(
                f"Updating entity registry for {entity_id}: {', '.join(updates_made)}"
            )
            result = await self._client.send_websocket_message(message)

            if not result.get("success"):
                raise_core_refusal(
                    "Failed to update entity", result, {"entity_id": entity_id}
                )

            core_result = dict(result.get("result") or {})

            # If entity was renamed, update entity_id for subsequent operations
            if new_entity_id:
                entity_id = new_entity_id

        return entity_id, core_result, has_registry_updates

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
                # Only include entity_entry when something actually mutated,
                # as _build_expose_failure_context does.
                if partial:
                    options_failure_context["entity_entry"] = entity_entry
                raise_core_refusal(
                    f"{msg_prefix} domain '{opts_domain}'",
                    opts_result,
                    options_failure_context,
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
        entity_entry: dict[str, Any] | None,
        new_device_name: str | None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Rename the associated device. Returns (device_rename_result, entity_entry).

        ``new_device_name`` is already normalized: None means no rename.
        The returned ``entity_entry`` is None for an entity without a registry entry.
        """
        if new_device_name is None:
            return None, entity_entry

        # An entity without a registry entry has no device.
        _, entity_entry = await self._resolve_target(entity_id, entity_entry)

        device_id = (entity_entry or {}).get("device_id")
        if not device_id:
            return {
                "warnings": ["Entity has no associated device — device rename skipped"],
            }, entity_entry

        device_msg: dict[str, Any] = {
            "type": "config/device_registry/update",
            "device_id": device_id,
            "name_by_user": new_device_name,
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
        entity_entry: dict[str, Any] | None,
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
            context["entity_entry"] = entity_entry
            if options_succeeded:
                context["options_succeeded"] = options_succeeded
            if device_rename_result and device_rename_result.get("success"):
                context["device_rename_succeeded"] = True
        return context

    async def _resolve_target(
        self, entity_id: str, entity_entry: dict[str, Any] | None
    ) -> tuple[str, dict[str, Any] | None]:
        """The id Core knows the entity by, and its registry entry if any.

        Same resolution as ``expose_targets``: an id Home Assistant does not
        know is refused (ENTITY_NOT_FOUND). Callers pass an empty or missing
        ``entity_entry`` only before any write, so the refusal leaves nothing
        applied; a non-empty one is returned as it is.
        """
        if entity_entry:
            return entity_entry["entity_id"], entity_entry
        try:
            entry = await self._fetch_entity(entity_id)
        except NotInRegistry:
            known = await unregistered_lookup(self._client, [entity_id])
            if entity_id not in known:
                raise_tool_error(create_entity_not_found_error(entity_id))
            return known[entity_id][0], None
        return entry["entity_id"], entry

    async def _apply_expose_to(
        self,
        entity_id: str,
        parsed_expose_to: dict[str, bool] | None,
        entity_entry: dict[str, Any] | None,
        has_registry_updates: bool,
        options_succeeded: dict[str, dict[str, Any]],
        device_rename_result: dict[str, Any] | None,
        updates_made: list[str],
    ) -> tuple[dict[str, bool] | None, dict[str, Any] | None, str | None]:
        """Apply expose_to changes and add their entry to updates_made.

        Returns (exposure_result, entity_entry, warning): entity_entry is None
        for an entity without a registry entry, and warning says when the
        entry could not be re-read after the write.
        """
        if parsed_expose_to is None:
            return None, entity_entry, None
        target, entry = await self._resolve_target(entity_id, entity_entry)
        if not parsed_expose_to:
            updates_made.append(_expose_update(parsed_expose_to, True))
            return None, entry, None

        # Group by should_expose value for efficient API calls
        expose_true = [a for a, v in parsed_expose_to.items() if v]
        expose_false = [a for a, v in parsed_expose_to.items() if not v]
        succeeded: dict[str, bool] = {}
        changed: set[str] = set()

        for assistants, should_expose in [(expose_true, True), (expose_false, False)]:
            if not assistants:
                continue

            expose_msg: dict[str, Any] = {
                "type": "homeassistant/expose_entity",
                "assistants": assistants,
                "entity_ids": [target],
                "should_expose": should_expose,
            }
            logger.info(
                f"{'Exposing' if should_expose else 'Hiding'} {target} "
                f"{'to' if should_expose else 'from'} {assistants}"
            )
            expose_result = await self._client.send_websocket_message(expose_msg)

            if not expose_result.get("success"):
                error_msg = _extract_ws_error(expose_result)
                failed = dict.fromkeys(assistants, should_expose)
                context = self._build_expose_failure_context(
                    entity_id,
                    entry,
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
                    )
                )

            # Track successful exposures
            for a in assistants:
                succeeded[a] = should_expose
            changed |= changed_entity_ids(expose_result)

        exposure_result: dict[str, bool] | None = succeeded if succeeded else None
        unchanged = target not in changed
        updates_made.append(_expose_update(parsed_expose_to, unchanged))
        if entry is None:
            return exposure_result, None, None

        # Exposure mutates the registry entry's options, so refetch to return
        # the post-exposure state. The exposure already committed: a failed
        # refresh keeps the pre-exposure entry and says so.
        try:
            get_result = await self._client.send_websocket_message(
                {"type": "config/entity_registry/get", "entity_id": target}
            )
        except HomeAssistantError as exc:
            get_result = {"success": False, "error": str(exc)}
        if get_result.get("success"):
            return exposure_result, get_result.get("result") or {}, None
        warning = refresh_warning(entity_id, _extract_ws_error(get_result))
        logger.warning(warning)
        return exposure_result, entry, warning

    def _build_entity_response(
        self,
        entity_id: str,
        original_entity_id: str,
        entity_entry: dict[str, Any] | None,
        updates_made: list[str],
        new_entity_id: str | None,
        exposure_result: dict[str, bool] | None,
        device_rename_result: dict[str, Any] | None,
        extra_warnings: list[str] | None = None,
        core_extras: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the final response dict for a single-entity update."""
        response_data: dict[str, Any] = {
            **(core_extras or {}),
            "success": True,
            "entity_id": entity_id,
            "updates": updates_made,
            "entity_entry": entity_entry,
        }

        # Include old_entity_id and rename warning when a rename was performed;
        # Core lowercases the entity_id it is sent; a new_entity_id it
        # accepts is already lowercase.
        if (
            new_entity_id is not None
            and new_entity_id.lower() != original_entity_id.lower()
        ):
            response_data["old_entity_id"] = original_entity_id
            response_data.setdefault("warnings", []).append(
                "Remember to update any automations, scripts, or dashboards "
                "that reference the old entity_id"
            )

        if exposure_result is not None:
            response_data["exposure"] = exposure_result

        if extra_warnings:
            response_data.setdefault("warnings", []).extend(extra_warnings)

        if device_rename_result is not None:
            response_data["device_rename"] = device_rename_result
            # Return-shape contract: warnings are a top-level list as well.
            if device_rename_result.get("warnings"):
                response_data.setdefault("warnings", []).extend(
                    device_rename_result["warnings"]
                )
            # Mark partial when the device rename failed (device_id present +
            # warnings). Not partial when the entity simply has no device —
            # that's a no-op, not an incomplete operation.
            if device_rename_result.get("warnings") and device_rename_result.get(
                "device_id"
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
        parsed_aliases: list[str | None] | None,
        parsed_categories: dict[str, str | None] | None,
        parsed_labels: list[str] | None,
        label_operation: str,
        parsed_expose_to: dict[str, bool] | None,
        new_entity_id: str | None = None,
        new_device_name: str | None = None,
        device_class: str | None = None,
        parsed_options: dict[str, dict[str, Any]] | None = None,
        use_entity_name_alias: bool | None = None,
        use_device_name: bool | None = None,
    ) -> dict[str, Any]:
        """Update a single entity. Orchestrates the phase pipeline."""
        reject_name_with_use_device_name(name, use_device_name)
        if new_entity_id == "auto":
            new_entity_id = await self._automatic_entity_id(entity_id)
        # Normalized before Phase 3: a quote-only value must be rejected before the
        # entity registry write, and a blank one must not count as deferred work.
        device_name_blank = new_device_name is not None and not new_device_name.strip()
        new_device_name = clearable_value(
            new_device_name, "new_device_name", hint=_CLEAR_DEVICE_NAME_HINT
        )
        async with registry_update_locks("entity", entity_id, new_entity_id):
            # Read under the lock: the main-entity write is built from this entry
            current_entry = await ensure_use_device_name_allowed(
                use_device_name, entity_id, self._fetch_entity
            )
            # Phase 1: For add/remove label operations, fetch current labels first
            final_labels = await self._resolve_final_labels(
                entity_id, parsed_labels, label_operation
            )
            parsed_aliases, alias_note = await self._resolve_final_aliases(
                entity_id, parsed_aliases, use_entity_name_alias
            )

            # Phase 2: Build update message for entity registry
            message: dict[str, Any] = {
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
            }
            updates_made: list[str] = []
            build_name_visibility_fields(
                message,
                updates_made,
                area_id,
                name,
                icon,
                device_class,
                use_device_name,
                current_entry,
            )
            build_state_tag_fields(
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
            if alias_note:
                updates_made.append(alias_note)
            if new_entity_id is not None:
                message["new_entity_id"] = new_entity_id
                updates_made.append(
                    f"entity_id -> {new_entity_id}"
                    if new_entity_id.lower() != entity_id.lower()
                    else "entity_id unchanged"
                )
            # expose_to and device_name are appended to updates_made only after their
            # WS phases run (Phases 5-6), so the Phase-4 error context never claims
            # they were applied before they ran.
            has_deferred_work = (
                parsed_expose_to is not None or new_device_name is not None
            )
            if not updates_made and not parsed_options and not has_deferred_work:
                if device_name_blank:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "new_device_name is blank and no other update was given",
                            suggestions=[_CLEAR_DEVICE_NAME_HINT],
                            context={"entity_id": entity_id},
                        )
                    )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "No updates specified",
                        suggestions=[
                            "Provide at least one of: area_id, name, use_device_name, icon, device_class, enabled, hidden, aliases, use_entity_name_alias, categories, labels, options, expose_to, new_entity_id, or new_device_name"
                        ],
                    )
                )

            # Save original entity_id before potential rename
            original_entity_id = entity_id

            # Issue #2159: validate cross-registry references immediately before
            # the write — the narrowest window against a concurrent registry
            # deletion (#2160 placed the area check here for the same reason).
            # Bulk calls also revalidate here after waiting for the entity lock.
            # For label add, only the added IDs are checked: the merged
            # set may legitimately carry pre-existing dangling labels, whose
            # cleanup path (label_operation="remove") must stay open.
            await validate_registry_ids(
                self._client,
                message.get("area_id"),  # normalized by build_name_visibility_fields
                parsed_labels if label_operation in ("set", "add") else None,
                parsed_categories,
                fail_closed=True,
            )

            # Phase 3: Send entity registry update (covers all fields except expose_to)
            (
                entity_id,
                core_result,
                registry_updated,
            ) = await self._execute_registry_update(
                entity_id, message, updates_made, new_entity_id
            )
            entity_entry = core_result.pop("entity_entry", {})

            # Phase 4: Per-domain options; Phases 3-6 run under one hold of the lock
            entity_entry, options_succeeded = await self._apply_options_updates(
                entity_id, parsed_options, entity_entry, registry_updated, updates_made
            )

            # Phase 5: Device rename
            device_rename_result: dict[str, Any] | None = None
            if device_name_blank:
                device_name_warning: str | None = _CLEAR_DEVICE_NAME_HINT
            else:
                device_name_warning = None
                device_rename_result, entity_entry = await self._apply_device_rename(
                    entity_id, entity_entry, new_device_name
                )
            if device_rename_result and device_rename_result.get("success"):
                updates_made.append(f"device_name -> {new_device_name}")

            # Phase 6: Expose to assistants
            (
                exposure_result,
                entity_entry,
                exposure_warning,
            ) = await self._apply_expose_to(
                entity_id,
                parsed_expose_to,
                entity_entry,
                registry_updated,
                options_succeeded,
                device_rename_result,
                updates_made,
            )

        # Phase 7: Build response
        return self._build_entity_response(
            entity_id,
            original_entity_id,
            entity_entry,
            updates_made,
            new_entity_id,
            exposure_result,
            device_rename_result,
            [w for w in (device_name_warning, exposure_warning) if w],
            core_result,
        )

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
    ) -> tuple[list[str], list[dict[str, Any]], list[str]]:
        """Batch-expose eligible_ids, then refetch the registry entries among them.

        Returns (still_eligible_ids, newly_failed, warnings). An id that
        ``expose_targets`` cannot resolve fails before the write. A failed
        expose call is reported against every id it was sent for (each call
        acts on the whole list) and clears eligibility. On success, options
        mutate, so one get_entries refetch refreshes each registry entry; a
        refetch failure keeps the pre-exposure entry, since the exposure
        already committed, and adds a warning.
        """
        targets, failed = await expose_targets(self._client, eligible_ids, entry_by_id)
        eligible_ids = [eid for eid in eligible_ids if eid in targets]
        if not eligible_ids:
            return [], failed, []
        expose_error, changed = await apply_bulk_expose(
            self._client,
            [targets[eid] for eid in eligible_ids],
            parsed_expose_to,
            _extract_ws_error,
        )
        if expose_error is not None:
            failed.extend(
                {"entity_id": eid, "error": f"Exposure failed: {expose_error}"}
                for eid in eligible_ids
            )
            return [], failed, []

        for eid in eligible_ids:
            unchanged = targets[eid] not in changed
            updates_by_id[eid].append(_expose_update(parsed_expose_to, unchanged))
        registered = [eid for eid in eligible_ids if entry_by_id.get(eid) is not None]
        refetched, refetch_errors = await get_entries_raw(
            self._client, [targets[eid] for eid in registered]
        )
        warnings: list[str] = []
        for eid in registered:
            if targets[eid] in refetched:
                entry_by_id[eid] = refetched[targets[eid]]
            else:
                warnings.append(refresh_warning(eid, refetch_errors[targets[eid]]))
        if warnings:
            logger.warning("Bulk exposure applied; %s", " ".join(warnings))
        return eligible_ids, failed, warnings

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

        warnings: list[str] = []
        if parsed_expose_to is not None and eligible_ids:
            eligible_ids, expose_failed, warnings = await self._bulk_expose_phase(
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
        if warnings:
            response["warnings"] = warnings
        return response

    async def _fetch_entity(self, eid: str) -> dict[str, Any]:
        """Fetch a single entity from the registry."""
        message: dict[str, Any] = {
            "type": "config/entity_registry/get",
            "entity_id": eid,
        }
        # Not in the registry is an answer here, not a failure to log.
        result = await self._client.send_websocket_message(
            message, expected_error_codes=frozenset({"not_found"})
        )

        if not result.get("success"):
            if result.get("error_code") == "not_found":
                raise NotInRegistry(_extract_ws_error(result))
            raise_core_refusal(
                f"Reading the registry entry of {eid} failed",
                result,
                {"entity_id": eid},
            )

        return result.get("result") or {}

    @tool(
        name="ha_set_entity",
        tags={"Entity Registry"},
        annotations=write_hints(
            "Set Entity", destructive=True, idempotent=True, open_world=False
        ),
    )
    @with_auto_backup(domain="entity", id_fn=_single_entity_target)
    @log_tool_usage
    async def ha_set_entity(
        self,
        entity_id: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Entity ID or list of entity IDs to update. Omit only for the "
                    "registry-wide expose_new_entities and entity_id_parts."
                ),
                default=None,
            ),
        ] = None,
        area_id: Annotated[
            str | None,
            Field(
                description=(
                    "Area/room ID to assign the entity to. Use empty string '' to "
                    "unassign from current area. " + WHITESPACE_CLEARS_NOTE
                ),
                default=None,
            ),
        ] = None,
        name: Annotated[
            str | None,
            Field(
                description=(
                    "The entity's own name part, not the full name. Use empty string '' to remove "
                    "the custom name and revert to default. " + WHITESPACE_CLEARS_NOTE
                ),
                default=None,
            ),
        ] = None,
        use_device_name: Annotated[
            bool | None,
            Field(
                description=(
                    "The entity dialog's 'Main entity of the device' switch. True "
                    "names the entity after its device, following device renames, "
                    "and like the dialog moves it from its own area to the "
                    "device's. An entity without a name of its own already is the "
                    "main entity and is left as is. False restores the "
                    "integration's default name. Omit to leave it as is; not "
                    "combinable with name."
                ),
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description=(
                    "Icon for the entity (e.g., 'mdi:thermometer'). Use empty string "
                    "'' to remove custom icon. " + WHITESPACE_CLEARS_NOTE
                ),
                default=None,
            ),
        ] = None,
        device_class: Annotated[
            str | None,
            Field(
                description=(
                    "Override the entity's display device class — what the HA UI's 'Show "
                    "As' dropdown writes. Use empty string '' to clear the override and "
                    "fall back to the integration default. Examples: 'window', 'door', "
                    "'motion' for binary_sensor; 'temperature', 'humidity' for sensor. "
                    + WHITESPACE_CLEARS_NOTE
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
                    "For 'Show As' use device_class; for "
                    "voice-assistant exposure use expose_to, not "
                    "options.<assistant>.should_expose."
                ),
                default=None,
            ),
        ] = None,
        enabled: Annotated[
            bool | None,
            Field(
                description=(
                    "True to enable the entity, False to disable it. WARNING: "
                    "enabled=False is a registry-level disable, not 'turning off': "
                    "the entity leaves the state machine, the UI, dashboards and "
                    "automations. Re-enabling returns Home Assistant's own answer: "
                    "require_restart, or reload_delay (seconds until it reloads "
                    "the integration itself)."
                ),
                default=None,
            ),
        ] = None,
        hidden: Annotated[
            bool | None,
            Field(
                description="True to hide the entity from UI, False to show it.",
                default=None,
            ),
        ] = None,
        aliases: Annotated[
            str | list[str | None] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "List of voice assistant aliases for the entity (replaces existing "
                    "aliases). A null entry is the entity's own name (HA's 'use entity "
                    "name' switch); it is kept automatically unless your list already "
                    "contains null. To turn that switch off or on, use "
                    "use_entity_name_alias."
                ),
                default=None,
            ),
        ] = None,
        use_entity_name_alias: Annotated[
            bool | None,
            Field(
                description=(
                    "HA's 'use entity name' voice-alias switch. True keeps the entity's "
                    "own name answering in Assist, False turns it off so only the "
                    "aliases match. Omit to leave it as is. Works with or without "
                    "aliases."
                ),
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
                    'Use null value to clear: {"automation": null}.'
                ),
                default=None,
            ),
        ] = None,
        labels: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description="List of label IDs for the entity. Behavior depends on label_operation parameter. Use [] with label_operation='set' to clear.",
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
                    "Assistant IDs Home Assistant knows, today 'conversation' (Assist), 'cloud.alexa' "
                    "and 'cloud.google_assistant'; it refuses any other name before any change is sent to it. "
                    'Example: {"conversation": true, "cloud.alexa": false}. '
                    "An entity without a registry entry is written under the id Home "
                    "Assistant reads it by and answers with entity_entry null; an id "
                    "Home Assistant does not know is refused before anything is written."
                ),
                default=None,
            ),
        ] = None,
        expose_new_entities: Annotated[
            dict[str, bool] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Registry-wide, no entity_id: each assistant's 'Expose new "
                    "entities' switch, as a dict mapping assistant IDs to booleans "
                    '(e.g. {"cloud.alexa": false}). Home Assistant refuses an '
                    "assistant it does not know before any change is sent."
                ),
                default=None,
            ),
        ] = None,
        entity_id_parts: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Registry-wide, no entity_id: the name parts new entity IDs "
                    "are built from, in order. Home Assistant judges the list; "
                    "today it takes 'area', 'floor', 'parent_device', 'device' "
                    "and 'entity', and requires 'device' and 'entity'. [] "
                    "restores Home Assistant's default."
                ),
                default=None,
            ),
        ] = None,
        new_entity_id: Annotated[
            str | None,
            Field(
                description=(
                    "New entity ID to rename to (e.g., 'light.new_name'); the domain "
                    "must stay the same. 'auto' takes the ID Home Assistant would "
                    "generate for the entity now, like the UI's Restore entity ID "
                    "(no rename when the entity already has it)."
                ),
                default=None,
            ),
        ] = None,
        new_device_name: Annotated[
            str | None,
            Field(
                description=(
                    "New display name for the associated device. An entity_id "
                    "Home Assistant does not know is refused; an entity without a "
                    "device gets a warning. Empty or whitespace-only never changes "
                    "the device name: it is "
                    "reported under warnings when other fields are given and is an "
                    "error when it is the only update. To clear the device name use "
                    "ha_set_device(name='')."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Update entity registry entries, or without entity_id the registry-wide settings.

        Allows modifying entity metadata such as area assignment, display name,
        icon, "Show As" device class override, per-domain registry options,
        enabled/disabled state, visibility, aliases, labels, voice assistant
        exposure, and entity_id rename in a single call.

        BULK OPERATIONS:
        When entity_id is a list, only labels, expose_to, and categories parameters are supported.
        Other parameters (area_id, name, use_device_name, icon, device_class, options, enabled, hidden, aliases, use_entity_name_alias, new_entity_id, new_device_name) require single entity.

        SHOW AS / DEVICE CLASS: a device_class change applies instantly, no reload
        needed.

        REGISTRY OPTIONS: multi-domain options are sent as separate registry
        updates because HA's WS schema requires options_domain + options to be
        paired one domain at a time.

        ENTITY ID RENAME: Voice exposure settings are preserved automatically.

        WARNING: Renaming an entity_id does NOT update references in automations,
        scripts, templates, or dashboards. All consumers of the old entity_id must
        be updated manually — HA does not propagate the rename automatically.

        Rename limitation: an entity without a unique ID cannot be renamed.

        REGISTRY-WIDE SETTINGS: for the parameters sent, the reply has Home
        Assistant's `settings`, the `expose_new_entities` written, and the values
        they replace
        (`previous_settings`, `previous_expose_new_entities`). ha_get_entity()
        reads them.

        Use ha_search() or ha_get_device() to find entity IDs.
        Use ha_config_get_label() to find available label IDs.

        EXAMPLES:
        - Assign to area: ha_set_entity("sensor.temp", area_id="living_room")
        - Set Show As: ha_set_entity("binary_sensor.zone_10", device_class="window")
        - Set sensor precision: ha_set_entity("sensor.power", options={"sensor": {"display_precision": 2}})
        - Stop exposing new entities to Alexa: ha_set_entity(expose_new_entities={"cloud.alexa": false})
        - Rename entity and device: ha_set_entity("light.old", new_entity_id="light.new", new_device_name="New Lamp")
        - Add labels to multiple: ha_set_entity(["light.a", "light.b"], labels=["new"], label_operation="add")
        - Expose to Alexa: ha_set_entity("light.lamp", expose_to={"cloud.alexa": True})

        AUTOMATIONS AND SCRIPTS:
        For automations and scripts, enabled=False is blocked. For automations,
        use ha_config_set_automation(entity_id="automation.xxx", enabled=False)
        (or enabled=True to re-enable).
        For scripts, ha_config_set_script(script_id="script.xxx", run="stop")
        only stops a currently running execution; it does not disable the script.
        Home Assistant has no script runtime enable/disable service.
        """
        try:
            single_entity_params = {
                "area_id": area_id,
                "name": name,
                "use_device_name": use_device_name,
                "icon": icon,
                "device_class": device_class,
                "options": options,
                "enabled": enabled,
                "hidden": hidden,
                "aliases": aliases,
                "use_entity_name_alias": use_entity_name_alias,
                "new_entity_id": new_entity_id,
                "new_device_name": new_device_name,
            }
            if is_registry_wide_call(
                entity_id,
                {
                    "expose_new_entities": expose_new_entities,
                    "entity_id_parts": entity_id_parts,
                },
                {
                    **single_entity_params,
                    "categories": categories,
                    "labels": labels,
                    "expose_to": expose_to,
                },
            ):
                return await set_registry_wide(
                    self._client, expose_new_entities, entity_id_parts
                )
            entity_ids, is_bulk = _parse_set_entity_ids(entity_id)

            # Per-element empty/whitespace check — the list-empty check above
            # rejects ``[]`` but not ``[""]``; without this guard, an empty
            # entity_id would propagate to the entity-registry update WS call
            # and surface as a misleading HA "entity not found".
            for eid in entity_ids:
                validate_identifier_not_empty(eid, "entity_id")

            # Validate: bulk operations only support categories, labels, and expose_to
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
                            "Use a single entity_id for area_id, name, use_device_name, icon, device_class, options, enabled, hidden, aliases, or use_entity_name_alias",
                            "Or remove single-entity parameters to use bulk categories/labels/expose_to",
                        ],
                    )
                )

            _validate_enabled_constraint(enabled, entity_ids)

            parsed_aliases = _parse_aliases_param(aliases)
            parsed_categories = _parse_categories_param(categories)
            parsed_labels = _parse_string_list_field(labels, "labels")
            parsed_options = _parse_options_param(options)
            parsed_expose_to = _parse_expose_to_param(expose_to)
            await reject_unknown_assistants(self._client, parsed_expose_to)

            # Reject invalid shared references before any bulk writes. Each
            # entity also revalidates under its lock to catch references deleted
            # while waiting, preserving per-entity partial-progress reporting.
            if is_bulk:
                await validate_registry_ids(
                    self._client,
                    None,
                    parsed_labels if label_operation in ("set", "add") else None,
                    parsed_categories,
                    fail_closed=True,
                )

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
                    use_entity_name_alias=use_entity_name_alias,
                    use_device_name=use_device_name,
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
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error updating entity: {e}")
            exception_to_structured_error(e, context={"entity_id": entity_id})
            return None  # unreachable: exception_to_structured_error always raises

    @tool(
        name="ha_get_entity",
        tags={"Entity Registry"},
        annotations=read_only_hints("Get Entity", open_world=False),
    )
    @log_tool_usage
    async def ha_get_entity(
        self,
        entity_id: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description="Entity ID or list of entity IDs to retrieve (e.g., 'sensor.temperature' or ['light.living_room', 'switch.porch']).",
                default=None,
            ),
        ] = None,
        unique_id: Annotated[
            str | None,
            Field(
                description=(
                    "Stable integration unique_id (entity_id is mutable, "
                    "unique_id is not). Mutually exclusive with entity_id."
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
        exposed_to: Annotated[
            str | None,
            Field(
                description=(
                    "Without entity_id and unique_id: list only the entities "
                    "exposed to this voice assistant, an ID Home Assistant knows "
                    "(today 'conversation', 'cloud.alexa', 'cloud.google_assistant'). "
                    "Adds `assistant` and its `expose_new_entities` flag; leaves "
                    "out `summary`."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get entity registry entries, or without an entity the registry settings and exposed entities.

        Returns each entry as Home Assistant's entity registry serves it
        (`config/entity_registry/get` / `get_entries`): area_id, name override,
        original_name, icon, disabled_by/hidden_by, aliases (a null alias is the
        entity's own name), labels, categories, device_class, capabilities,
        entity_category, per-domain options, platform, device_id,
        config_entry_id, unique_id and `next_name_part`. An entity with a
        device_id and no name of its own (`name: ""`, or `name: null` with no
        `original_name`) is the main entity of its device (the dialog's "Main
        entity of the device" switch). Plus the enrichment keys below.

        RESOLVER MODE:
        Pass unique_id to resolve it to its entity_id(s). Since the registry's
        unique key is (domain, platform, unique_id), the same unique_id can match
        multiple platforms — all matches are returned in entity_entries with a
        `matches` count. Narrow with domain/platform.

        When config_entry_id is non-null — e.g. for UI-created template / group /
        utility_meter / derivative helpers — pass it to
        ``ha_get_integration(entry_id=..., include_options=True)`` to read the
        helper's current config (template body, group members, etc.) without
        scanning a domain list.

        Resolved-name enrichment on an entity_id or unique_id lookup, served by the
        ha_mcp_tools component (absent without it, and a warning says which
        fields are missing and whether to install or update the component):
        `full_name` (the name Home Assistant shows, composed from the parent
        device, device and entity names; null when it has none),
        `area` (assigned area NAME, device-inherited when the entity has none),
        `floor`, and `label_names`. This tool's base `labels` carries the label ids;
        ha_search result_fields and the exposure listing's `entity_info`
        instead emit the resolved names under `labels`.

        VOICE ASSISTANT EXPOSURE:
        An entity with a registry entry keeps its exposure in the entry's
        `options` (`options.<assistant>.should_expose`; absent while nothing is
        stored); ha_set_entity(expose_to=...) sets it. An entity without a
        registry entry that has a state, or is stored as exposed, returns
        `entity_entry: null` with `exposed_to` (the assistants Home Assistant has
        stored as exposed) and
        `is_exposed_anywhere`; a list call returns such entities under
        `unregistered_entities`.

        WITHOUT entity_id AND unique_id: returns the registry-wide state:
        `settings` (Home Assistant's `config/entity_registry/settings/get`:
        `entity_id_parts`, the name parts new entity IDs are built from, null
        for Home Assistant's default) and `exposed_entities`, the entities
        exposed to at least one voice assistant as Home Assistant's
        `expose_entity/list` map, with `count` and a per-assistant `summary`
        (an assistant with no exposed entity is left out). An entity missing
        from that map is not stored as exposed: either it is stored as not
        exposed, or nothing is stored yet and Home Assistant applies its own
        default when an assistant first asks (it depends on the assistant's
        expose-new-entities flag and on the entity). When the ha_mcp_tools
        component advertises it, `entity_info` names each listed entity:
        domain, area, floor and labels (plus friendly_name and state when it
        has a state). Without it the listing has no `entity_info`, and a
        warning says whether to install or update the component.

        RELATED TOOLS: ha_set_entity() to modify these properties; ha_get_state()
        for current state/attributes; ha_search() to find entities.

        EXAMPLES:
        - Single entity: ha_get_entity("sensor.temperature")
        - Multiple entities: ha_get_entity(["light.living_room", "switch.porch"])
        - Resolve a unique_id: ha_get_entity(unique_id="00:11:22:33", domain="sensor")
        - Entities exposed to Alexa: ha_get_entity(exposed_to="cloud.alexa")
        - Registry settings and all exposed entities: ha_get_entity()
        """
        try:
            if exposed_to is not None and (
                entity_id is not None or unique_id is not None
            ):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "exposed_to lists the exposed entities; it takes no "
                        "entity_id or unique_id.",
                        suggestions=[
                            "Drop exposed_to: an entry's exposure is in its options",
                            "Drop entity_id/unique_id to list the exposed entities",
                        ],
                    )
                )
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
                resolved = await resolve_by_unique_id(
                    self._client, unique_id, domain, platform
                )
                if warning := await self._enrich_entries(resolved["entity_entries"]):
                    resolved["warnings"] = [warning]
                return resolved

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

            if entity_id is None:
                return await get_registry_wide(self._client, exposed_to)
            return await self._get_by_entity_id(entity_id)

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
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
        except NotInRegistry:
            # Without a registry entry, an entity has exposure settings only
            # once Core stored some, in its separate exposed-entities store;
            # Core knows it by its state, or while an assistant has it stored
            # as exposed.
            try:
                known = await unregistered_lookup(self._client, [eid])
            except HomeAssistantError as exc:
                exception_to_structured_error(
                    exc, context={"entity_id": eid, "registry_entry": None}
                )
            if eid not in known:
                raise_tool_error(create_entity_not_found_error(eid))
            return {"success": True, **unregistered_row(eid, known[eid][1])}
        warning = await self._enrich_entries([result])
        response: dict[str, Any] = {
            "success": True,
            "entity_id": eid,
            "entity_entry": result,
        }
        if warning:
            response["warnings"] = [warning]
        return response

    async def _get_by_entity_id(self, entity_id: str | list[str]) -> dict[str, Any]:
        """Look up entities by entity_id (single or bulk list)."""
        entity_ids, is_bulk, early_response = _parse_get_entity_ids(entity_id)
        if early_response is not None:
            return early_response

        if not is_bulk:
            return await self._get_single_entity(entity_ids[0])

        logger.info(f"Getting entity registry entries for {len(entity_ids)} entities")
        raw_map, known, error_map = await get_entries_or_unregistered(
            self._client, entity_ids
        )

        # Iterate the requested ids (not the map) to preserve request order and
        # duplicate handling; each id is found, unregistered, or an error.
        entity_entries: list[dict[str, Any]] = []
        unregistered_entities: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for eid in entity_ids:
            raw = raw_map.get(eid)
            if raw is not None:
                entity_entries.append(raw)
            elif eid in known:
                unregistered_entities.append(unregistered_row(eid, known[eid][1]))
            else:
                errors.append({"entity_id": eid, "error": error_map[eid]})

        warning = await self._enrich_entries(entity_entries)

        response: dict[str, Any] = {
            "success": True,
            "count": len(entity_entries),
            "entity_entries": entity_entries,
        }
        if warning:
            response["warnings"] = [warning]
        if unregistered_entities:
            response["unregistered_entities"] = unregistered_entities
        if errors:
            response["errors"] = errors
            response["suggestions"] = [
                "Use ha_search() to find valid entity IDs for failed lookups"
            ]
        return response

    async def _enrich_entries(self, entity_entries: list[dict[str, Any]]) -> str | None:
        """Add the component's entity_enrich fields to the found entries.

        Returns a warning whenever the entries come without some of them: no
        component answered (install it), the component is too old for the
        command, its command failed, or it predates ``full_name`` (update it),
        or it could not be asked.
        """
        found_ids = [e["entity_id"] for e in entity_entries]
        if not found_ids:
            return None
        # Read before the fetch: an unknown_command there drops the cached caps.
        # Strict, so a refused or failed probe is not taken for no component.
        try:
            caps = await get_component_caps(self._client, strict=True)
        except ComponentDiscoveryError as exc:
            return component_unasked_warning(exc, _WITHOUT_ENRICHMENT)
        enriched = await fetch_entity_enrichment_via_component(self._client, found_ids)
        if enriched is None:
            return component_unusable_warning(caps, _WITHOUT_ENRICHMENT)
        with_full_name = await component_serves(self._client, ENTITY_ENRICH_FULL_NAME)
        for entry in entity_entries:
            _merge_entity_enrichment(
                entry, enriched.get(entry["entity_id"]), with_full_name
            )
        if with_full_name:
            return None
        return (
            "The ha_mcp_tools component predates full_name, so the entries come "
            f"without it. Update it {COMPONENT_VIA_HACS}"
        )

    async def _bulk_remove_entities(self, entity_ids: list[str]) -> dict[str, Any]:
        """Validate the id list, then remove the ids one at a time; registry
        writes are not parallelized (see ``remove_registry_entries``)."""
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
        return await remove_registry_entries(
            self._client, entity_ids, _extract_ws_error
        )

    @tool(
        name="ha_remove_entity",
        tags={"Entity Registry"},
        annotations=write_hints(
            "Remove Entity", destructive=True, idempotent=True, open_world=False
        ),
    )
    @with_auto_backup(domain="entity", id_fn=_single_entity_target)
    @log_tool_usage
    async def ha_remove_entity(
        self,
        entity_id: Annotated[
            str | list[str],
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Entity ID, or a list of entity IDs, to remove from the "
                    "entity registry (e.g., 'sensor.old_temperature')."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Remove one or more entities from the Home Assistant entity registry.

        Permanently removes the entity registration; the entity will no longer
        appear in the UI or be available to automations. Use only for orphaned
        or stale entity entries: if the underlying device, integration or
        stored helper is still there, the entity may be re-added on the next HA
        restart or reload; for a stored helper's entity a single-id removal
        names the tool that removes the helper. Added again, it gets back its registry
        settings while its config entry exists, and for 30 days after that entry, or an
        entity without one, was removed. For most use cases, consider disabling instead:
        ha_set_entity(entity_id="sensor.old", enabled=False).

        BULK MODE:
        Pass a list of entity IDs to remove up to 100 at once — handy for
        clearing the restored=true orphans an integration leaves behind after
        its filters change. Removals run sequentially and return:
          {removed: [...], skipped: [...], errors: [{entity_id, code, message}]}
        where skipped = ids already absent (not-found is idempotent). A lost connection
        stops the run; not_attempted lists the ids never sent (the failing one too if
        it never left), safe to retry. Only single-id removal is auto-backed-up.

        EXAMPLES:
        - Remove orphaned sensor: ha_remove_entity("sensor.old_temperature")
        - Bulk cleanup: ha_remove_entity(["sensor.orphan_1", "sensor.orphan_2"])

        Use ha_search / ha_get_entity to verify an entity before removing it.
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
            result, notes = await remove_registry_entry(self._client, entity_id)

            if not result.get("success"):
                error_msg = _extract_ws_error(result)
                if result.get("error_code") == "not_found":
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

            return {"success": True, "entity_id": entity_id} | (
                {"warnings": notes} if notes else {}
            )

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error removing entity '{entity_id}': {e}")
            exception_to_structured_error(
                e,
                context={"entity_id": entity_id},
            )
            return None  # unreachable: exception_to_structured_error always raises


def register_entity_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register entity management tools with the MCP server."""
    register_tool_methods(mcp, EntityTools(client))
