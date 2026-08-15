"""Step submission and flow walking for config-entry flows.

Extracted from ``config_entry_flow.py`` (which holds the public create/update
entry points) when that module crossed the ~1000-line split threshold. Holds
the two walkers that drive a flow to completion, the step submission that
translates HA's 4xx bodies into structured errors, and the flow introspection
that feeds those errors a ``data_schema``. Imports the menu and form step
handling from ``config_entry_flow_menu`` / ``config_entry_flow_form``; the
public entry points in ``config_entry_flow`` import from here.
"""

import asyncio
import logging
from enum import StrEnum
from typing import Any, NoReturn

from ..client.rest_client import HomeAssistantAPIError
from ..errors import ErrorCode, create_error_response
from ..redaction import redact_flow_schema, redaction_enabled
from .config_entry_flow_form import (
    _MENU_SELECTION_KEYS,
    _auto_confirm_form_payload,
    _handle_form_step,
    _ReuseState,
    _success_warnings,
)
from .config_entry_flow_menu import (
    _flow_step_budget,
    _handle_menu_step,
)
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)

# Flow step types an MCP client cannot drive: external steps need a browser
# (OAuth / cloud authorization), progress steps wait on HA-side async work.
# Surfaced as structured errors instead of attempted (issue #1814).
_UNDRIVABLE_STEP_TYPES = frozenset(
    {"external", "external_done", "progress", "progress_done"}
)
_RECONFIGURE_SUCCESS_REASONS = frozenset(
    {
        "reauth_successful",
        "reconfigure_successful",
    }
)


class _FlowType(StrEnum):
    """HA config flow result type strings."""

    FORM = "form"
    MENU = "menu"
    ABORT = "abort"
    CREATE_ENTRY = "create_entry"


class ReconfigureStatus(StrEnum):
    """Outcome vocabulary for the ``ha_set_integration`` reconfigure mode.

    Ordered by how far the request got. Everything from ``APPLIED_*`` onward
    means Home Assistant committed the change, so a caller must not blindly
    retry those.
    """

    #: Read-only preflight: validated, nothing started.
    PREVIEW = "preview"
    #: A confirmation was required and none was supplied.
    CONFIRMATION_REQUIRED = "confirmation_required"
    #: The supplied confirm_token no longer matches the entry's state.
    STALE_PREFLIGHT = "stale_preflight"
    #: The flow was aborted before any value reached Home Assistant.
    FLOW_ABORTED_BEFORE_APPLY = "flow_aborted_before_apply"
    #: Home Assistant rejected the change; nothing was committed.
    APPLY_FAILED = "apply_failed"
    #: Committed, but the flow never consumed every supplied value.
    APPLIED_BUT_INCOMPLETE = "applied_but_incomplete"
    #: Committed, and the entry no longer carries the identity it had.
    APPLIED_IDENTITY_MISMATCH = "applied_identity_mismatch"
    #: Committed, identity intact, and the entry reloaded cleanly.
    APPLIED_AND_VERIFIED = "applied_and_verified"
    #: Committed, but the result could not be fully verified.
    APPLIED_BUT_UNVERIFIED = "applied_but_unverified"


#: Statuses that mean Home Assistant already committed the change. Callers use
#: this to decide whether aborting a flow or retrying is still safe.
POST_COMMIT_STATUSES = frozenset(
    {
        ReconfigureStatus.APPLIED_BUT_INCOMPLETE,
        ReconfigureStatus.APPLIED_IDENTITY_MISMATCH,
        ReconfigureStatus.APPLIED_AND_VERIFIED,
        ReconfigureStatus.APPLIED_BUT_UNVERIFIED,
    }
)


def _parse_flow_api_error(
    api_error: HomeAssistantAPIError,
) -> dict[str, Any]:
    """Extract structured field-level info from an HA flow 4xx response.

    Home Assistant returns voluptuous validation failures during flow
    submission as either:

    - ``{"message": "User input malformed: extra keys not allowed @ data['name']"}``
      (raised before form validation, e.g. unknown field in payload)
    - ``{"errors": {"base": "..."}, "description_placeholders": {...}}``
      (per-field errors after voluptuous validation succeeds)
    - Free-form text (when the body isn't JSON).

    Returns a dict with at least:
      - ``message``: the most informative human-readable string we found.
      - ``field_errors``: dict of field-name -> error code/message, when
        the body contained an ``errors`` map. Empty dict otherwise.
      - ``raw``: the response_data dict (or ``None``) for diagnostics.
    """
    body = api_error.response_data or {}
    field_errors: dict[str, Any] = {}
    message_parts: list[str] = []

    if isinstance(body, dict):
        errors_field = body.get("errors")
        if isinstance(errors_field, dict):
            field_errors = {
                key: val for key, val in errors_field.items() if isinstance(key, str)
            }

        # HA's stock 400 carries a `message` key with the voluptuous detail.
        msg = body.get("message")
        if isinstance(msg, str) and msg.strip():
            message_parts.append(msg.strip())

        # description_placeholders sometimes carry the human-readable error.
        placeholders = body.get("description_placeholders")
        if isinstance(placeholders, dict):
            for key, val in placeholders.items():
                if isinstance(val, str) and val.strip():
                    message_parts.append(f"{key}: {val.strip()}")

    if not message_parts:
        # Fall back to the wrapper exception message ("API error: 400 - ...").
        message_parts.append(str(api_error))

    return {
        "message": " | ".join(dict.fromkeys(message_parts)),  # de-dupe, preserve order
        "field_errors": field_errors,
        "raw": body if isinstance(body, dict) else None,
    }


async def _process_menu_flow_result(
    flow_result: dict[str, Any],
    client: Any,
    intro_flow_id: str | None,
    menu_choice: str | None,
) -> dict[str, Any]:
    """Return schema or menu_options dict for a MENU-type flow result."""
    info: dict[str, Any] = {}
    if menu_choice:
        if not intro_flow_id:
            return info
        try:
            step = await asyncio.wait_for(
                client.submit_config_flow_step(
                    intro_flow_id, {"next_step_id": menu_choice}
                ),
                timeout=10.0,
            )
        except (HomeAssistantAPIError, TimeoutError):
            return info
        if step.get("type") == _FlowType.FORM:
            schema = step.get("data_schema")
            if isinstance(schema, list):
                info["schema"] = schema
        return info

    options = flow_result.get("menu_options")
    if isinstance(options, list):
        filtered = [opt for opt in options if isinstance(opt, str)]
        if filtered:
            info["menu_options"] = filtered
    return info


async def fetch_helper_flow_info(
    client: Any,
    helper_type: str | None,
    menu_choice: str | None = None,
) -> dict[str, Any]:
    """Best-effort introspection of a helper's config-entry flow.

    Starts a fresh introspection flow (always aborted) and returns a dict
    with optional keys ``"schema"`` and ``"menu_options"`` so a single HA
    round-trip serves both the schema-attach path (used by
    ``_raise_flow_api_error`` and the pre-flow validation gates in
    ``_handle_flow_helper``) and the menu-sub-types path (used when a
    menu-rooted helper has no branch chosen yet — issue #1186).

    Behaviour:

    - FORM at top: ``{"schema": [...]}``
    - MENU at top with ``menu_choice``: submits and returns the branch
      form schema as ``{"schema": [...]}`` (no ``menu_options`` since
      the caller already picked a branch)
    - MENU at top without ``menu_choice``: ``{"menu_options": [...]}``
    - any failure or unparseable shape: ``{}`` (callers branch on
      ``"schema" in info`` / ``"menu_options" in info``)
    """
    info: dict[str, Any] = {}
    if not helper_type or client is None:
        return info
    intro_flow_id: str | None = None
    try:
        flow_result = await client.start_config_flow(helper_type)
        intro_flow_id = flow_result.get("flow_id")
        flow_type = flow_result.get("type")

        if flow_type == _FlowType.FORM:
            schema = flow_result.get("data_schema")
            if isinstance(schema, list):
                info["schema"] = schema
            return info

        if flow_type == _FlowType.MENU:
            return await _process_menu_flow_result(
                flow_result, client, intro_flow_id, menu_choice
            )

        return info
    except Exception:
        return info
    finally:
        if intro_flow_id:
            try:
                await asyncio.wait_for(
                    client.abort_config_flow(intro_flow_id), timeout=5.0
                )
            except Exception as abort_err:
                logger.debug(
                    f"Failed to abort introspection flow {intro_flow_id}: {abort_err}"
                )


def _build_flow_error_context(
    flow_id: str,
    status_code: int,
    helper_type: str | None,
    menu_choice: str | None,
    current_step: dict[str, Any] | None,
    submitted: dict[str, Any] | None,
    parsed_raw: dict[str, Any] | None,
) -> dict[str, Any]:
    context: dict[str, Any] = {"flow_id": flow_id, "status_code": status_code}
    if helper_type:
        context["helper_type"] = helper_type
    if menu_choice:
        context["menu_choice"] = menu_choice
    if current_step is not None:
        context["step_id"] = current_step.get("step_id")
    if submitted is not None:
        context["submitted_keys"] = sorted(submitted.keys())
    if parsed_raw is not None:
        context["response_body"] = parsed_raw
    return context


async def _raise_flow_api_error(
    api_error: HomeAssistantAPIError,
    *,
    client: Any,
    flow_id: str,
    helper_type: str | None,
    menu_choice: str | None,
    current_step: dict[str, Any] | None,
    submitted: dict[str, Any] | None,
    is_reconfigure: bool = False,
) -> NoReturn:
    """Translate an HA 4xx during a flow submit into a structured ToolError.

    For 400/422 responses, parses ``response_data`` for field-level info
    via ``_parse_flow_api_error``. When the body is unstructured (no
    ``errors`` map), attaches a ``data_schema`` so the caller has actionable
    information.

    ``is_reconfigure`` changes two things. The schema comes from the live
    reconfigure step rather than a fresh introspection flow (``helper_type``
    carries the integration domain there and reaches no schema fetch), and
    the prose names the integration instead of a helper, because this path
    also serves ``ha_set_integration(reconfigure=True)``.

    Always raises ``ToolError`` — never returns.
    """
    parsed = _parse_flow_api_error(api_error)
    field_errors = parsed["field_errors"]
    status_code = api_error.status_code or 0

    context = _build_flow_error_context(
        flow_id,
        status_code,
        helper_type,
        menu_choice,
        current_step,
        submitted,
        parsed["raw"],
    )

    suggestions: list[str] = []
    message: str

    current_schema = None
    if current_step is not None:
        step_schema = current_step.get("data_schema")
        if isinstance(step_schema, list):
            current_schema = step_schema

    if is_reconfigure:
        # The live reconfigure step already carries the schema HA is asking
        # against. Introspecting here would start a second, normal setup flow
        # whose schema is a different contract.
        schema = current_schema
    else:
        # Single introspection round-trip — used by both branches below.
        info = await fetch_helper_flow_info(client, helper_type, menu_choice)
        schema = info.get("schema") or current_schema
    # An options/reconfigure step's schema carries the persisted values in
    # description.suggested_value — under redact_secrets those must not ride
    # into the error context verbatim (#2157). Deep-copies, so the live
    # flow_result is untouched.
    if schema is not None and redaction_enabled():
        schema = redact_flow_schema(schema)

    if field_errors:
        # Structured field errors — tell the caller which fields failed.
        context["field_errors"] = field_errors
        readable = ", ".join(f"{k}: {v}" for k, v in field_errors.items())
        subject = (
            f"{helper_type} reconfigure validation"
            if is_reconfigure and helper_type
            else "Reconfigure validation"
            if is_reconfigure
            else "Helper validation"
        )
        message = f"{subject} failed — {readable}"
        suggestions.append(
            "Fix the field(s) listed in 'field_errors' and retry the call."
        )
        # Issue #1149: also attach the data_schema so the LLM sees the field
        # shape (selector, required, ...) alongside the per-field error
        # codes — symmetric with the unstructured-error branch below.
        # `field_errors` tells "what failed", `data_schema` tells "what's
        # accepted"; together they're enough for self-correction.
        if schema is not None:
            context["data_schema"] = schema
    else:
        # Unstructured — attach the data_schema so the LLM has something to use.
        subject = (
            f"{helper_type} reconfigure"
            if is_reconfigure and helper_type
            else "reconfigure"
            if is_reconfigure
            else helper_type or "flow"
        )
        message = (
            f"Home Assistant rejected the {subject} request "
            f"({status_code}): {parsed['message']}"
        )
        if schema is not None:
            context["data_schema"] = schema
            suggestions.append(
                "Inspect 'data_schema' in this error to see the fields HA expects, "
                "then retry with a corrected config."
            )

    if is_reconfigure:
        context["status"] = ReconfigureStatus.APPLY_FAILED

    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            message,
            suggestions=suggestions,
            context=context,
        )
    )


async def _submit_step(
    submit_fn: Any,
    flow_id: str,
    payload: dict[str, Any],
    *,
    client: Any,
    helper_type: str | None,
    last_menu_choice: str | None,
    current_step: dict[str, Any],
    is_reconfigure: bool = False,
) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(submit_fn(flow_id, payload), timeout=20.0)
    except HomeAssistantAPIError as api_err:
        if api_err.status_code in (400, 422):
            await _raise_flow_api_error(
                api_err,
                client=client,
                flow_id=flow_id,
                helper_type=helper_type,
                menu_choice=last_menu_choice,
                current_step=current_step,
                submitted=payload,
                is_reconfigure=is_reconfigure,
            )
        raise
    except TimeoutError:
        if is_reconfigure:
            raise_tool_error(
                create_error_response(
                    ErrorCode.TIMEOUT_OPERATION,
                    "Reconfigure flow submission timed out; the change may have been applied",
                    suggestions=[
                        "Do not retry automatically. Inspect the config entry in Home Assistant before attempting rollback."
                    ],
                    context={
                        "flow_id": flow_id,
                        "status": ReconfigureStatus.APPLIED_BUT_UNVERIFIED,
                        "submitted_keys": sorted(payload),
                        "details": current_step,
                    },
                )
            )
        raise


def _finish_flow_entry(
    flow_id: str,
    current_step: dict[str, Any],
    *,
    supplied_keys: list[str],
    saw_form_step: bool,
    any_form_key_consumed: bool,
    ignored_config_keys: set[str],
    remaining_config: dict[str, Any],
    reuse_state: _ReuseState,
) -> dict[str, Any]:
    """Build the CREATE_ENTRY success response, or raise on total key miss.

    When the flow presented at least one form step, the caller supplied
    config keys, and NONE were consumed by any form (typos / wrong field
    names), the flow was walked on empty forms and HA saved form defaults —
    not the caller's values. A success + warning there reads as "done" to an
    LLM caller, so fail loudly with the schema route instead. Partial
    consumption — and flows that complete without any form step (instant
    creates) — keep the established success + warnings contract.
    """
    if supplied_keys and saw_form_step and not any_form_key_consumed:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Flow completed without consuming any of the supplied "
                "config keys — every form step was submitted empty, so "
                "the flow saved its defaults, not your values",
                suggestions=[
                    "Check the field names against the flow's data_schema — "
                    "ha_get_integration(entry_id=..., include_schema=True) "
                    "shows the accepted fields — then retry with corrected "
                    "keys.",
                ],
                context={
                    "flow_id": flow_id,
                    "supplied_keys": supplied_keys,
                    "details": current_step,
                },
            )
        )
    response: dict[str, Any] = {"success": True, "entry": current_step}
    warnings = _success_warnings(ignored_config_keys, remaining_config, reuse_state)
    if warnings:
        response["warnings"] = warnings
    return response


def _flow_failure_context(
    flow_id: str, current_step: dict[str, Any], *, is_reconfigure: bool
) -> dict[str, Any]:
    context: dict[str, Any] = {"flow_id": flow_id, "details": current_step}
    if is_reconfigure:
        context["status"] = ReconfigureStatus.APPLY_FAILED
    return context


def _raise_flow_abort(
    flow_id: str, current_step: dict[str, Any], *, is_reconfigure: bool = False
) -> NoReturn:
    """Raise the structured error for an ABORT flow step."""
    reason = current_step.get("reason")
    abort_suggestions: list[str] = []
    if reason in ("already_configured", "single_instance_allowed"):
        # Common benign aborts on the add-integration path (#1814): give the
        # caller a route to the existing entry instead of a bare failure.
        abort_suggestions.append(
            "The integration is already set up — use "
            "ha_get_integration() to find the existing entry."
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Flow aborted: {reason}",
            suggestions=abort_suggestions or None,
            context=_flow_failure_context(
                flow_id, current_step, is_reconfigure=is_reconfigure
            ),
        )
    )


def _unconsumed_reconfigure_keys(
    ignored_config_keys: set[str],
    remaining_config: dict[str, Any],
) -> list[str]:
    """List caller keys that a reconfigure flow never consumed.

    ``remaining_config`` starts as a copy of the caller's config and is only
    ever popped from, or reassigned at a key it already holds, so every key
    still in it is a caller key.
    """
    return sorted(ignored_config_keys | set(remaining_config))


def _reconfigure_success_response(
    current_step: dict[str, Any],
    *,
    ignored_config_keys: set[str],
    remaining_config: dict[str, Any],
    reuse_state: _ReuseState,
) -> dict[str, Any]:
    """Build the common successful reconfigure response."""
    response: dict[str, Any] = {
        # Past tense, matching the "created" this walker's sibling emits. Only
        # set_config_subentry reads this; the ha_set_integration reconfigure
        # surface builds its own response and reports operation "reconfigure"
        # on both the preview and the applied leg.
        "success": True,
        "operation": "reconfigured",
        "flow_result": current_step,
    }
    warnings = _success_warnings(ignored_config_keys, remaining_config, reuse_state)
    if warnings:
        response["warnings"] = warnings
    return response


def _reconfigure_abort_result(
    current_step: dict[str, Any],
    *,
    is_reconfigure: bool,
    flow_id: str,
    ignored_config_keys: set[str],
    remaining_config: dict[str, Any],
    reuse_state: _ReuseState,
) -> dict[str, Any] | None:
    """Translate a successful reconfigure abort into a walker result."""
    if (
        not is_reconfigure
        or current_step.get("reason") not in _RECONFIGURE_SUCCESS_REASONS
    ):
        return None
    unresolved = _unconsumed_reconfigure_keys(ignored_config_keys, remaining_config)
    if unresolved:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Reconfigure flow completed without consuming all supplied "
                "configuration values",
                suggestions=[
                    "Inspect the existing entry state before taking any further "
                    "action; Home Assistant may already have applied part of the "
                    "requested change.",
                ],
                context={
                    "flow_id": flow_id,
                    "status": ReconfigureStatus.APPLIED_BUT_INCOMPLETE,
                    "unconsumed_config_keys": unresolved,
                    "details": current_step,
                },
            )
        )
    return _reconfigure_success_response(
        current_step,
        ignored_config_keys=ignored_config_keys,
        remaining_config=remaining_config,
        reuse_state=reuse_state,
    )


def _handle_abort_step(
    flow_id: str,
    current_step: dict[str, Any],
    *,
    is_reconfigure: bool,
    ignored_config_keys: set[str],
    remaining_config: dict[str, Any],
    reuse_state: _ReuseState,
) -> dict[str, Any]:
    """Handle normal aborts and successful reconfigure aborts."""
    reconfigure_result = _reconfigure_abort_result(
        current_step,
        is_reconfigure=is_reconfigure,
        flow_id=flow_id,
        ignored_config_keys=ignored_config_keys,
        remaining_config=remaining_config,
        reuse_state=reuse_state,
    )
    if reconfigure_result is not None:
        return reconfigure_result
    _raise_flow_abort(flow_id, current_step, is_reconfigure=is_reconfigure)
    raise AssertionError("unreachable after _raise_flow_abort")


def _flow_form_payload(
    current_step: dict[str, Any],
    *,
    flow_id: str,
    remaining_config: dict[str, Any],
    ignored_config_keys: set[str],
    reuse_state: _ReuseState,
) -> tuple[dict[str, Any], bool]:
    """Build one generic flow form payload.

    Returns the payload and whether the step consumed at least one caller key
    — not which ones; ``remaining_config`` and ``ignored_config_keys`` carry
    that.
    """
    consumed_form_keys: set[str] = set()
    form_data = _auto_confirm_form_payload(current_step)
    if form_data is None:
        form_data = _handle_form_step(
            flow_id,
            current_step,
            remaining_config,
            ignored_config_keys,
            consumed_form_keys,
            reuse_state,
        )
    return form_data, bool(consumed_form_keys)


def _handle_flow_create_entry(
    flow_id: str,
    current_step: dict[str, Any],
    *,
    is_reconfigure: bool,
    supplied_keys: list[str],
    saw_form_step: bool,
    any_form_key_consumed: bool,
    ignored_config_keys: set[str],
    remaining_config: dict[str, Any],
    reuse_state: _ReuseState,
) -> dict[str, Any]:
    """Finish a normal flow, but never treat create_entry as reconfigure success."""
    if is_reconfigure:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Reconfigure flow created a new config entry instead of "
                "updating the existing entry",
                suggestions=[
                    "Inspect Home Assistant for a duplicate entry before "
                    "retrying; do not create another entry automatically."
                ],
                context={
                    "flow_id": flow_id,
                    "status": ReconfigureStatus.APPLIED_BUT_UNVERIFIED,
                    "details": current_step,
                },
            )
        )
    return _finish_flow_entry(
        flow_id,
        current_step,
        supplied_keys=supplied_keys,
        saw_form_step=saw_form_step,
        any_form_key_consumed=any_form_key_consumed,
        ignored_config_keys=ignored_config_keys,
        remaining_config=remaining_config,
        reuse_state=reuse_state,
    )


async def _handle_flow_steps(
    client: Any,
    flow_id: str,
    initial_step: dict[str, Any],
    config: dict[str, Any],
    submit_fn: Any = None,
    helper_type: str | None = None,
    *,
    is_reconfigure: bool = False,
) -> dict[str, Any]:
    """Walk a multi-step config flow handling menu and form steps.

    The step budget comes from :func:`_flow_step_budget`: a base of 10
    steps plus two per caller-supplied menu selection.

    HA flows can present steps in sequence:
    - ``menu``: caller supplies selection via ``group_type``/``next_step_id`` key
    - ``form``: caller supplies field values; aborts immediately on validation errors
    - ``create_entry``: flow complete
    - ``abort``: flow terminated by HA

    Args:
        client: HomeAssistantClient instance
        flow_id: Flow ID from start_config_flow or start_options_flow
        initial_step: The first step returned by the flow start call
        config: Full caller-provided config dict. Menu selection keys are
            consumed by menu steps — a key may carry a single selection or a
            list of successive selections, consumed one per menu encounter
            (flows can revisit a menu after each branch; issue #2116).
            Remaining keys are submitted on the first
            form step whose schema declares them or, when HA omits the schema,
            on the first form step outright. A later step that redeclares an
            already-consumed field gets that value resubmitted once, with a
            warning, where the step marks it required and supplies neither a
            default nor a value of its own (see
            :func:`_redeclared_field_submission`).
        submit_fn: Async function to submit a step. Defaults to
            client.submit_config_flow_step (create). Pass
            client.submit_options_flow_step for options (update) flows.
        helper_type: Optional helper type (e.g. ``"statistics"``). When
            provided outside reconfigure mode, surfaces the helper's
            data_schema in error context for unstructured HA 4xx responses so
            the caller can react. Under ``is_reconfigure`` no schema is
            fetched (the live step already carries the right one) and the
            value is used only to name the integration in error prose.
        is_reconfigure: Whether this is the official reconfigure flow — the
            same mode HA uses for reauth, so both ``reconfigure_successful``
            and ``reauth_successful`` count as its success aborts. In this
            mode ``create_entry`` is rejected as
            ``ReconfigureStatus.APPLIED_BUT_UNVERIFIED``, and a success abort
            is a success only when the flow consumed EVERY supplied config
            key — any leftover raises
            ``ReconfigureStatus.APPLIED_BUT_INCOMPLETE``, because Home
            Assistant has already committed whatever it did consume.

    Returns:
        ``{"success": True, "entry": result}`` for a normal flow, or
        ``{"success": True, "operation": "reconfigure", "flow_result":
        result}`` under ``is_reconfigure``. Both carry ``warnings``
        when SOME caller-supplied config keys were not declared by any flow
        step, or when a key had to be resubmitted to a later step that
        redeclared it. When the flow presented at least one form step and NONE
        of the supplied keys were consumed, raises ``VALIDATION_INVALID_PARAMETER``
        instead of reporting a misleading success — the flow completed on
        empty forms (defaults), applying nothing the caller asked for (see
        :func:`_finish_flow_entry`). Raises ToolError on any failure.
    """
    if submit_fn is None:
        submit_fn = client.submit_config_flow_step
    remaining_config = dict(config)
    current_step = initial_step
    last_menu_choice: str | None = None
    consumed_menu_selections: list[str] = []
    consumed_menu_selection_keys: list[str] = []
    ignored_config_keys: set[str] = set()
    reuse_state = _ReuseState()
    supplied_keys = sorted(k for k in config if k not in _MENU_SELECTION_KEYS)
    saw_form_step = False
    any_form_key_consumed = False
    max_steps = _flow_step_budget(config)

    for step_num in range(max_steps):
        result_type = current_step.get("type")

        if result_type == _FlowType.CREATE_ENTRY:
            return _handle_flow_create_entry(
                flow_id,
                current_step,
                is_reconfigure=is_reconfigure,
                supplied_keys=supplied_keys,
                saw_form_step=saw_form_step,
                any_form_key_consumed=any_form_key_consumed,
                ignored_config_keys=ignored_config_keys,
                remaining_config=remaining_config,
                reuse_state=reuse_state,
            )

        if result_type == _FlowType.ABORT:
            return _handle_abort_step(
                flow_id,
                current_step,
                is_reconfigure=is_reconfigure,
                ignored_config_keys=ignored_config_keys,
                remaining_config=remaining_config,
                reuse_state=reuse_state,
            )

        if result_type == _FlowType.MENU:
            menu_choice = _handle_menu_step(
                flow_id,
                current_step,
                remaining_config,
                consumed_menu_selections,
                consumed_menu_selection_keys,
            )
            last_menu_choice = menu_choice
            logger.debug(
                f"Flow step {step_num}: menu '{menu_choice}' "
                f"(step_id={current_step.get('step_id')})"
            )
            current_step = await _submit_step(
                submit_fn,
                flow_id,
                {"next_step_id": menu_choice},
                client=client,
                helper_type=helper_type,
                last_menu_choice=last_menu_choice,
                current_step=current_step,
                is_reconfigure=is_reconfigure,
            )

        elif result_type == _FlowType.FORM:
            # _handle_form_step pops only the keys declared in the current
            # step's data_schema, leaving any other keys in remaining_config
            # for subsequent steps (HA can present multi-step forms, e.g.
            # statistics: user step then pick-characteristic step), and records
            # what it consumed for any later step that redeclares the same
            # field.
            saw_form_step = True
            form_data, consumed_any = _flow_form_payload(
                current_step,
                flow_id=flow_id,
                remaining_config=remaining_config,
                ignored_config_keys=ignored_config_keys,
                reuse_state=reuse_state,
            )
            if consumed_any:
                any_form_key_consumed = True
            logger.debug(
                f"Flow step {step_num}: form submit "
                f"(step_id={current_step.get('step_id')}, keys={list(form_data.keys())})"
            )
            current_step = await _submit_step(
                submit_fn,
                flow_id,
                form_data,
                client=client,
                helper_type=helper_type,
                last_menu_choice=last_menu_choice,
                current_step=current_step,
                is_reconfigure=is_reconfigure,
            )

        elif result_type in _UNDRIVABLE_STEP_TYPES:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Flow reached a '{result_type}' step that cannot be "
                    "completed via MCP (browser/OAuth authorization or an "
                    "asynchronous provider step)",
                    suggestions=[
                        "Complete this flow in the Home Assistant UI "
                        "(Settings > Devices & Services)."
                    ],
                    context=_flow_failure_context(
                        flow_id, current_step, is_reconfigure=is_reconfigure
                    ),
                )
            )

        else:
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_UNEXPECTED,
                    f"Unexpected flow result type: {result_type}",
                    context=_flow_failure_context(
                        flow_id, current_step, is_reconfigure=is_reconfigure
                    ),
                )
            )

    raise_tool_error(
        create_error_response(
            ErrorCode.TIMEOUT_OPERATION,
            f"Flow exceeded {max_steps} steps",
            context={
                "flow_id": flow_id,
                "max_steps": max_steps,
                "consumed_menu_selections": consumed_menu_selections,
                # Reconfigure-only keys: the reconfigure caller reads both to
                # decide whether to abort the pending flow. Adding them to
                # every flow's error shape would change the public contract of
                # the add-integration, options and helper paths for nothing.
                **(
                    {
                        "flow_budget_exhausted": True,
                        "status": ReconfigureStatus.FLOW_ABORTED_BEFORE_APPLY,
                    }
                    if is_reconfigure
                    else {}
                ),
            },
        )
    )


def _handle_subentry_create_entry(
    flow_id: str,
    current_step: dict[str, Any],
    *,
    is_reconfigure: bool,
    ignored_config_keys: set[str],
    remaining_config: dict[str, Any],
    reuse_state: _ReuseState,
) -> dict[str, Any]:
    """Handle a subentry create result and reject it in reconfigure mode."""
    if is_reconfigure:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Config subentry reconfigure created a new entry instead "
                "of updating the existing entry",
                suggestions=[
                    "Inspect Home Assistant for a duplicate subentry before "
                    "retrying; do not create another entry automatically."
                ],
                context={
                    "flow_id": flow_id,
                    "status": ReconfigureStatus.APPLIED_BUT_UNVERIFIED,
                    "details": current_step,
                },
            )
        )
    response: dict[str, Any] = {
        "success": True,
        "operation": "created",
        "flow_result": current_step,
    }
    warnings = _success_warnings(ignored_config_keys, remaining_config, reuse_state)
    if warnings:
        response["warnings"] = warnings
    return response


def _handle_subentry_abort(
    flow_id: str,
    current_step: dict[str, Any],
    *,
    is_reconfigure: bool,
    ignored_config_keys: set[str],
    remaining_config: dict[str, Any],
    reuse_state: _ReuseState,
) -> dict[str, Any]:
    """Handle a subentry abort, enforcing consumed values after reconfigure."""
    reason = current_step.get("reason")
    reconfigure_result = _reconfigure_abort_result(
        current_step,
        is_reconfigure=is_reconfigure,
        flow_id=flow_id,
        ignored_config_keys=ignored_config_keys,
        remaining_config=remaining_config,
        reuse_state=reuse_state,
    )
    if reconfigure_result is not None:
        return reconfigure_result
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Config subentry flow aborted: {reason}",
            context={"flow_id": flow_id, "details": current_step},
        )
    )
    raise AssertionError("unreachable after subentry flow abort")


async def _handle_config_subentry_flow_steps(
    client: Any,
    flow_id: str,
    initial_step: dict[str, Any],
    config: dict[str, Any],
    *,
    is_reconfigure: bool,
) -> dict[str, Any]:
    """Walk a config subentry flow and accept HA's reconfigure-success abort.

    Successful results include ``warnings`` when caller-supplied config keys
    were not declared by any flow step, or when a key was resubmitted to a
    later step that redeclared it. Fields redeclared by a later step are filled
    on the same terms as :func:`_handle_flow_steps`.

    ``is_reconfigure`` (set by ``set_config_subentry`` whenever a
    ``subentry_id`` is supplied) tightens that contract to match the
    integration reconfigure path: a success abort that left ANY supplied key
    unconsumed raises ``ReconfigureStatus.APPLIED_BUT_INCOMPLETE`` instead of
    returning success plus a warning. Home Assistant has already committed
    the keys it did consume, so a partial apply reported as success would
    read as "done" to an agent. This is a behaviour change for
    ``ha_config_set_helper(helper_type="config_subentry", subentry_id=...)``
    callers who previously relied on the warning.
    """
    remaining_config = dict(config)
    current_step = initial_step
    last_menu_choice: str | None = None
    consumed_menu_selections: list[str] = []
    consumed_menu_selection_keys: list[str] = []
    ignored_config_keys: set[str] = set()
    reuse_state = _ReuseState()
    max_steps = _flow_step_budget(config)

    for step_num in range(max_steps):
        result_type = current_step.get("type")

        if result_type == _FlowType.CREATE_ENTRY:
            return _handle_subentry_create_entry(
                flow_id,
                current_step,
                is_reconfigure=is_reconfigure,
                ignored_config_keys=ignored_config_keys,
                remaining_config=remaining_config,
                reuse_state=reuse_state,
            )

        if result_type == _FlowType.ABORT:
            return _handle_subentry_abort(
                flow_id,
                current_step,
                is_reconfigure=is_reconfigure,
                ignored_config_keys=ignored_config_keys,
                remaining_config=remaining_config,
                reuse_state=reuse_state,
            )

        if result_type == _FlowType.MENU:
            menu_choice = _handle_menu_step(
                flow_id,
                current_step,
                remaining_config,
                consumed_menu_selections,
                consumed_menu_selection_keys,
            )
            last_menu_choice = menu_choice
            logger.debug(
                "Config subentry flow step %s: menu %s (step_id=%s)",
                step_num,
                menu_choice,
                current_step.get("step_id"),
            )
            current_step = await _submit_step(
                client.submit_config_subentry_flow_step,
                flow_id,
                {"next_step_id": menu_choice},
                client=client,
                helper_type=None,
                last_menu_choice=last_menu_choice,
                current_step=current_step,
                is_reconfigure=is_reconfigure,
            )
            continue

        if result_type == _FlowType.FORM:
            form_data = _handle_form_step(
                flow_id,
                current_step,
                remaining_config,
                ignored_config_keys,
                reuse_state=reuse_state,
            )
            logger.debug(
                "Config subentry flow step %s: form submit (step_id=%s, keys=%s)",
                step_num,
                current_step.get("step_id"),
                sorted(form_data.keys()),
            )
            current_step = await _submit_step(
                client.submit_config_subentry_flow_step,
                flow_id,
                form_data,
                client=client,
                helper_type=None,
                last_menu_choice=last_menu_choice,
                current_step=current_step,
                is_reconfigure=is_reconfigure,
            )
            continue

        if result_type in {"progress", "progress_done"}:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Config subentry flow requires an asynchronous progress step",
                    suggestions=[
                        "Complete the provider setup in Home Assistant so the external resource is available.",
                        "Retry the same ha_config_set_helper call after the resource is ready.",
                    ],
                    context={"flow_id": flow_id, "details": current_step},
                )
            )

        raise_tool_error(
            create_error_response(
                ErrorCode.INTERNAL_UNEXPECTED,
                f"Unexpected config subentry flow result type: {result_type}",
                context={"flow_id": flow_id, "details": current_step},
            )
        )

    raise_tool_error(
        create_error_response(
            ErrorCode.TIMEOUT_OPERATION,
            f"Config subentry flow exceeded {max_steps} steps",
            context={
                "flow_id": flow_id,
                "max_steps": max_steps,
                "consumed_menu_selections": consumed_menu_selections,
                # Reconfigure-only keys: the reconfigure caller reads both to
                # decide whether to abort the pending flow. Adding them to
                # every flow's error shape would change the public contract of
                # the add-integration, options and helper paths for nothing.
                **(
                    {
                        "flow_budget_exhausted": True,
                        "status": ReconfigureStatus.FLOW_ABORTED_BEFORE_APPLY,
                    }
                    if is_reconfigure
                    else {}
                ),
            },
        )
    )
