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
import logging
from typing import Any, Literal

from ..errors import ErrorCode, create_error_response
from ..redaction import sentinel_option_keys
from .config_entry_flow_form import _extract_schema_field_names
from .config_entry_flow_walker import (
    _FlowType,
    _handle_config_subentry_flow_steps,
    _handle_flow_steps,
)
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)


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
    except Exception:
        try:
            await asyncio.wait_for(
                client.abort_config_subentry_flow(flow_id), timeout=5.0
            )
        except Exception as abort_err:
            logger.warning(
                "Failed to abort config subentry flow %s after error: %s",
                flow_id,
                abort_err,
            )
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
