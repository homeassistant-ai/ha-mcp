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
``create_config_entry`` and edits them through ``update_config_entry_options``;
``ha_config_set_helper(helper_type="config_subentry")`` drives subentry flows
through ``set_config_subentry``. Changing a live entry's connection settings is
a different problem — it commits in place and Home Assistant reloads afterwards
— and lives in ``config_entry_reconfigure``, which depends on this module for
the shared flow-abort and sentinel-rejection helpers.

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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, ClassVar, Literal

from fastmcp.exceptions import ToolError

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantError,
)
from ..errors import ErrorCode, create_error_response
from ..redaction import sentinel_option_keys
from .config_entry_flow_form import _extract_schema_field_names
from .config_entry_flow_walker import (
    POST_COMMIT_STATUSES,
    _FlowType,
    _handle_config_subentry_flow_steps,
    _handle_flow_steps,
)
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)


OptionsRestoreReason = Literal[
    "unsupported_form", "unsupported_fields", "validation_failed", "flow_aborted"
]
_RESTORE_REASON_MESSAGES: dict[OptionsRestoreReason, str] = {
    "unsupported_form": "Options restore requires an authoritative options form",
    "unsupported_fields": "Snapshot fields are not accepted by the options form",
    "validation_failed": "Home Assistant rejected the restored options as invalid",
    "flow_aborted": "Home Assistant aborted the options restore",
}
# These are diagnostic labels, not an allowlist of restorable fields. Arbitrary
# snapshot keys and HA error payloads may contain values and must not be echoed.
_SAFE_RESTORE_FIELD_NAMES = frozenset(
    {
        "additional_options",
        "availability",
        "availability_template",
        "device_class",
        "icon",
        "name",
        "state",
        "state_class",
        "template_type",
        "unit_of_measurement",
    }
)


class OptionsFlowError(HomeAssistantError):
    """Complete-restore failure with submission knowledge for reconciliation."""

    reason_messages: ClassVar[dict[OptionsRestoreReason, str]] = (
        _RESTORE_REASON_MESSAGES
    )

    def __init__(
        self,
        message: str,
        *,
        apply_status: Literal["not_applied", "unknown", "applied"],
        entry_id: str,
        flow_id: str | None = None,
        reason: OptionsRestoreReason | None = None,
        fields: tuple[str, ...] = (),
    ) -> None:
        self.reason = reason
        self.fields = tuple(
            sorted(
                field
                for field in set(fields)
                if all(
                    segment in _SAFE_RESTORE_FIELD_NAMES for segment in field.split(".")
                )
            )
        )
        if reason is not None:
            message = self.reason_messages[reason]
            if self.fields:
                message += ": " + ", ".join(self.fields)
        super().__init__(message)
        self.apply_status = apply_status
        self.entry_id = entry_id
        self.flow_id = flow_id


class CreationFlowError(OptionsFlowError):
    """Complete-snapshot creation failure with conservative application knowledge."""

    reason_messages: ClassVar[dict[OptionsRestoreReason, str]] = {
        "unsupported_form": "Template recreation requires an authoritative creation form",
        "unsupported_fields": "Snapshot fields are not accepted by the creation form",
        "validation_failed": "Home Assistant rejected the recreated helper as invalid",
        "flow_aborted": "Home Assistant aborted the helper recreation",
    }


@dataclass
class _OptionsFlowProgress:
    """Track replies before the walker can fail while interpreting them."""

    entry_id: str
    flow_id: str | None = None
    apply_status: Literal["not_applied", "unknown", "applied"] = "not_applied"
    reason: OptionsRestoreReason | None = None
    fields: tuple[str, ...] = ()
    error_type: ClassVar[type[OptionsFlowError]] = OptionsFlowError
    operation: ClassVar[str] = "Options restore"

    def failure(self) -> OptionsFlowError:
        messages = {
            "not_applied": f"{self.operation} was refused before application",
            "unknown": f"{self.operation} got no completion reply; the change may have been applied",
            "applied": f"{self.operation} completed but its result could not be processed",
        }
        return self.error_type(
            messages[self.apply_status],
            apply_status=self.apply_status,
            entry_id=self.entry_id,
            flow_id=self.flow_id,
            reason=self.reason,
            fields=self.fields,
        )

    async def submit(
        self,
        client: Any,
        flow_id: str,
        payload: dict[str, Any],
        *,
        submit_fn: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
        | None = None,
    ) -> dict[str, Any]:
        self.apply_status = "unknown"
        try:
            submit = submit_fn or client.submit_options_flow_step
            result = await submit(flow_id, payload)
        except HomeAssistantAuthError:
            self.apply_status = "not_applied"
            raise
        except HomeAssistantAPIError as err:
            if err.status_code is not None and 400 <= err.status_code < 500:
                self.apply_status = "not_applied"
            raise
        self.record_reply(result)
        return result

    def record_reply(self, result: dict[str, Any]) -> None:
        """Record application knowledge before the walker interprets a reply."""
        if result.get("type") == _FlowType.CREATE_ENTRY:
            self.apply_status = "applied"
        elif result.get("type") == _FlowType.ABORT:
            self.apply_status = "not_applied"
            self.reason = "flow_aborted"
        elif result.get("type") == _FlowType.FORM and result.get("errors"):
            # Complete-restore callers require an options form that does not
            # commit when it reports a validation rejection.
            self.apply_status = "not_applied"
            self.reason = "validation_failed"
            if isinstance(result["errors"], dict):
                self.fields = tuple(result["errors"])
        else:
            # A complete snapshot cannot safely populate an unexpected next step.
            # Supported snapshot flows commit only on CREATE_ENTRY; another
            # form or menu leaves the submitted options pending in the flow.
            if result.get("type") in (_FlowType.FORM, _FlowType.MENU):
                self.apply_status = "not_applied"
                self.reason = "unsupported_form"
            raise self.failure()


def _unknown_snapshot_fields(
    schema: list[Any], config: dict[str, Any], prefix: str = ""
) -> list[str]:
    fields = {
        field["name"]: field
        for field in schema
        if isinstance(field, dict) and isinstance(field.get("name"), str)
    }
    unknown: list[str] = []
    for name, value in config.items():
        path = f"{prefix}.{name}" if prefix else name
        if name not in fields:
            unknown.append(path)
        elif isinstance(fields[name].get("schema"), list) and isinstance(value, dict):
            unknown.extend(
                _unknown_snapshot_fields(fields[name]["schema"], value, path)
            )
    return unknown


def _preflight_options_restore(
    progress: _OptionsFlowProgress, step: dict[str, Any], config: dict[str, Any]
) -> None:
    """Reject unsupported complete-snapshot fields before an options submit."""
    schema = step.get("data_schema")
    reason: OptionsRestoreReason
    fields: tuple[str, ...] = ()
    if step.get("type") != _FlowType.FORM or not isinstance(schema, list):
        reason = "unsupported_form"
    elif unknown := _unknown_snapshot_fields(schema, config):
        reason = "unsupported_fields"
        fields = tuple(unknown)
    elif step.get("errors"):
        reason = "validation_failed"
        if isinstance(step["errors"], dict):
            fields = tuple(step["errors"])
    else:
        return
    raise progress.error_type(
        _RESTORE_REASON_MESSAGES[reason],
        apply_status=progress.apply_status,
        entry_id=progress.entry_id,
        flow_id=progress.flow_id,
        reason=reason,
        fields=fields,
    )


class _CreationFlowProgress(_OptionsFlowProgress):
    """Require the selected creation form to consume a complete snapshot."""

    error_type = CreationFlowError
    operation = "Template recreation"

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(entry_id="")
        self.config = {
            key: value for key, value in config.items() if key != "next_step_id"
        }
        self.current_step: dict[str, Any] = {}

    def record_reply(self, result: dict[str, Any]) -> None:
        previous_type = self.current_step.get("type")
        self.current_step = result
        if previous_type in (None, _FlowType.MENU) and result.get("type") in (
            _FlowType.MENU,
            _FlowType.FORM,
        ):
            self.apply_status = "not_applied"
            return
        super().record_reply(result)
        if self.apply_status == "applied" and isinstance(result.get("result"), dict):
            entry_id = result["result"].get("entry_id")
            if isinstance(entry_id, str):
                self.entry_id = entry_id
        if self.apply_status == "applied" and previous_type != _FlowType.FORM:
            self.reason = "unsupported_form"
            raise self.failure()

    async def submit(
        self, client: Any, flow_id: str, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        if self.current_step.get("type") == _FlowType.FORM:
            _preflight_options_restore(self, self.current_step, self.config)
        return await super().submit(
            client, flow_id, payload, submit_fn=client.submit_config_flow_step
        )


async def _create_snapshot_helper(
    client: Any, helper_type: str, config: dict[str, Any]
) -> dict[str, Any]:
    """Create a helper from a complete snapshot without dropping fields."""
    progress = _CreationFlowProgress(config)
    try:
        _reject_redaction_sentinels(config)
        progress.apply_status = "unknown"
        initial_step = await client.start_config_flow(helper_type)
        progress.flow_id = initial_step.get("flow_id")
        progress.record_reply(initial_step)
        if not progress.flow_id:
            raise progress.failure()
        result = await _handle_flow_steps(
            client,
            progress.flow_id,
            initial_step,
            config,
            submit_fn=partial(progress.submit, client),
            helper_type=helper_type,
        )
        entry = result["entry"].get("result", {})
        return {
            "success": True,
            "entry_id": entry.get("entry_id"),
            "title": entry.get("title"),
            "domain": helper_type,
            "message": f"{helper_type} helper recreated successfully",
        }
    except (Exception, asyncio.CancelledError) as err:
        failure = err if isinstance(err, CreationFlowError) else progress.failure()
        await _cleanup_snapshot_creation(client, progress, failure)
        if isinstance(err, (CreationFlowError, asyncio.CancelledError)):
            raise
        raise failure from err


async def _cleanup_snapshot_creation(
    client: Any, progress: _CreationFlowProgress, failure: OptionsFlowError
) -> None:
    if not progress.flow_id:
        return
    if failure.apply_status == "not_applied":
        try:
            await asyncio.wait_for(
                client.abort_config_flow(progress.flow_id), timeout=5
            )
        except Exception as err:
            logger.warning(
                "Template recreation flow %s cleanup failed (reason=%s, error_type=%s)",
                progress.flow_id,
                failure.reason,
                type(err).__name__,
            )
    else:
        logger.warning(
            "Template recreation flow %s was not aborted "
            "(apply_status=%s, reason=%s); reconcile Home Assistant before retrying",
            progress.flow_id,
            failure.apply_status,
            failure.reason,
        )


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
    :func:`_handle_config_subentry_flow_steps` for why. It also walks with
    ``keep_current_values`` (issue #2254), so a partial patch keeps the
    subentry fields it does not name instead of resetting them. The create
    branch is unchanged on both counts.
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
            keep_current_values=subentry_id is not None,
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
    keep_current_values: bool = True,
) -> dict[str, Any]:
    """Update an existing config entry via its options flow.

    When ``expected_domain`` is provided, verifies the entry's domain matches
    it first (the helper path passes the helper_type; the generic
    ``ha_set_integration`` path passes ``None`` to accept any domain). Starts
    an options flow, walks the flow steps, and returns the result. ``noun``
    only affects response wording.

    By default the walk runs with ``keep_current_values``: every field an
    options step declares that
    ``config_dict`` does not name is submitted with the value the step itself
    carries, exactly as the HA UI's "Configure" dialog posts back the boxes
    nobody touched. Before issue #2254 those keys were dropped and voluptuous
    substituted each field's static default, so a one-key patch silently reset
    the rest of the entry's options. A key the caller sets to ``None`` is the
    opposite request and is honoured as a clear, which for a field carrying a
    schema default means submitting the ``None`` for Home Assistant to
    validate rather than omitting it into that default.
    A complete options snapshot restore passes ``keep_current_values=False``
    so current values absent from the snapshot are not copied into the payload.
    The caller must ensure the integration replaces its options only when its
    flow returns CREATE_ENTRY. The restore requires
    a single authoritative form and raises ``OptionsFlowError``
    with apply knowledge on failure; uncertain or completed restores are not
    aborted. Ordinary edits retain their existing error/abort behavior.
    """
    progress = None if keep_current_values else _OptionsFlowProgress(entry_id)
    try:
        return await _update_config_entry_options(
            client, entry_id, config_dict, expected_domain, noun, progress
        )
    except (Exception, asyncio.CancelledError) as err:
        if progress is None:
            raise
        failure = err if isinstance(err, OptionsFlowError) else progress.failure()
        if failure.flow_id and failure.apply_status != "not_applied":
            logger.warning(
                "Options restore flow %s for entry %s was not aborted after failure "
                "(apply_status=%s, reason=%s, fields=%s, error_type=%s); "
                "reconcile Home Assistant state before retrying",
                failure.flow_id,
                failure.entry_id,
                failure.apply_status,
                failure.reason,
                failure.fields,
                type(err).__name__,
            )
        if isinstance(err, (OptionsFlowError, asyncio.CancelledError)):
            raise
        raise failure from err


async def _update_config_entry_options(
    client: Any,
    entry_id: str,
    config_dict: dict[str, Any],
    expected_domain: str | None,
    noun: str,
    progress: _OptionsFlowProgress | None,
) -> dict[str, Any]:
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
    if progress is not None:
        progress.flow_id = flow_id
        if flow_result.get("type") == _FlowType.CREATE_ENTRY:
            progress.apply_status = "applied"

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
        if progress is not None:
            _preflight_options_restore(progress, flow_result, config_dict)

        result = await _handle_flow_steps(
            client,
            flow_id,
            flow_result,
            config_dict,
            submit_fn=partial(progress.submit, client)
            if progress is not None
            else client.submit_options_flow_step,
            helper_type=expected_domain,
            keep_current_values=progress is None,
        )
    except Exception as flow_err:
        if progress is None or progress.apply_status == "not_applied":
            try:
                await asyncio.wait_for(client.abort_options_flow(flow_id), timeout=5.0)
            except Exception as abort_err:
                logger.warning(
                    "Failed to abort options flow %s for entry %s "
                    "(stage=abort_cleanup, reason=%s, error_type=%s)",
                    flow_id,
                    entry_id,
                    flow_err.reason if isinstance(flow_err, OptionsFlowError) else None,
                    type(abort_err).__name__,
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
    *,
    complete_snapshot: bool = False,
) -> dict[str, Any]:
    """Create a new flow-based helper via the config flow.

    Starts a config flow, walks the flow steps, and returns the result.
    Ordinary creation aborts the flow on error.
    Complete snapshots require all options in the selected form and preserve
    uncertain or completed creation flows for reconciliation.
    """
    if complete_snapshot:
        return await _create_snapshot_helper(client, helper_type, config_dict)
    return await create_config_entry(client, helper_type, config_dict, noun="helper")
