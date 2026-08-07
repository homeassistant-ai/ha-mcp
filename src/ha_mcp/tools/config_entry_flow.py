"""
Config Entry Flow API machinery for Home Assistant MCP server.

This module provides the shared machinery for creating and updating
config-entry-based helpers (template, group, utility_meter, etc.) via the
Config Entry Flow API.

The create/update entry point is the unified ha_config_set_helper tool in
tools_config_helpers.py, which routes to create_flow_helper / update_flow_helper
for the 15 helper types listed in FLOW_HELPER_TYPES.

The same flow walkers drive every other config-entry surface, not just
helpers: ``ha_set_integration`` creates entries for arbitrary domains through
``create_config_entry`` and edits them through ``update_config_entry_options``,
and ``ha_config_set_helper(helper_type="config_subentry")`` drives subentry
flows through ``set_config_subentry``.

The step machinery those entry points drive lives in three sibling modules,
imported in one direction only (menu <- form <- walker <- here):

- ``config_entry_flow_menu``: menu selection keys and menu-step handling
- ``config_entry_flow_form``: form-step schema consumption and reuse tracking
- ``config_entry_flow_walker``: step submission, HA error translation, flow
  introspection, and the two flow walkers
"""

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

from fastmcp.exceptions import ToolError

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .config_entry_flow_form import _extract_schema_field_names
from .config_entry_flow_walker import (
    _FlowType,
    _handle_config_subentry_flow_steps,
    _handle_flow_steps,
)
from .helpers import raise_tool_error, validate_identifier_not_empty

logger = logging.getLogger(__name__)

_KNOWN_AUXILIARY_ENTRY_DOMAINS = frozenset(
    {"derivative", "switch_as_x", "threshold", "utility_meter"}
)
_DEVICE_CONNECTION_ID_TYPES = frozenset({"ieee", "mac", "zigbee"})
_TRANSIENT_RECONFIGURE_STATES = frozenset({"setup_in_progress"})


@dataclass(frozen=True)
class PreparedReconfigure:
    """Validated state shared by reconfigure preflight and confirmed apply."""

    entry_id: str
    entry: dict[str, Any]
    flow_config: dict[str, Any]
    identity: dict[str, Any]
    expected_identity: dict[str, Any]


def build_reconfigure_rollback_metadata(
    entry_id: str,
    domain: str,
) -> dict[str, Any]:
    """Describe the manual rollback path for an existing config entry.

    Home Assistant's config-entry REST representation does not expose the
    entry's connection data. The generic backup is therefore audit evidence,
    not an automatic endpoint rollback. An operator must repeat the official
    reconfigure flow with the known previous values.
    """
    return {
        "strategy": "official_reconfigure_flow",
        "automatic": False,
        "operator_action_required": True,
        "manual_required": True,
        "manual_reason": "previous_config_unavailable",
        "entry_id": entry_id,
        "domain": domain,
        "previous_config": None,
        "backup_scope": "edits",
        "backup_restore_supported": False,
        "backup_restore_note": (
            "The generic integration snapshot is audit evidence only; its shared "
            "restore handler does not restore connection settings. Use the official "
            "reconfigure flow instead."
        ),
    }


async def _abort_flow_best_effort(client: Any, flow_id: str) -> None:
    """Abort a still-pending flow without hiding the original failure."""
    try:
        await asyncio.wait_for(client.abort_config_flow(flow_id), timeout=5.0)
    except Exception as abort_err:
        logger.warning("Failed to abort flow %s after error: %s", flow_id, abort_err)


async def _abort_subentry_flow_best_effort(client: Any, flow_id: str) -> None:
    """Abort a pending subentry flow without hiding the original failure."""
    try:
        await asyncio.wait_for(client.abort_config_subentry_flow(flow_id), timeout=5.0)
    except Exception as abort_err:
        logger.warning(
            "Failed to abort config subentry flow %s after error: %s",
            flow_id,
            abort_err,
        )


def _normalise_identity_value(value: Any) -> str:
    """Normalise registry identifiers for stable comparisons."""
    return "".join(character for character in str(value).upper() if character.isalnum())


def _is_hardware_identifier(value: Any) -> bool:
    """Return whether ``value`` looks like a MAC or IEEE hardware ID."""
    normalised = _normalise_identity_value(value)
    return len(normalised) in {12, 16} and all(
        character in "0123456789ABCDEF" for character in normalised
    )


async def _collect_reconfigure_identity(
    client: Any,
    entry: dict[str, Any],
    entry_id: str,
) -> dict[str, Any]:
    """Collect registry identity without requiring the physical device online."""
    identity: dict[str, Any] = {
        "unique_id": entry.get("unique_id"),
        "device_ids": [],
        "entity_ids": [],
        "macs": [],
        "related_entry_ids": [],
        "entity_registry_available": False,
        "device_registry_available": False,
        "registry_available": False,
    }
    entity_rows = await _optional_registry_rows(
        client, "list_entity_registry", entry_id
    )
    device_rows = await _optional_registry_rows(
        client, "list_device_registry", entry_id
    )

    if entity_rows is not None:
        matching_entities = [
            row for row in entity_rows if row.get("config_entry_id") == entry_id
        ]
        identity["entity_ids"] = sorted(
            row["entity_id"]
            for row in matching_entities
            if isinstance(row.get("entity_id"), str)
        )
        identity["device_ids"] = sorted(
            {row["device_id"] for row in matching_entities if row.get("device_id")}
        )
        current_device_ids = set(identity["device_ids"])
        identity["related_entry_ids"] = sorted(
            {
                row["config_entry_id"]
                for row in entity_rows
                if row.get("device_id") in current_device_ids
                and row.get("config_entry_id")
            }
        )
        identity["entity_registry_available"] = True
        identity["registry_available"] = True

    if device_rows is not None:
        device_ids = set(identity["device_ids"])
        matching_device_rows = [
            row
            for row in device_rows
            if row.get("id") in device_ids or entry_id in row.get("config_entries", [])
        ]
        identity["device_ids"] = sorted(
            {
                row["id"]
                for row in matching_device_rows
                if isinstance(row.get("id"), str)
            }
            | device_ids
        )
        identifiers: list[Any] = []
        for row in matching_device_rows:
            for identifier in (
                *row.get("connections", []),
                *row.get("identifiers", []),
            ):
                if not isinstance(identifier, (list, tuple)) or len(identifier) < 2:
                    continue
                identifier_type = str(identifier[0]).lower()
                identifier_value = identifier[1]
                if (
                    identifier_type in _DEVICE_CONNECTION_ID_TYPES
                    or _is_hardware_identifier(identifier_value)
                ):
                    identifiers.append(identifier_value)
        identity["macs"] = sorted(
            {
                _normalise_identity_value(value)
                for value in identifiers
                if _normalise_identity_value(value)
            }
        )
        related_entry_ids = set(identity["related_entry_ids"])
        for row in matching_device_rows:
            related_entry_ids.update(
                item for item in row.get("config_entries", []) if isinstance(item, str)
            )
        identity["related_entry_ids"] = sorted(related_entry_ids)
        identity["device_registry_available"] = True
        identity["registry_available"] = True
    return identity


async def _same_domain_related_entry_ids(
    client: Any,
    identity: dict[str, Any],
    *,
    entry_id: str,
    domain: str,
) -> list[str]:
    """Classify related entries and return those that must block reconfigure.

    Home Assistant may intentionally attach an auxiliary platform entry such as
    ``switch_as_x`` to the same physical device. That is not a second physical
    integration entry and must not block reconfiguration of the primary entry.
    Only explicitly known auxiliary domains are allowed. Unknown or malformed
    relationships remain blocking so the duplicate safeguard fails closed.
    """
    related_entry_ids = set(identity.get("related_entry_ids", [])) - {entry_id}
    identity["auxiliary_related_entry_ids"] = []
    identity["blocking_related_entry_ids"] = []
    if not related_entry_ids:
        return []

    entries = await _validated_config_entry_rows(client, entry_id)
    entries_by_id = {
        item.get("entry_id"): item
        for item in entries
        if isinstance(item, dict) and item.get("entry_id")
    }
    blocking: list[str] = []
    auxiliary: list[str] = []
    for related_id in sorted(related_entry_ids):
        related_entry = entries_by_id.get(related_id)
        related_domain = (
            related_entry.get("domain") if isinstance(related_entry, dict) else None
        )
        if not isinstance(related_domain, str) or not related_domain:
            related_domain = (
                related_entry.get("handler")
                if isinstance(related_entry, dict)
                else None
            )
        if (
            related_domain == domain
            or related_domain not in _KNOWN_AUXILIARY_ENTRY_DOMAINS
        ):
            blocking.append(related_id)
        else:
            auxiliary.append(related_id)

    identity["auxiliary_related_entry_ids"] = auxiliary
    identity["blocking_related_entry_ids"] = blocking
    return blocking


async def _validated_config_entry_rows(
    client: Any, entry_id: str
) -> list[dict[str, Any]]:
    """Read config entries and reject malformed rows before identity checks."""
    result = await client.list_config_entries()
    if not isinstance(result, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("entry_id"), str)
        for row in result
    ):
        raise HomeAssistantAPIError(
            f"Unexpected config-entry registry response for {entry_id}",
            status_code=500,
        )
    return result


async def _optional_registry_rows(
    client: Any,
    method_name: str,
    entry_id: str,
) -> list[dict[str, Any]] | None:
    """Read one registry, preserving transport failures for fail-closed callers."""
    method: Any = getattr(client, method_name, None)
    if not callable(method):
        return None
    try:
        result: Any = method()
        if not inspect.isawaitable(result):
            return None
        result = await result
    except (
        ConnectionError,
        OSError,
        TimeoutError,
        HomeAssistantConnectionError,
        HomeAssistantCommandError,
        HomeAssistantCommandTimeout,
    ) as err:
        raise (
            err
            if isinstance(err, HomeAssistantConnectionError)
            else HomeAssistantConnectionError(
                f"{method_name} transport unavailable for {entry_id}"
            )
        ) from err
    if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
        raise HomeAssistantAPIError(
            f"Unexpected response from {method_name} for {entry_id}",
            status_code=500,
        )
    return result


def _has_reconfigure_identity_anchor(
    identity: dict[str, Any], expected_identity: dict[str, Any]
) -> bool:
    """Return whether preflight has stable or caller-supplied identity evidence."""
    return bool(
        identity.get("unique_id")
        or identity.get("device_ids")
        or identity.get("entity_ids")
        or identity.get("macs")
        or expected_identity.get("device_id")
        or expected_identity.get("unique_id")
        or expected_identity.get("mac")
        or expected_identity.get("entity_ids")
    )


def _verification_state(before_available: bool, after_available: bool) -> str:
    if before_available and after_available:
        return "preserved"
    if after_available:
        return "available_after_change"
    if before_available:
        return "unavailable_after_change"
    return "unavailable_before_change"


def _is_transient_reconfigure_state(entry: dict[str, Any]) -> bool:
    """Return whether HA is still transitioning the entry after a flow."""
    return entry.get("state") in _TRANSIENT_RECONFIGURE_STATES


def _raise_identity_mismatch(
    entry_id: str,
    message: str,
    *,
    before: dict[str, Any],
    after: dict[str, Any],
    expected: dict[str, Any],
) -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            message,
            suggestions=[
                "Do not retry automatically; inspect the config, device, and "
                "entity registries before attempting another reconfiguration.",
            ],
            context={
                "entry_id": entry_id,
                "expected_identity": expected,
                "before_identity": before,
                "after_identity": after,
            },
        )
    )


def _raise_post_commit_verification_error(
    error: ToolError,
    *,
    entry_id: str,
    domain: str,
    rollback_metadata: dict[str, Any],
) -> NoReturn:
    """Preserve rollback guidance when a committed change fails verification."""
    try:
        payload = json.loads(str(error))
    except (TypeError, ValueError):
        payload = create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Reconfiguration was applied but could not be verified",
            details=str(error),
        )
    if not isinstance(payload, dict):
        payload = create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Reconfiguration was applied but could not be verified",
            details=str(error),
        )

    payload["status"] = (
        payload.get("status")
        if payload.get("status") in {"applied_but_incomplete", "applied_but_unverified"}
        else "applied_but_unverified"
    )
    payload["entry_id"] = entry_id
    payload["domain"] = domain
    payload["rollback"] = rollback_metadata
    raise_tool_error(payload)


# 15 helpers that use Config Entry Flow API (Issue #324).
SUPPORTED_HELPERS = Literal[
    "template",
    "group",
    "utility_meter",
    "derivative",
    "min_max",
    "threshold",
    "integration",
    "statistics",
    "trend",
    "random",
    "filter",
    "tod",
    "generic_thermostat",
    "switch_as_x",
    "generic_hygrostat",
]

# Value-set form of SUPPORTED_HELPERS for runtime routing checks.
# Exported for import by tools_config_helpers.ha_config_set_helper.
FLOW_HELPER_TYPES: frozenset[str] = frozenset(
    {
        "template",
        "group",
        "utility_meter",
        "derivative",
        "min_max",
        "threshold",
        "integration",
        "statistics",
        "trend",
        "random",
        "filter",
        "tod",
        "generic_thermostat",
        "switch_as_x",
        "generic_hygrostat",
    }
)


# ---------------------------------------------------------------------------
# Module-level flow machinery
#
# These functions are shared by the unified ha_config_set_helper tool in
# tools_config_helpers.py. They take a client instance as an explicit
# parameter so the same logic can be used from any caller.
# ---------------------------------------------------------------------------


async def set_config_subentry(
    client: Any,
    entry_id: str,
    subentry_type: str,
    config_dict: dict[str, Any],
    *,
    subentry_id: str | None = None,
    show_advanced_options: bool | None = None,
) -> dict[str, Any]:
    """Create or reconfigure a config subentry via its flow.

    Presence of ``subentry_id`` is the discriminator: omitted creates a new
    subentry, provided reconfigures that existing subentry.
    ``show_advanced_options`` is a no-op on HA 2026.6+ and kept only for older
    HA versions pending removal before HA 2027.6.
    """
    flow_result = await client.start_config_subentry_flow(
        entry_id,
        subentry_type,
        subentry_id=subentry_id,
        show_advanced_options=show_advanced_options,
    )
    flow_id = flow_result.get("flow_id")

    if not flow_id:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Failed to start config subentry flow",
                suggestions=[
                    "Use ha_get_integration(include_subentries=True) to confirm "
                    "the parent entry and available subentry metadata.",
                ],
                context={
                    "entry_id": entry_id,
                    "subentry_type": subentry_type,
                    "subentry_id": subentry_id,
                    "details": flow_result,
                },
            )
        )

    try:
        result = await _handle_config_subentry_flow_steps(
            client,
            flow_id,
            flow_result,
            config_dict,
            is_reconfigure=subentry_id is not None,
        )
    except asyncio.CancelledError:
        await _abort_subentry_flow_best_effort(client, flow_id)
        raise
    except Exception as flow_error:
        payload: dict[str, Any] = {}
        if isinstance(flow_error, ToolError):
            try:
                parsed_payload = json.loads(str(flow_error))
            except (TypeError, ValueError):
                parsed_payload = {}
            if isinstance(parsed_payload, dict):
                payload = parsed_payload
        post_commit_status = payload.get("status") in {
            "applied_but_incomplete",
            "applied_but_unverified",
        }
        if payload.get("flow_budget_exhausted") or not post_commit_status:
            await _abort_subentry_flow_best_effort(client, flow_id)
        raise

    response = {
        "success": True,
        "entry_id": entry_id,
        "subentry_type": subentry_type,
        "subentry_id": subentry_id,
        "operation": result["operation"],
        "flow_result": result["flow_result"],
        "message": f"Config subentry {result['operation']} successfully",
    }
    if result.get("warnings"):
        response["warnings"] = result["warnings"]
    return response


async def get_user_step_field_names(client: Any, helper_type: str) -> set[str] | None:
    """Return field names in the user-step form schema for ``helper_type``.

    Starts a config flow, peeks at the initial step's ``data_schema``,
    and immediately aborts the flow. Used to decide whether to fold the
    top-level ``name`` parameter into the form payload — some helpers
    (e.g. ``switch_as_x``) take their entity name from the source switch
    and reject ``name`` as an extra key.

    Returns:
        A set of field names if the initial step is a form. ``None`` if
        the flow type is not introspectable from the top step (menu or
        unexpected) — callers should fall back to the legacy behaviour
        in that case to avoid regressing menu helpers (template, group).
        Also returns ``None`` if the introspection itself fails; the
        subsequent real flow will surface the error in context.
    """
    flow_id = None
    try:
        flow_result = await client.start_config_flow(helper_type)
        flow_id = flow_result.get("flow_id")
        if flow_result.get("type") != _FlowType.FORM:
            return None
        return _extract_schema_field_names(flow_result.get("data_schema"))
    except Exception as e:
        logger.debug(f"Schema introspection failed for {helper_type}: {e}")
        return None
    finally:
        if flow_id:
            try:
                await asyncio.wait_for(client.abort_config_flow(flow_id), timeout=5.0)
            except Exception as abort_err:
                logger.warning(
                    f"Failed to abort introspection flow {flow_id}: {abort_err}"
                )


async def update_config_entry_options(
    client: Any,
    entry_id: str,
    config_dict: dict[str, Any],
    *,
    expected_domain: str | None = None,
    noun: str = "integration",
) -> dict[str, Any]:
    """Update an existing config entry via its options flow.

    When ``expected_domain`` is provided, verifies the entry's domain matches
    it first (the helper path passes the helper_type; the generic
    ``ha_set_integration`` path passes ``None`` to accept any domain). Starts
    an options flow, walks the flow steps, and returns the result. Aborts the
    flow on error. ``noun`` only affects response wording.
    """
    config_entry = await client.get_config_entry(entry_id)
    actual_domain = config_entry.get("domain")
    if expected_domain is not None and actual_domain != expected_domain:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"entry_id '{entry_id}' belongs to domain '{actual_domain}', not '{expected_domain}'",
                suggestions=[
                    f"Use ha_get_integration(domain='{expected_domain}') to find valid entry IDs",
                ],
                context={
                    "entry_id": entry_id,
                    "expected": expected_domain,
                    "actual": actual_domain,
                },
            )
        )

    flow_result = await client.start_options_flow(entry_id)
    flow_id = flow_result.get("flow_id")

    if not flow_id:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Failed to start options flow",
                suggestions=[
                    "Check that the entry supports options (supports_options=true)"
                ],
                context={"entry_id": entry_id, "details": flow_result},
            )
        )

    try:
        result = await _handle_flow_steps(
            client,
            flow_id,
            flow_result,
            config_dict,
            submit_fn=client.submit_options_flow_step,
            helper_type=expected_domain,
        )
    except Exception:
        try:
            await asyncio.wait_for(client.abort_options_flow(flow_id), timeout=5.0)
        except Exception as abort_err:
            logger.warning(
                f"Failed to abort options flow {flow_id} after error: {abort_err}"
            )
        raise

    entry = result["entry"].get("result", {})
    response = {
        "success": True,
        "entry_id": entry_id,
        "title": entry.get("title"),
        "domain": actual_domain,
        "message": f"{actual_domain} {noun} updated successfully",
        "updated": True,
    }
    if result.get("warnings"):
        response["warnings"] = result["warnings"]
    return response


def _verify_reconfigure_identity_fields(
    *,
    entry_id: str,
    before: dict[str, Any],
    before_identity: dict[str, Any],
    after: dict[str, Any],
    after_identity: dict[str, Any],
    expected_identity: dict[str, Any],
) -> None:
    """Verify the identity anchors that must survive a reconfigure flow."""
    before_unique_id = before.get("unique_id")
    after_unique_id = after.get("unique_id")
    if before_unique_id is not None and after_unique_id != before_unique_id:
        _raise_identity_mismatch(
            entry_id,
            "Reconfigure flow changed the original entry unique_id",
            before=before_identity,
            after=after_identity,
            expected=expected_identity,
        )

    expected_unique_id = expected_identity.get("unique_id")
    if expected_unique_id is not None and after_unique_id != expected_unique_id:
        _raise_identity_mismatch(
            entry_id,
            "Reconfigure result does not match expected unique_id",
            before=before_identity,
            after=after_identity,
            expected=expected_identity,
        )

    before_device_ids = set(before_identity.get("device_ids", []))
    after_device_ids = set(after_identity.get("device_ids", []))
    expected_device_id = expected_identity.get("device_id")
    if (
        before_device_ids
        and after_identity.get("device_registry_available")
        and after_device_ids != before_device_ids
    ):
        _raise_identity_mismatch(
            entry_id,
            "Reconfigure flow changed the associated device_id",
            before=before_identity,
            after=after_identity,
            expected=expected_identity,
        )
    if expected_device_id is not None and expected_device_id not in after_device_ids:
        _raise_identity_mismatch(
            entry_id,
            "Reconfigure result does not match expected device_id",
            before=before_identity,
            after=after_identity,
            expected=expected_identity,
        )

    before_entity_ids = set(before_identity.get("entity_ids", []))
    after_entity_ids = set(after_identity.get("entity_ids", []))
    expected_entity_ids = set(expected_identity.get("entity_ids", []))
    if (
        before_entity_ids
        and after_identity.get("entity_registry_available")
        and after_entity_ids != before_entity_ids
    ):
        _raise_identity_mismatch(
            entry_id,
            "Reconfigure flow changed the associated entity set",
            before=before_identity,
            after=after_identity,
            expected=expected_identity,
        )
    if expected_entity_ids and after_entity_ids != expected_entity_ids:
        _raise_identity_mismatch(
            entry_id,
            "Reconfigure result does not match expected entity_ids",
            before=before_identity,
            after=after_identity,
            expected=expected_identity,
        )

    expected_mac = expected_identity.get("mac")
    before_macs = set(before_identity.get("macs", []))
    after_macs = set(after_identity.get("macs", []))
    if before_macs and after_macs and before_macs != after_macs:
        _raise_identity_mismatch(
            entry_id,
            "Reconfigure flow changed the associated MAC/IEEE identity",
            before=before_identity,
            after=after_identity,
            expected=expected_identity,
        )
    if expected_mac is not None and (
        not after_macs or _normalise_identity_value(expected_mac) not in after_macs
    ):
        _raise_identity_mismatch(
            entry_id,
            "Reconfigure result does not match expected MAC",
            before=before_identity,
            after=after_identity,
            expected=expected_identity,
        )


async def _verify_reconfigured_entry(
    client: Any,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    entry_id: str,
    domain: str,
    before_identity: dict[str, Any],
    after_identity: dict[str, Any],
    expected_identity: dict[str, Any],
) -> dict[str, Any]:
    """Verify identity preservation and absence of duplicate config entries."""
    if after.get("entry_id") != entry_id or after.get("domain") != domain:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Reconfigure flow completed but the original config entry could not be verified",
                suggestions=[
                    "Inspect ha_get_integration() before retrying; do not create a duplicate entry.",
                ],
                context={
                    "entry_id": entry_id,
                    "expected_domain": domain,
                    "verified_entry": after,
                },
            )
        )

    _verify_reconfigure_identity_fields(
        entry_id=entry_id,
        before=before,
        before_identity=before_identity,
        after=after,
        after_identity=after_identity,
        expected_identity=expected_identity,
    )

    before_unique_id = before.get("unique_id")
    after_unique_id = after.get("unique_id")
    before_device_ids = set(before_identity.get("device_ids", []))
    after_device_ids = set(after_identity.get("device_ids", []))
    expected_device_id = expected_identity.get("device_id")
    before_entity_ids = set(before_identity.get("entity_ids", []))
    after_entity_ids = set(after_identity.get("entity_ids", []))
    expected_entity_ids = set(expected_identity.get("entity_ids", []))
    expected_mac = expected_identity.get("mac")
    before_macs = set(before_identity.get("macs", []))
    after_macs = set(after_identity.get("macs", []))

    after_related_entry_ids = await _same_domain_related_entry_ids(
        client,
        after_identity,
        entry_id=entry_id,
        domain=domain,
    )
    if after_related_entry_ids:
        _raise_identity_mismatch(
            entry_id,
            "Reconfigure flow left an incompatible related config entry in the same "
            "domain",
            before=before_identity,
            after=after_identity,
            expected=expected_identity,
        )

    identity_unique_ids = {
        value for value in (before_unique_id, after_unique_id) if value is not None
    }
    entries = await _validated_config_entry_rows(client, entry_id)
    same_identity_entries = [
        entry
        for entry in entries
        if entry.get("domain") == domain
        and (
            entry.get("entry_id") == entry_id
            or entry.get("unique_id") in identity_unique_ids
        )
    ]
    if len(same_identity_entries) != 1:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Reconfigure flow verification found duplicate or missing config entries",
                suggestions=[
                    "Do not create another entry; inspect ha_get_integration() and the "
                    "Home Assistant config-entry registry first.",
                ],
                context={
                    "entry_id": entry_id,
                    "domain": domain,
                    "matching_entry_ids": [
                        item.get("entry_id") for item in same_identity_entries
                    ],
                },
            )
        )

    device_identity_verified = bool(
        before_identity.get("device_registry_available")
        and after_identity.get("device_registry_available")
        and after_device_ids == before_device_ids
        and (not expected_device_id or expected_device_id in after_device_ids)
    )
    entity_identity_verified = bool(
        before_identity.get("entity_registry_available")
        and after_identity.get("entity_registry_available")
        and after_entity_ids == before_entity_ids
        and (not expected_entity_ids or after_entity_ids == expected_entity_ids)
    )
    if expected_mac is not None:
        mac_identity_verified = bool(
            after_identity.get("device_registry_available")
            and _normalise_identity_value(expected_mac) in after_macs
        )
    elif before_macs:
        mac_identity_verified = before_macs == after_macs
    else:
        mac_identity_verified = True

    return {
        "entry_state": after.get("state"),
        "operational_state_verified": after.get("state") == "loaded",
        "entry_id_preserved": True,
        "domain_preserved": True,
        "unique_id_preserved": (
            before_unique_id == after_unique_id
            if before_unique_id is not None and after_unique_id is not None
            else None
        ),
        "unique_id_verification": (
            _verification_state(
                before_unique_id is not None,
                after_unique_id is not None,
            )
        ),
        "device_id_verification": _verification_state(
            bool(before_device_ids),
            bool(after_identity.get("device_registry_available")),
        ),
        "entity_verification": _verification_state(
            bool(before_entity_ids),
            bool(after_identity.get("entity_registry_available")),
        ),
        "identity_verification": (
            "complete"
            if (
                device_identity_verified
                and entity_identity_verified
                and mac_identity_verified
            )
            else "partial"
        ),
        "duplicate_entry_created": False if identity_unique_ids else None,
        "duplicate_verification": (
            "complete" if identity_unique_ids else "limited_without_unique_id"
        ),
    }


def _build_reconfigure_flow_config(
    entry_id: str,
    *,
    config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate and copy the integration-defined reconfigure flow values."""
    if config is not None and not isinstance(config, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "config must be an object containing the integration's flow values",
                context={"entry_id": entry_id},
            )
        )
    return dict(config or {})


async def prepare_reconfigure_request(
    client: Any,
    entry_id: str,
    *,
    config: dict[str, Any] | None = None,
    expected_identity: dict[str, Any] | None = None,
) -> PreparedReconfigure:
    """Prepare and validate one reconfigure request for all execution paths."""
    validated_entry_id = validate_identifier_not_empty(
        entry_id,
        "entry_id",
        suggestions=["Use ha_get_integration() to find valid config entry IDs"],
    )
    flow_config = _build_reconfigure_flow_config(validated_entry_id, config=config)
    entry = await client.get_config_entry(validated_entry_id)
    domain = entry.get("domain")
    if not isinstance(domain, str) or not domain:
        raise_tool_error(
            create_error_response(
                ErrorCode.INTERNAL_UNEXPECTED,
                "Home Assistant returned a config entry without a valid domain",
                context={"entry_id": validated_entry_id, "entry": entry},
            )
        )
    if not entry.get("supports_reconfigure", False):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Integration '{domain}' does not support the official reconfigure flow",
                suggestions=[
                    "Use ha_get_integration(entry_id=...) to inspect the entry; "
                    "only integrations implementing async_step_reconfigure can be changed this way.",
                ],
                context={
                    "entry_id": validated_entry_id,
                    "domain": domain,
                    "supports_reconfigure": False,
                },
            )
        )

    expected = {
        "device_id": None,
        "unique_id": None,
        "mac": None,
        "entity_ids": [],
        **(expected_identity or {}),
    }
    identity = await _validate_reconfigure_identity_and_duplicates(
        client,
        entry,
        entry_id=validated_entry_id,
        domain=domain,
        expected_identity=expected,
        prepared_identity=None,
    )
    if not _has_reconfigure_identity_anchor(identity, expected):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Cannot safely reconfigure an entry without an identity anchor",
                suggestions=[
                    "Provide expected_device_id, expected_unique_id, expected_mac, "
                    "or expected_entity_ids, or choose an entry with stable registry identity."
                ],
                context={
                    "entry_id": validated_entry_id,
                    "domain": domain,
                    "available_identity": {
                        key: identity.get(key)
                        for key in ("unique_id", "device_ids", "entity_ids", "macs")
                    },
                    "expected_identity": expected,
                },
            )
        )
    return PreparedReconfigure(
        entry_id=validated_entry_id,
        entry=entry,
        flow_config=flow_config,
        identity=identity,
        expected_identity=expected,
    )


async def _validate_reconfigure_identity_and_duplicates(
    client: Any,
    entry: dict[str, Any],
    *,
    entry_id: str,
    domain: str,
    expected_identity: dict[str, Any],
    prepared_identity: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate pre-flow identity anchors and same-domain duplicates."""
    before_identity = (
        prepared_identity
        if prepared_identity is not None
        else await _collect_reconfigure_identity(client, entry, entry_id)
    )
    checks = (
        (
            expected_identity.get("device_id"),
            before_identity["device_ids"],
            "device_id",
            "The entry does not match expected device_id before reconfigure",
        ),
        (
            expected_identity.get("unique_id"),
            [entry["unique_id"]] if entry.get("unique_id") is not None else [],
            "unique_id",
            "The entry does not match expected unique_id before reconfigure",
        ),
        (
            expected_identity.get("mac"),
            before_identity["macs"],
            "mac",
            "The entry does not match expected MAC before reconfigure",
        ),
    )
    for expected, available, key, message in checks:
        if expected is None:
            continue
        if not available:
            _raise_identity_mismatch(
                entry_id,
                f"Cannot compare expected {key}: identity evidence is unavailable",
                before=before_identity,
                after=before_identity,
                expected=expected_identity,
            )
        normalised = (
            {_normalise_identity_value(value) for value in available}
            if key == "mac"
            else set(available)
        )
        if (
            _normalise_identity_value(expected) if key == "mac" else expected
        ) not in normalised:
            _raise_identity_mismatch(
                entry_id,
                message,
                before=before_identity,
                after=before_identity,
                expected=expected_identity,
            )
    expected_entities = expected_identity["entity_ids"]
    if expected_entities and not before_identity.get("entity_registry_available"):
        _raise_identity_mismatch(
            entry_id,
            "Cannot compare expected entity_ids: entity registry is unavailable",
            before=before_identity,
            after=before_identity,
            expected=expected_identity,
        )
    if expected_entities and set(expected_entities) != set(
        before_identity["entity_ids"]
    ):
        _raise_identity_mismatch(
            entry_id,
            "The entry does not match expected entity_ids before reconfigure",
            before=before_identity,
            after=before_identity,
            expected=expected_identity,
        )
    expected_mac = expected_identity.get("mac")
    if expected_mac is not None and not before_identity.get(
        "device_registry_available"
    ):
        _raise_identity_mismatch(
            entry_id,
            "Cannot compare expected MAC: device registry is unavailable",
            before=before_identity,
            after=before_identity,
            expected=expected_identity,
        )
    related_entry_ids = await _same_domain_related_entry_ids(
        client,
        before_identity,
        entry_id=entry_id,
        domain=domain,
    )
    if related_entry_ids:
        _raise_identity_mismatch(
            entry_id,
            "The entry has a registered device shared with a duplicate or incompatible "
            "related config entry in the same domain",
            before=before_identity,
            after=before_identity,
            expected=expected_identity,
        )
    return before_identity


async def _run_reconfigure_flow(
    client: Any,
    *,
    domain: str,
    entry_id: str,
    flow_config: dict[str, Any],
    rollback_metadata: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """Start and walk the official reconfigure flow."""
    flow_result = await client.start_reconfigure_flow(domain, entry_id)
    flow_id = flow_result.get("flow_id")
    if not flow_id:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Failed to start the integration reconfigure flow",
                suggestions=[
                    "Confirm that the entry still exists and supports reconfigure",
                ],
                context={
                    "entry_id": entry_id,
                    "domain": domain,
                    "details": flow_result,
                },
            )
        )
    try:
        result = await _handle_flow_steps(
            client,
            flow_id,
            flow_result,
            flow_config,
            helper_type=domain,
            is_reconfigure=True,
        )
    except asyncio.CancelledError:
        await _abort_flow_best_effort(client, flow_id)
        raise
    except ToolError as flow_error:
        try:
            payload = json.loads(str(flow_error))
        except (TypeError, ValueError):
            payload = {}
        if payload.get("status") in {
            "applied_but_incomplete",
            "applied_but_unverified",
        }:
            if payload.get("flow_budget_exhausted"):
                await _abort_flow_best_effort(client, flow_id)
            _raise_post_commit_verification_error(
                flow_error,
                entry_id=entry_id,
                domain=domain,
                rollback_metadata=rollback_metadata,
            )
        try:
            await asyncio.wait_for(client.abort_config_flow(flow_id), timeout=5.0)
        except Exception as abort_err:
            logger.warning(
                "Failed to abort reconfigure flow %s: %s", flow_id, abort_err
            )
        raise
    except Exception:
        try:
            await asyncio.wait_for(client.abort_config_flow(flow_id), timeout=5.0)
        except Exception as abort_err:
            logger.warning(
                "Failed to abort reconfigure flow %s: %s", flow_id, abort_err
            )
        raise
    return flow_id, result


async def _verify_reconfigure_result(
    client: Any,
    *,
    before: dict[str, Any],
    entry_id: str,
    domain: str,
    before_identity: dict[str, Any],
    expected_identity: dict[str, Any],
    rollback_metadata: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read back and verify the committed config entry with bounded retries."""
    after: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    last_verification_error: Exception | None = None
    for attempt in range(3):
        try:
            current_after = await client.get_config_entry(entry_id)
            after = current_after
            after_identity = await _collect_reconfigure_identity(
                client, current_after, entry_id
            )
            verification = await _verify_reconfigured_entry(
                client,
                before,
                current_after,
                entry_id=entry_id,
                domain=domain,
                before_identity=before_identity,
                after_identity=after_identity,
                expected_identity=expected_identity,
            )
            if _is_transient_reconfigure_state(current_after) and attempt < 2:
                logger.info(
                    "Reconfigure verification observed transient state %s; retrying",
                    current_after.get("state"),
                )
                await asyncio.sleep(0.25 * (attempt + 1))
                continue
            break
        except ToolError as verification_error:
            _raise_post_commit_verification_error(
                verification_error,
                entry_id=entry_id,
                domain=domain,
                rollback_metadata=rollback_metadata,
            )
        except Exception as err:
            last_verification_error = err
            logger.warning(
                "Reconfigure verification attempt %d failed (%s): %s",
                attempt + 1,
                type(err).__name__,
                err,
                exc_info=True,
            )
            if attempt < 2:
                await asyncio.sleep(0.25 * (attempt + 1))
    if after is None or verification is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Reconfiguration was submitted but the applied state could not "
                "be verified",
                suggestions=[
                    "Do not retry automatically. Inspect Home Assistant and the "
                    "device, then verify the entry before attempting rollback.",
                ],
                context={
                    "entry_id": entry_id,
                    "domain": domain,
                    "status": "applied_but_unverified",
                    "error": str(last_verification_error),
                    "rollback": rollback_metadata,
                },
            )
        )
    return after, verification


async def reconfigure_config_entry(
    client: Any,
    entry_id: str,
    *,
    config: dict[str, Any] | None = None,
    expected_device_id: str | None = None,
    expected_unique_id: str | None = None,
    expected_mac: str | None = None,
    expected_entity_ids: list[str] | None = None,
    prepared: PreparedReconfigure | None = None,
) -> dict[str, Any]:
    """Reconfigure an existing integration through HA's official flow.

    The operation is intentionally generic: integrations opt in by exposing
    ``async_step_reconfigure`` and decide how the submitted values are
    validated. The existing config entry is never deleted or recreated.
    """
    expected_identity: dict[str, Any] = {
        "device_id": expected_device_id,
        "unique_id": expected_unique_id,
        "mac": expected_mac,
        "entity_ids": list(expected_entity_ids or []),
    }
    if prepared is None:
        prepared = await prepare_reconfigure_request(
            client,
            entry_id,
            config=config,
            expected_identity=expected_identity,
        )
    else:
        validated_entry_id = validate_identifier_not_empty(entry_id, "entry_id")
        if validated_entry_id != prepared.entry_id:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Prepared reconfigure state belongs to a different entry_id",
                    context={
                        "entry_id": validated_entry_id,
                        "prepared_entry_id": prepared.entry_id,
                    },
                )
            )
        if config is not None and config != prepared.flow_config:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "config does not match the prepared reconfigure request",
                    context={"entry_id": prepared.entry_id},
                )
            )
        supplied_identity = {
            "device_id": expected_device_id,
            "unique_id": expected_unique_id,
            "mac": expected_mac,
            "entity_ids": list(expected_entity_ids or []),
        }
        if (
            expected_device_id is not None
            or expected_unique_id is not None
            or expected_mac is not None
            or expected_entity_ids is not None
        ) and supplied_identity != prepared.expected_identity:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Identity anchors do not match the prepared reconfigure request",
                    context={"entry_id": prepared.entry_id},
                )
            )

    entry_id = prepared.entry_id
    before = prepared.entry
    flow_config = prepared.flow_config
    domain = before.get("domain")
    if not isinstance(domain, str) or not domain:
        raise_tool_error(
            create_error_response(
                ErrorCode.INTERNAL_UNEXPECTED,
                "Home Assistant returned a config entry without a valid domain",
                context={"entry_id": entry_id, "entry": before},
            )
        )

    rollback_metadata = build_reconfigure_rollback_metadata(entry_id, domain)
    before_identity = prepared.identity

    _, result = await _run_reconfigure_flow(
        client,
        domain=domain,
        entry_id=entry_id,
        flow_config=flow_config,
        rollback_metadata=rollback_metadata,
    )
    after, verification = await _verify_reconfigure_result(
        client,
        before=before,
        entry_id=entry_id,
        domain=domain,
        before_identity=before_identity,
        expected_identity=prepared.expected_identity,
        rollback_metadata=rollback_metadata,
    )

    response: dict[str, Any] = {
        "success": True,
        "status": (
            "applied_and_verified"
            if (
                verification.get("identity_verification") == "complete"
                and verification.get("operational_state_verified") is True
            )
            else "applied_but_unverified"
        ),
        "operation": "reconfigured",
        "entry_id": entry_id,
        "domain": domain,
        "title": after.get("title"),
        "message": f"{domain} integration reconfigured successfully",
        "verification": verification,
        "rollback_strategy": rollback_metadata["strategy"],
        "rollback_automatic": rollback_metadata["automatic"],
        "rollback_operator_action_required": rollback_metadata[
            "operator_action_required"
        ],
        "rollback_manual_required": rollback_metadata["manual_required"],
        "rollback_reference": rollback_metadata,
        "target_config": flow_config,
    }
    if result.get("warnings"):
        response["warnings"] = result["warnings"]
    return response


async def update_flow_helper(
    client: Any,
    helper_type: str,
    config_dict: dict[str, Any],
    entry_id: str,
) -> dict[str, Any]:
    """Update an existing flow-based helper via its options flow.

    Verifies the entry domain matches helper_type, starts an options flow,
    walks the flow steps, and returns the result. Aborts the flow on error.
    """
    return await update_config_entry_options(
        client,
        entry_id,
        config_dict,
        expected_domain=helper_type,
        noun="helper",
    )


async def create_config_entry(
    client: Any,
    domain: str,
    config_dict: dict[str, Any],
    *,
    noun: str = "integration",
) -> dict[str, Any]:
    """Create a config entry by driving ``domain``'s config flow.

    Starts a config flow, walks the flow steps (menus and multi-step forms),
    and returns the result. Aborts the flow on error. ``noun`` only affects
    response wording.
    """
    flow_result = await client.start_config_flow(domain)
    flow_id = flow_result.get("flow_id")

    if not flow_id:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Failed to start config flow",
                suggestions=[
                    f"Check that the {noun} domain exists and Home Assistant is reachable"
                ],
                context={"domain": domain, "details": flow_result},
            )
        )

    try:
        result = await _handle_flow_steps(
            client,
            flow_id,
            flow_result,
            config_dict,
            helper_type=domain,
        )
    except Exception:
        try:
            await asyncio.wait_for(client.abort_config_flow(flow_id), timeout=5.0)
        except Exception as abort_err:
            logger.warning(
                f"Failed to abort config flow {flow_id} after error: {abort_err}"
            )
        raise

    entry = result["entry"].get("result", {})
    response = {
        "success": True,
        "entry_id": entry.get("entry_id"),
        "title": entry.get("title"),
        "domain": domain,
        "message": f"{domain} {noun} created successfully",
    }
    if result.get("warnings"):
        response["warnings"] = result["warnings"]
    return response


async def create_flow_helper(
    client: Any,
    helper_type: str,
    config_dict: dict[str, Any],
) -> dict[str, Any]:
    """Create a new flow-based helper via the config flow.

    Starts a config flow, walks the flow steps, and returns the result.
    Aborts the flow on error.
    """
    return await create_config_entry(client, helper_type, config_dict, noun="helper")
