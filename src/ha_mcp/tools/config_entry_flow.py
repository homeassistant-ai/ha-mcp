"""
Config Entry Flow API machinery for Home Assistant MCP server.

This module provides the shared machinery for creating and updating
config-entry-based helpers (template, group, utility_meter, etc.) via the
Config Entry Flow API.

The create/update entry point is the unified ha_config_set_helper tool in
tools_config_helpers.py, which routes to create_flow_helper / update_flow_helper
for the 17 helper types listed in FLOW_HELPER_TYPES.

The same flow walkers drive every other config-entry surface, not just
helpers: ``ha_set_integration`` creates entries for arbitrary domains through
``create_config_entry``, edits them through ``update_config_entry_options``,
and changes their connection settings through ``reconfigure_config_entry``
(whose read-only preflight, ``prepare_reconfigure_request``, and the
``PreparedReconfigure`` it returns are imported by ``tools_integrations``);
``ha_config_set_helper(helper_type="config_subentry")`` drives subentry flows
through ``set_config_subentry``.

The step machinery those entry points drive lives in three sibling modules,
imported in one direction only (menu <- form <- walker <- here):

- ``config_entry_flow_menu``: menu selection keys and menu-step handling
- ``config_entry_flow_form``: form-step schema consumption and reuse tracking
- ``config_entry_flow_walker``: step submission, HA error translation, flow
  introspection, and the two flow walkers
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, NoReturn

from fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from ..redaction import sentinel_option_keys
from .component_config_entries import (
    fetch_domain_unique_ids,
)
from .config_entry_flow_form import _extract_schema_field_names
from .config_entry_flow_walker import (
    POST_COMMIT_STATUSES,
    ReconfigureStatus,
    _FlowType,
    _handle_config_subentry_flow_steps,
    _handle_flow_steps,
)
from .config_entry_identity import (
    ReconfigureIdentity,
    _classify_related_entries,
    _collect_reconfigure_identity,
    _has_reconfigure_identity_anchor,
    _normalise_identity_value,
    _raise_identity_mismatch,
    _validated_config_entry_rows,
    _verification_state,
    _verify_reconfigure_identity_fields,
    cross_domain_warnings,
)
from .config_entry_reload_watch import (
    _RELOAD_SETTLE_TIMEOUT,
    _is_transient_reconfigure_state,
    _observe_reload_settled,
    _subscribe_entry_changes,
)
from .helpers import raise_tool_error, validate_identifier_not_empty

logger = logging.getLogger(__name__)

_VERIFICATION_ATTEMPTS = 5
_VERIFICATION_BACKOFF_SECONDS = (0.25, 0.5, 1.0, 2.0)


@dataclass(frozen=True)
class PreparedReconfigure:
    """Validated state shared by reconfigure preflight and confirmed apply."""

    entry_id: str
    entry: dict[str, Any]
    flow_config: dict[str, Any]
    identity: ReconfigureIdentity
    expected_identity: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def build_reconfigure_rollback_metadata(
    entry_id: str,
    domain: str,
) -> dict[str, Any]:
    """Describe the manual rollback path for an existing config entry.

    The snapshot the auto-backup layer captures is Home Assistant's config
    entry fragment (``config_entries/get`` over WebSocket, which serializes
    ``ConfigEntry.as_json_fragment``). That fragment carries no ``data`` key,
    so the entry's connection settings are not in it: the snapshot is audit
    evidence, not an automatic endpoint rollback. An operator must repeat the
    official reconfigure flow with the known previous values.
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


def _raise_post_commit_verification_error(
    error: ToolError,
    *,
    entry_id: str,
    domain: str,
    rollback_metadata: dict[str, Any],
) -> NoReturn:
    """Preserve rollback guidance when a committed change fails verification.

    Keeps the raiser's own status when it named one, so a device-swap safety
    violation stays distinguishable from a registry read that timed out.
    """
    payload: Any = None
    try:
        payload = json.loads(str(error))
    except (TypeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        payload = create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Reconfiguration was applied but could not be verified: {error}",
            details=str(error),
        )

    if payload.get("status") not in POST_COMMIT_STATUSES:
        payload["status"] = ReconfigureStatus.APPLIED_BUT_UNVERIFIED
    payload["entry_id"] = entry_id
    payload["domain"] = domain
    payload["rollback"] = rollback_metadata
    raise_tool_error(payload)


def _reject_redaction_sentinels(config_dict: dict[str, Any]) -> None:
    """Reject config values that are redaction placeholders (#2157).

    A caller round-tripping a redacted read back through a flow write would
    overwrite the live credential with the placeholder string. Omitting the
    key keeps the current value, so rejection loses nothing. Active
    regardless of the redact_secrets toggle: a sentinel captured while
    redaction was on must not overwrite a credential after the operator
    turns it off.
    """
    sentinel_keys = sentinel_option_keys(config_dict)
    if sentinel_keys:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "config contains redaction placeholder values for: "
                f"{', '.join(sentinel_keys)}. These came from a redacted "
                "read, not real values — omit these keys to keep the "
                "current values, or submit the real value.",
                context={"parameter": "config"},
            )
        )


# 17 helpers that use Config Entry Flow API (Issue #324, #2187).
# `otp` is the one helper-typed config flow deliberately left out: its confirm
# step demands a live TOTP code derived from the secret, which no flow walker
# can supply. It stays reachable through ha_set_integration(domain="otp").
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
    "history_stats",
    "mold_indicator",
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
        "history_stats",
        "mold_indicator",
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

    The reconfigure branch fails when the flow leaves any supplied config key
    unconsumed, where it previously returned success plus a warning — see
    :func:`_handle_config_subentry_flow_steps` for why. The create branch is
    unchanged.
    """
    _reject_redaction_sentinels(config_dict)
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
        post_commit_status = payload.get("status") in POST_COMMIT_STATUSES
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
    _reject_redaction_sentinels(config_dict)
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


def _build_reconfigure_verification(
    *,
    after: dict[str, Any],
    before_unique_id: Any,
    after_unique_id: Any,
    before_identity: ReconfigureIdentity,
    after_identity: ReconfigureIdentity,
    expected_identity: dict[str, Any],
    scanned_unique_id: bool,
    unique_id_comparable: bool,
    unique_id_anchor_unverifiable: bool,
    cross_domain_related: list[str],
) -> dict[str, Any]:
    """Summarise what the post-commit read-back could and could not confirm.

    Every field here reports a comparison that actually ran. Values that the
    surrounding code has already made unconditional — the entry_id and domain
    it raises on, the duplicate branch that raises rather than reporting — are
    deliberately absent rather than hardcoded ``True``.
    """
    before_device_ids = set(before_identity.device_ids)
    after_device_ids = set(after_identity.device_ids)
    before_entity_ids = set(before_identity.entity_ids)
    after_entity_ids = set(after_identity.entity_ids)
    before_macs = set(before_identity.macs)
    after_macs = set(after_identity.macs)
    expected_device_id = expected_identity.get("device_id")
    expected_entity_ids = set(expected_identity.get("entity_ids") or [])
    expected_mac = expected_identity.get("mac")

    device_identity_verified = after_device_ids == before_device_ids and (
        not expected_device_id or expected_device_id in after_device_ids
    )
    entity_identity_verified = after_entity_ids == before_entity_ids and (
        not expected_entity_ids or after_entity_ids == expected_entity_ids
    )
    if expected_mac is not None:
        mac_identity_verified = _normalise_identity_value(expected_mac) in after_macs
    else:
        mac_identity_verified = before_macs == after_macs

    return {
        "entry_state": after.get("state"),
        # A disabled entry's terminal state is not_loaded, not loaded —
        # _is_transient_reconfigure_state already treats it as settled, so
        # demanding "loaded" here would report every disabled entry's clean
        # reconfigure as unverified.
        "operational_state_verified": after.get("state") == "loaded"
        or bool(after.get("disabled_by") and after.get("state") == "not_loaded"),
        # None, not True: without a readable unique_id nothing was compared,
        # and reporting "preserved" for two unknowns is a false assurance.
        "unique_id_preserved": (
            before_unique_id == after_unique_id if unique_id_comparable else None
        ),
        "unique_id_verification": (
            "anchor_unverifiable_after_change"
            if unique_id_anchor_unverifiable
            else (
                "changed_during_change"
                if before_unique_id is not None
                and after_unique_id is not None
                and before_unique_id != after_unique_id
                else _verification_state(
                    before_unique_id is not None, after_unique_id is not None
                )
            )
            if unique_id_comparable
            # Home Assistant does not expose unique_id; only the ha_mcp_tools
            # custom component can supply it.
            else "unavailable_without_component"
        ),
        "device_id_verification": _verification_state(
            bool(before_device_ids), bool(after_device_ids)
        ),
        "entity_verification": _verification_state(
            bool(before_entity_ids), bool(after_entity_ids)
        ),
        "identity_verification": (
            "complete"
            if (
                device_identity_verified
                and entity_identity_verified
                and mac_identity_verified
                # A caller who pinned expected_unique_id and whose post-commit
                # read failed has NOT had that anchor checked. Reporting
                # "complete" off the device/entity sets alone would pass a
                # verification the caller explicitly asked for and we skipped.
                and not unique_id_anchor_unverifiable
            )
            else "partial"
        ),
        # What the duplicate scan could see. Without a unique_id on either
        # side, only entries sharing the device are checked.
        "duplicate_scan": (
            "unique_id_and_shared_device" if scanned_unique_id else "shared_device_only"
        ),
        "cross_domain_related_entries": cross_domain_related,
    }


async def _verify_reconfigured_entry(
    client: Any,
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    entry_id: str,
    domain: str,
    before_identity: ReconfigureIdentity,
    after_identity: ReconfigureIdentity,
    expected_identity: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
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
                    "status": ReconfigureStatus.APPLIED_IDENTITY_MISMATCH,
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

    # One config-entry list-all serves both the related-entry classification
    # and the duplicate scan below.
    entries = await _validated_config_entry_rows(client, entry_id)
    after_related = await _classify_related_entries(
        client,
        after_identity,
        entry_id=entry_id,
        domain=domain,
        entries=entries,
    )
    if after_related.blocking:
        _raise_identity_mismatch(
            entry_id,
            "Reconfigure flow left a second config entry in the same domain "
            "sharing this entry's device",
            before=before_identity,
            after=after_identity,
            expected=expected_identity,
            status=ReconfigureStatus.APPLIED_IDENTITY_MISMATCH,
        )

    unique_id_comparable = (
        before_identity.unique_id_known and after_identity.unique_id_known
    )
    # An anchor the caller pinned but that we can no longer read post-commit is
    # an unchecked anchor, not a passed one.
    unique_id_anchor_unverifiable = (
        expected_identity.get("unique_id") is not None
        and not after_identity.unique_id_known
    )
    before_unique_id = before_identity.unique_id if unique_id_comparable else None
    after_unique_id = after_identity.unique_id if unique_id_comparable else None
    identity_unique_ids = {
        value for value in (before_unique_id, after_unique_id) if value is not None
    }
    # Home Assistant's config-entry rows carry no unique_id, so comparing
    # entry["unique_id"] against them would never match and the scan would
    # claim a check it never made. One component read maps the whole domain.
    domain_unique_ids = (
        await fetch_domain_unique_ids(client, domain) if identity_unique_ids else None
    )

    def _shares_identity(candidate: dict[str, Any]) -> bool:
        candidate_id = candidate.get("entry_id")
        if candidate_id == entry_id:
            return True
        if domain_unique_ids is None or not isinstance(candidate_id, str):
            return False
        candidate_unique_id = domain_unique_ids.get(candidate_id)
        return candidate_unique_id is not None and (
            candidate_unique_id in identity_unique_ids
        )

    same_identity_entries = [
        entry
        for entry in entries
        if entry.get("domain") == domain and _shares_identity(entry)
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
                    "status": ReconfigureStatus.APPLIED_IDENTITY_MISMATCH,
                    "matching_entry_ids": [
                        item.get("entry_id") for item in same_identity_entries
                    ],
                },
            )
        )

    verification = _build_reconfigure_verification(
        after=after,
        before_unique_id=before_unique_id,
        after_unique_id=after_unique_id,
        before_identity=before_identity,
        after_identity=after_identity,
        expected_identity=expected_identity,
        # Only claim a unique_id scan when one actually ran.
        scanned_unique_id=bool(identity_unique_ids) and domain_unique_ids is not None,
        unique_id_comparable=unique_id_comparable,
        unique_id_anchor_unverifiable=unique_id_anchor_unverifiable,
        cross_domain_related=after_related.cross_domain,
    )
    return verification, cross_domain_warnings(after_related.cross_domain)


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
    """Prepare and validate one reconfigure request for all execution paths.

    Everything here is read-only: the entry exists, it implements
    ``async_step_reconfigure``, the caller's identity anchors match what the
    registries currently say, no second entry in the same domain shares the
    device, and some identity anchor exists at all. The integration's own
    reconfigure form is NOT consulted — the flow is never started — so the
    keys in ``config`` are still unvalidated when this returns.
    """
    validated_entry_id = validate_identifier_not_empty(
        entry_id,
        "entry_id",
        suggestions=["Use ha_get_integration() to find valid config entry IDs"],
    )
    flow_config = _build_reconfigure_flow_config(validated_entry_id, config=config)
    # Fourth path that writes caller-supplied config into a flow, alongside
    # create_config_entry, update_config_entry_options and set_config_subentry.
    # Without this a caller round-tripping a redacted read would write the
    # placeholder string over a live credential (#2157).
    _reject_redaction_sentinels(flow_config)
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
    identity, warnings = await _validate_reconfigure_identity_and_duplicates(
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
                    "Provide expected_device_id, expected_mac or "
                    "expected_entity_ids, or choose an entry with stable "
                    "registry identity. (expected_unique_id needs the "
                    "ha_mcp_tools custom component: Home Assistant does not "
                    "expose a config entry's unique_id.)"
                ],
                context={
                    "entry_id": validated_entry_id,
                    "domain": domain,
                    "available_identity": {
                        "unique_id": identity.unique_id,
                        "device_ids": identity.device_ids,
                        "entity_ids": identity.entity_ids,
                        "macs": identity.macs,
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
        warnings=warnings,
    )


async def _validate_reconfigure_identity_and_duplicates(
    client: Any,
    entry: dict[str, Any],
    *,
    entry_id: str,
    domain: str,
    expected_identity: dict[str, Any],
    prepared_identity: ReconfigureIdentity | None,
) -> tuple[ReconfigureIdentity, list[str]]:
    """Validate pre-flow identity anchors and same-domain duplicates."""
    before_identity = (
        prepared_identity
        if prepared_identity is not None
        else await _collect_reconfigure_identity(client, entry, entry_id)
    )
    checks = (
        (
            expected_identity.get("device_id"),
            before_identity.device_ids,
            "device_id",
            "The entry does not match expected device_id before reconfigure",
        ),
        (
            expected_identity.get("unique_id"),
            (
                [before_identity.unique_id]
                if before_identity.unique_id_known
                and before_identity.unique_id is not None
                else []
            ),
            "unique_id",
            "The entry does not match expected unique_id before reconfigure",
        ),
        (
            expected_identity.get("mac"),
            before_identity.macs,
            "mac",
            "The entry does not match expected MAC before reconfigure",
        ),
        (
            expected_identity.get("entity_ids") or None,
            before_identity.entity_ids,
            "entity_ids",
            "The entry does not match expected entity_ids before reconfigure",
        ),
    )
    if (
        expected_identity.get("unique_id") is not None
        and not before_identity.unique_id_known
    ):
        # Not a mismatch — nothing could read the value. Home Assistant omits
        # unique_id from every config-entry endpoint, so only the ha_mcp_tools
        # custom component can supply it. Say that, rather than letting the
        # generic "registries report none" below imply the entry has none.
        _raise_identity_mismatch(
            entry_id,
            "Cannot verify expected_unique_id: Home Assistant does not expose a "
            "config entry's unique_id, and the ha_mcp_tools custom component "
            "(which does) is not installed or is too old. Anchor on "
            "expected_device_id, expected_mac or expected_entity_ids instead, "
            "or install/update the component.",
            before=before_identity,
            after=before_identity,
            expected=expected_identity,
        )
    for expected, available, key, message in checks:
        if expected is None:
            continue
        if not available:
            _raise_identity_mismatch(
                entry_id,
                f"Cannot compare expected {key}: the registries report none for "
                "this entry",
                before=before_identity,
                after=before_identity,
                expected=expected_identity,
            )
        if key == "mac":
            matched = _normalise_identity_value(expected) in {
                _normalise_identity_value(value) for value in available
            }
        elif key == "entity_ids":
            matched = set(expected) == set(available)
        else:
            matched = expected in set(available)
        if not matched:
            _raise_identity_mismatch(
                entry_id,
                message,
                before=before_identity,
                after=before_identity,
                expected=expected_identity,
            )
    related = await _classify_related_entries(
        client,
        before_identity,
        entry_id=entry_id,
        domain=domain,
    )
    if related.blocking:
        _raise_identity_mismatch(
            entry_id,
            "A second config entry in the same domain shares this entry's "
            "registered device",
            before=before_identity,
            after=before_identity,
            expected=expected_identity,
        )
    return before_identity, cross_domain_warnings(related.cross_domain)


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
                    "status": ReconfigureStatus.APPLY_FAILED,
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
        if not isinstance(payload, dict):
            payload = {}
        # A flow HA has already committed must not be aborted, unless the walker
        # ran out of step budget and left it pending.
        if payload.get("status") not in POST_COMMIT_STATUSES or payload.get(
            "flow_budget_exhausted"
        ):
            await _abort_flow_best_effort(client, flow_id)
        if payload.get("status") in POST_COMMIT_STATUSES:
            _raise_post_commit_verification_error(
                flow_error,
                entry_id=entry_id,
                domain=domain,
                rollback_metadata=rollback_metadata,
            )
        raise
    except Exception:
        await _abort_flow_best_effort(client, flow_id)
        raise
    return flow_id, result


async def _verify_reconfigure_result(
    client: Any,
    *,
    before: dict[str, Any],
    entry_id: str,
    domain: str,
    before_identity: ReconfigureIdentity,
    expected_identity: dict[str, Any],
    rollback_metadata: dict[str, Any],
    observed_entry: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Read back and verify the committed config entry with bounded retries.

    Home Assistant schedules the post-reconfigure reload rather than awaiting
    it, so a poll can catch the entry either mid-reload OR still in its
    pre-reload state. ``observed_entry`` is the settled fragment seen on the
    change subscription that was opened BEFORE the flow — when present it is
    authoritative for the operational state, because an event proves the
    reload reached that state, whereas a poll cannot tell a finished reload
    from one that has not started. The retry loop below remains the fallback
    for when no subscription could be opened.
    """
    after: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    warnings: list[str] = []
    last_verification_error: Exception | None = None
    for attempt in range(_VERIFICATION_ATTEMPTS):
        is_last_attempt = attempt == _VERIFICATION_ATTEMPTS - 1
        try:
            current_after = await client.get_config_entry(entry_id)
            after_identity = await _collect_reconfigure_identity(
                client, current_after, entry_id
            )
            if observed_entry is not None:
                # Trust the observed state over the polled one; the identity
                # reads still come from the registries below.
                current_after = {**current_after, **observed_entry}
            current_verification, current_warnings = await _verify_reconfigured_entry(
                client,
                before,
                current_after,
                entry_id=entry_id,
                domain=domain,
                before_identity=before_identity,
                after_identity=after_identity,
                expected_identity=expected_identity,
            )
            after = current_after
            verification = current_verification
            warnings = current_warnings
            if (
                observed_entry is None
                and _is_transient_reconfigure_state(current_after)
                and not is_last_attempt
            ):
                logger.info(
                    "Reconfigure verification observed transient state %s; retrying",
                    current_after.get("state"),
                )
                await asyncio.sleep(_VERIFICATION_BACKOFF_SECONDS[attempt])
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
            if not is_last_attempt:
                await asyncio.sleep(_VERIFICATION_BACKOFF_SECONDS[attempt])
    if after is None or verification is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Reconfiguration was submitted but the applied state could not "
                f"be verified: {last_verification_error!r}",
                suggestions=[
                    "Do not retry automatically. Inspect Home Assistant and the "
                    "device, then verify the entry before attempting rollback.",
                ],
                context={
                    "entry_id": entry_id,
                    "domain": domain,
                    "status": ReconfigureStatus.APPLIED_BUT_UNVERIFIED,
                    # NOT "error": create_error_response merges context into the
                    # top level, so that key would replace the structured error
                    # block and take the code and suggestions with it.
                    "verification_error": repr(last_verification_error),
                    "rollback": rollback_metadata,
                },
            )
        )
    return after, verification, warnings


def _reconfigure_response(
    *,
    status: str,
    entry_id: str,
    domain: str,
    title: Any,
    message: str,
    verification: dict[str, Any] | None,
    rollback: dict[str, Any],
    target_config: dict[str, Any],
    warnings: list[str],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the one response shape every reconfigure surface returns.

    Single construction point so the preflight and the applied result cannot
    drift into two differently-keyed payloads.
    """
    response: dict[str, Any] = {
        "success": True,
        "status": status,
        "operation": "reconfigure",
        "entry_id": entry_id,
        "domain": domain,
        "title": title,
        "message": message,
        "rollback": rollback,
        "target_config": target_config,
    }
    if verification is not None:
        response["verification"] = verification
    if extra:
        response.update(extra)
    if warnings:
        response["warnings"] = warnings
    return response


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

    # Subscribe BEFORE the flow: HA queues the post-commit reload with
    # async_create_task and returns, so a read-back can land either mid-reload
    # or before it starts. Only a stream opened ahead of the commit can tell a
    # finished reload from one that has not begun.
    subscription = await _subscribe_entry_changes(client)
    observed_entry: dict[str, Any] | None = None
    try:
        _, result = await _run_reconfigure_flow(
            client,
            domain=domain,
            entry_id=entry_id,
            flow_config=flow_config,
            rollback_metadata=rollback_metadata,
        )
        if subscription is not None:
            _, (_, queue) = subscription
            observed_entry = await _observe_reload_settled(
                queue, entry_id, timeout=_RELOAD_SETTLE_TIMEOUT
            )
    finally:
        if subscription is not None:
            ws, (sub_id, _) = subscription
            try:
                # Shielded so a cancelled caller cannot abort the HA-side
                # teardown mid-flight and leak the subscription.
                await asyncio.shield(ws.unsubscribe_command(sub_id))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Failed to unsubscribe entry changes: %r", exc)

    after, verification, related_warnings = await _verify_reconfigure_result(
        client,
        before=before,
        entry_id=entry_id,
        domain=domain,
        before_identity=prepared.identity,
        expected_identity=prepared.expected_identity,
        rollback_metadata=rollback_metadata,
        observed_entry=observed_entry,
    )
    # Say which mechanism produced the operational state, so a caller can tell
    # an observed reload from a polled sample.
    verification["operational_state_source"] = (
        "observed" if observed_entry is not None else "polled"
    )

    verified = (
        verification.get("identity_verification") == "complete"
        and verification.get("operational_state_verified") is True
    )
    status = (
        ReconfigureStatus.APPLIED_AND_VERIFIED
        if verified
        else ReconfigureStatus.APPLIED_BUT_UNVERIFIED
    )
    message = (
        f"{domain} integration reconfigured and verified"
        if verified
        else (
            f"{domain} integration reconfigure was applied, but the result could "
            f"not be fully verified (entry state: {verification.get('entry_state')})"
        )
    )
    return _reconfigure_response(
        status=status,
        entry_id=entry_id,
        domain=domain,
        title=after.get("title"),
        message=message,
        verification=verification,
        rollback=rollback_metadata,
        target_config=flow_config,
        warnings=[
            *prepared.warnings,
            *related_warnings,
            *(result.get("warnings") or []),
        ],
    )


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
    _reject_redaction_sentinels(config_dict)
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
