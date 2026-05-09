"""
Config Entry Flow API tools for Home Assistant MCP server.

This module provides the shared machinery for creating and updating
config-entry-based helpers (template, group, utility_meter, etc.) via the
Config Entry Flow API, plus the ha_get_helper_schema tool.

The create/update entry point is the unified ha_config_set_helper tool in
tools_config_helpers.py, which routes to create_flow_helper / update_flow_helper
for the 15 helper types listed in FLOW_HELPER_TYPES.
"""

import asyncio
import logging
from enum import StrEnum
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..client.rest_client import HomeAssistantAPIError
from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)

logger = logging.getLogger(__name__)

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
FLOW_HELPER_TYPES: frozenset[str] = frozenset({
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
})

# Issue #1149: full set accepted by ha_get_helper_schema (15 flow + 12
# simple). Simple types route to a static dict in tools_config_helpers
# rather than starting an HA flow.
ALL_HELPER_TYPES = Literal[
    # Flow helpers (mirrors SUPPORTED_HELPERS above)
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
    # Simple helpers (mirrors SIMPLE_HELPER_TYPES in tools_config_helpers)
    "input_button",
    "input_boolean",
    "input_select",
    "input_number",
    "input_text",
    "input_datetime",
    "counter",
    "timer",
    "schedule",
    "zone",
    "person",
    "tag",
]

# Keys used to specify a menu selection — stripped before submitting form data.
_MENU_SELECTION_KEYS = frozenset({"group_type", "next_step_id", "menu_option"})


class _FlowType(StrEnum):
    """HA config flow result type strings."""
    FORM = "form"
    MENU = "menu"
    ABORT = "abort"
    CREATE_ENTRY = "create_entry"


# ---------------------------------------------------------------------------
# Module-level flow machinery
#
# These functions are shared by the unified ha_config_set_helper tool in
# tools_config_helpers.py and by ha_get_helper_schema below. They take a
# client instance as an explicit parameter so they can be called from either
# a closure-registered tool or a class method.
# ---------------------------------------------------------------------------


def _handle_menu_step(
    flow_id: str,
    current_step: dict[str, Any],
    remaining_config: dict[str, Any],
) -> str:
    """Extract menu selection from config, raising on missing selection.

    Returns the menu choice string. Mutates remaining_config to pop
    the consumed selection key.
    """
    menu_choice = None
    for key in _MENU_SELECTION_KEYS:
        if key in remaining_config:
            menu_choice = remaining_config.pop(key)
            break

    if not menu_choice:
        menu_options = current_step.get("menu_options", [])
        raise_tool_error(create_error_response(
            ErrorCode.CONFIG_MISSING_REQUIRED_FIELDS,
            "Menu step requires a selection. "
            "Add 'group_type' or 'next_step_id' to your config.",
            suggestions=[
                f"Available options: {menu_options}",
                "Example: {\"group_type\": \"light\", \"name\": \"My Group\", ...}",
            ],
            context={
                "flow_id": flow_id,
                "step_id": current_step.get("step_id"),
                "menu_options": menu_options,
            },
        ))

    return str(menu_choice)


def _extract_schema_field_names(data_schema: Any) -> set[str] | None:
    """Extract the set of field names declared by a step's data_schema.

    HA returns data_schema as a list of {name, selector, required, ...} dicts.
    Returns ``None`` when the schema is absent or not a list (signalling
    the caller to fall back to legacy submit-all behaviour). Returns a
    (possibly empty) set when the schema is present and parseable.
    """
    if not isinstance(data_schema, list):
        return None
    names: set[str] = set()
    for field in data_schema:
        if isinstance(field, dict):
            name = field.get("name")
            if isinstance(name, str):
                names.add(name)
    return names


def _handle_form_step(
    flow_id: str,
    current_step: dict[str, Any],
    remaining_config: dict[str, Any],
) -> dict[str, Any]:
    """Validate a form step and return form data to submit.

    When the step's ``data_schema`` is provided, pops ONLY the keys declared
    in that schema from ``remaining_config`` (mutating it) so any unconsumed
    keys remain available for subsequent steps. Menu selection keys are never
    submitted.

    When ``data_schema`` is absent (HA didn't tell us field names), falls
    back to legacy behaviour: submit all non-menu keys and clear them. This
    keeps single-step flows working when HA omits the schema.

    Raises ToolError on validation errors.
    """
    if current_step.get("errors"):
        raise_tool_error(create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            "Form validation failed",
            suggestions=["Fix the field errors and retry with corrected values"],
            context={
                "flow_id": flow_id,
                "step_id": current_step.get("step_id"),
                "errors": current_step["errors"],
                "data_schema": current_step.get("data_schema"),
            },
        ))

    schema_fields = _extract_schema_field_names(current_step.get("data_schema"))

    form_data: dict[str, Any] = {}
    if schema_fields is None:
        # Legacy fallback: no schema info — dump every non-menu key and
        # consume them all so a follow-up step (rare without schema) won't
        # re-submit the same data.
        for key in list(remaining_config.keys()):
            if key in _MENU_SELECTION_KEYS:
                continue
            form_data[key] = remaining_config.pop(key)
    else:
        for key in list(remaining_config.keys()):
            if key in _MENU_SELECTION_KEYS:
                continue
            if key in schema_fields:
                form_data[key] = remaining_config.pop(key)

    return form_data


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
                key: val
                for key, val in errors_field.items()
                if isinstance(key, str)
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


async def _fetch_data_schema_for_error_context(
    client: Any,
    helper_type: str | None,
    menu_choice: str | None,
) -> list[Any] | None:
    """Best-effort fetch of the helper's data_schema for error context.

    Starts a fresh introspection flow (always aborted), and returns the
    user step's ``data_schema`` so the LLM has something concrete to react
    to when HA's error body is unstructured. Returns ``None`` on any
    failure or when the helper is menu-based without a chosen branch.

    Public alias ``fetch_helper_data_schema`` is exported below for the
    pre-flow validation gates in ``_handle_flow_helper`` (issue #1149) —
    they need the same best-effort fetch but live in another module.
    """
    if not helper_type or client is None:
        return None
    intro_flow_id: str | None = None
    try:
        flow_result = await client.start_config_flow(helper_type)
        intro_flow_id = flow_result.get("flow_id")
        flow_type = flow_result.get("type")

        if flow_type == _FlowType.FORM:
            schema = flow_result.get("data_schema")
            return schema if isinstance(schema, list) else None

        if flow_type == _FlowType.MENU and menu_choice and intro_flow_id:
            try:
                step = await asyncio.wait_for(
                    client.submit_config_flow_step(
                        intro_flow_id, {"next_step_id": menu_choice}
                    ),
                    timeout=10.0,
                )
            except Exception:
                return None
            if step.get("type") == _FlowType.FORM:
                schema = step.get("data_schema")
                return schema if isinstance(schema, list) else None
        return None
    except Exception:
        return None
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


# Public alias for use by pre-flow validation gates in tools_config_helpers
# (issue #1149). The underscore-prefixed original is kept to preserve the
# call sites already in this module; the alias avoids importing a private
# name across modules.
fetch_helper_data_schema = _fetch_data_schema_for_error_context


async def _raise_flow_api_error(
    api_error: HomeAssistantAPIError,
    *,
    client: Any,
    flow_id: str,
    helper_type: str | None,
    menu_choice: str | None,
    current_step: dict[str, Any] | None,
    submitted: dict[str, Any] | None,
) -> None:
    """Translate an HA 4xx during a flow submit into a structured ToolError.

    For 400/422 responses, parses ``response_data`` for field-level info
    via ``_parse_flow_api_error``. When the body is unstructured (no
    ``errors`` map), attaches the helper's ``data_schema`` (if it can be
    fetched) so the caller has actionable information.

    Always raises ``ToolError`` — never returns.
    """
    parsed = _parse_flow_api_error(api_error)
    field_errors = parsed["field_errors"]
    status_code = api_error.status_code or 0

    context: dict[str, Any] = {
        "flow_id": flow_id,
        "status_code": status_code,
    }
    if helper_type:
        context["helper_type"] = helper_type
    if menu_choice:
        context["menu_choice"] = menu_choice
    if current_step is not None:
        context["step_id"] = current_step.get("step_id")
    if submitted is not None:
        context["submitted_keys"] = sorted(submitted.keys())
    if parsed["raw"] is not None:
        context["response_body"] = parsed["raw"]

    suggestions: list[str] = []
    message: str

    if field_errors:
        # Structured field errors — tell the caller which fields failed.
        context["field_errors"] = field_errors
        readable = ", ".join(f"{k}: {v}" for k, v in field_errors.items())
        message = f"Helper validation failed — {readable}"
        suggestions.append(
            "Fix the field(s) listed in 'field_errors' and retry the call."
        )
        # Issue #1149: also attach the data_schema so the LLM sees the field
        # shape (selector, required, ...) alongside the per-field error
        # codes — symmetric with the unstructured-error branch below.
        # `field_errors` tells "what failed", `data_schema` tells "what's
        # accepted"; together they're enough for self-correction.
        schema = await _fetch_data_schema_for_error_context(
            client, helper_type, menu_choice
        )
        if schema is not None:
            context["data_schema"] = schema
    else:
        # Unstructured — attach the data_schema so the LLM has something to use.
        message = (
            f"Home Assistant rejected the {helper_type or 'flow'} request "
            f"({status_code}): {parsed['message']}"
        )
        schema = await _fetch_data_schema_for_error_context(
            client, helper_type, menu_choice
        )
        if schema is not None:
            context["data_schema"] = schema
            suggestions.append(
                "Inspect 'data_schema' in this error to see the fields HA expects, "
                "then retry with a corrected config."
            )
        suggestions.append(
            f"Call ha_get_helper_schema(helper_type='{helper_type}') for the "
            f"full field list and selectors." if helper_type else
            "Call ha_get_helper_schema for this helper to see required fields."
        )

    raise_tool_error(create_error_response(
        ErrorCode.SERVICE_CALL_FAILED,
        message,
        suggestions=suggestions,
        context=context,
    ))


async def _handle_flow_steps(
    client: Any,
    flow_id: str,
    initial_step: dict[str, Any],
    config: dict[str, Any],
    submit_fn: Any = None,
    helper_type: str | None = None,
) -> dict[str, Any]:
    """Walk a multi-step config flow handling menu and form steps (max 10 steps).

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
            consumed by menu steps; remaining keys are submitted on the
            first form step.
        submit_fn: Async function to submit a step. Defaults to
            client.submit_config_flow_step (create). Pass
            client.submit_options_flow_step for options (update) flows.
        helper_type: Optional helper type (e.g. ``"statistics"``). When
            provided, surfaces the helper's data_schema in error context
            for unstructured HA 4xx responses so the caller can react.

    Returns:
        ``{"success": True, "entry": result}`` on success.
        Raises ToolError on any failure.
    """
    if submit_fn is None:
        submit_fn = client.submit_config_flow_step
    remaining_config = dict(config)
    current_step = initial_step
    last_menu_choice: str | None = None
    max_steps = 10

    for step_num in range(max_steps):
        result_type = current_step.get("type")

        if result_type == _FlowType.CREATE_ENTRY:
            return {"success": True, "entry": current_step}

        if result_type == _FlowType.ABORT:
            raise_tool_error(create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Flow aborted: {current_step.get('reason')}",
                context={"flow_id": flow_id, "details": current_step},
            ))

        if result_type == _FlowType.MENU:
            menu_choice = _handle_menu_step(flow_id, current_step, remaining_config)
            last_menu_choice = menu_choice
            logger.debug(
                f"Flow step {step_num}: menu '{menu_choice}' "
                f"(step_id={current_step.get('step_id')})"
            )
            menu_payload = {"next_step_id": menu_choice}
            try:
                current_step = await asyncio.wait_for(
                    submit_fn(flow_id, menu_payload),
                    timeout=20.0,
                )
            except HomeAssistantAPIError as api_err:
                if api_err.status_code in (400, 422):
                    await _raise_flow_api_error(
                        api_err,
                        client=client,
                        flow_id=flow_id,
                        helper_type=helper_type,
                        menu_choice=last_menu_choice,
                        current_step=current_step,
                        submitted=menu_payload,
                    )
                raise

        elif result_type == _FlowType.FORM:
            # _handle_form_step pops only the keys declared in the current
            # step's data_schema, leaving any other keys in remaining_config
            # for subsequent steps (HA can present multi-step forms, e.g.
            # statistics: user step then pick-characteristic step).
            form_data = _handle_form_step(flow_id, current_step, remaining_config)
            logger.debug(
                f"Flow step {step_num}: form submit "
                f"(step_id={current_step.get('step_id')}, keys={list(form_data.keys())})"
            )
            try:
                current_step = await asyncio.wait_for(
                    submit_fn(flow_id, form_data),
                    timeout=20.0,
                )
            except HomeAssistantAPIError as api_err:
                if api_err.status_code in (400, 422):
                    await _raise_flow_api_error(
                        api_err,
                        client=client,
                        flow_id=flow_id,
                        helper_type=helper_type,
                        menu_choice=last_menu_choice,
                        current_step=current_step,
                        submitted=form_data,
                    )
                raise

        else:
            raise_tool_error(create_error_response(
                ErrorCode.INTERNAL_UNEXPECTED,
                f"Unexpected flow result type: {result_type}",
                context={"flow_id": flow_id, "details": current_step},
            ))

    raise_tool_error(create_error_response(
        ErrorCode.TIMEOUT_OPERATION,
        f"Flow exceeded {max_steps} steps",
        context={"flow_id": flow_id, "max_steps": max_steps},
    ))


async def get_user_step_field_names(
    client: Any, helper_type: str
) -> set[str] | None:
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
                await asyncio.wait_for(
                    client.abort_config_flow(flow_id), timeout=5.0
                )
            except Exception as abort_err:
                logger.warning(
                    f"Failed to abort introspection flow {flow_id}: {abort_err}"
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
    config_entry = await client.get_config_entry(entry_id)
    actual_domain = config_entry.get("domain")
    if actual_domain != helper_type:
        raise_tool_error(create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"entry_id '{entry_id}' belongs to domain '{actual_domain}', not '{helper_type}'",
            suggestions=[
                f"Use ha_get_integration(domain='{helper_type}') to find valid entry IDs",
            ],
            context={"entry_id": entry_id, "expected": helper_type, "actual": actual_domain},
        ))

    flow_result = await client.start_options_flow(entry_id)
    flow_id = flow_result.get("flow_id")

    if not flow_id:
        raise_tool_error(create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Failed to start options flow",
            suggestions=["Check that the entry supports options (supports_options=true)"],
            context={"entry_id": entry_id, "details": flow_result},
        ))

    try:
        result = await _handle_flow_steps(
            client, flow_id, flow_result, config_dict,
            submit_fn=client.submit_options_flow_step,
            helper_type=helper_type,
        )
    except Exception:
        try:
            await asyncio.wait_for(client.abort_options_flow(flow_id), timeout=5.0)
        except Exception as abort_err:
            logger.warning(f"Failed to abort options flow {flow_id} after error: {abort_err}")
        raise

    entry = result["entry"].get("result", {})
    return {
        "success": True,
        "entry_id": entry_id,
        "title": entry.get("title"),
        "domain": helper_type,
        "message": f"{helper_type} helper updated successfully",
        "updated": True,
    }


async def create_flow_helper(
    client: Any,
    helper_type: str,
    config_dict: dict[str, Any],
) -> dict[str, Any]:
    """Create a new flow-based helper via the config flow.

    Starts a config flow, walks the flow steps, and returns the result.
    Aborts the flow on error.
    """
    flow_result = await client.start_config_flow(helper_type)
    flow_id = flow_result.get("flow_id")

    if not flow_id:
        raise_tool_error(create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Failed to start config flow",
            suggestions=["Check that the helper type is supported and Home Assistant is reachable"],
            context={"helper_type": helper_type, "details": flow_result},
        ))

    try:
        result = await _handle_flow_steps(
            client, flow_id, flow_result, config_dict,
            helper_type=helper_type,
        )
    except Exception:
        try:
            await asyncio.wait_for(client.abort_config_flow(flow_id), timeout=5.0)
        except Exception as abort_err:
            logger.warning(f"Failed to abort config flow {flow_id} after error: {abort_err}")
        raise

    entry = result["entry"].get("result", {})
    return {
        "success": True,
        "entry_id": entry.get("entry_id"),
        "title": entry.get("title"),
        "domain": helper_type,
        "message": f"{helper_type} helper created successfully",
    }


# ---------------------------------------------------------------------------
# Schema introspection tool (ha_get_helper_schema)
# ---------------------------------------------------------------------------


class ConfigEntryFlowTools:
    """Schema introspection tool for Config Entry Flow helpers."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_get_helper_schema",
        tags={"Helper Entities"},
        annotations={
            "readOnlyHint": True,
            "title": "Get Helper Schema"
        }
    )
    @log_tool_usage
    async def ha_get_helper_schema(
        self,
        helper_type: Annotated[ALL_HELPER_TYPES, Field(description="Helper type")],
        menu_option: Annotated[
            str | None,
            Field(
                description=(
                    "For menu-based flow helpers (template, group): the sub-type to "
                    "inspect (e.g. 'sensor' or 'binary_sensor' for template). Omit to "
                    "see available menu options first. Ignored for simple helpers."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get configuration schema for a helper type.

        Returns the field list and types needed to create this helper. Use
        before ha_config_set_helper when unsure of the required `config`
        (flow helpers) or required typed parameters (simple helpers). The
        same schema is also auto-attached to validation-error responses
        from ha_config_set_helper, so an explicit pre-call is optional.

        Three branches:

        1. Simple helpers (input_*, counter, timer, schedule, zone, person,
           tag): static schema returned from a built-in dict.
              ha_get_helper_schema("input_select")
              → {flow_type: "form", data_schema: [{name: "name", required: True, ...}, ...]}

        2. Form-based flow helpers (min_max, utility_meter, statistics, ...):
           starts a fresh HA flow, reads the schema, and aborts the flow.
              ha_get_helper_schema("min_max")
              → {flow_type: "form", data_schema: [...]}

        3. Menu-based flow helpers (template, group): two-call workflow.
              # Step 1 — discover sub-types:
              ha_get_helper_schema("template")
              → {flow_type: "menu", menu_options: ["sensor", "binary_sensor", ...]}

              # Step 2 — inspect form fields for a sub-type:
              ha_get_helper_schema("template", menu_option="sensor")
              → {flow_type: "form", menu_option: "sensor", data_schema: [...]}
        """
        # Issue #1149: simple-helper dispatch — return a static schema
        # without round-tripping HA. Lazy import keeps the existing
        # tools_config_helpers ↔ tools_config_entry_flow boundary intact.
        from .tools_config_helpers import (
            SIMPLE_HELPER_TYPES,
            get_simple_helper_schema,
        )

        if helper_type in SIMPLE_HELPER_TYPES:
            schema = get_simple_helper_schema(helper_type)
            # Invariant in tools_config_helpers asserts every simple type
            # has a schema entry — None here would indicate a developer
            # error that should fail loudly rather than mask as empty.
            if schema is None:
                raise_tool_error(create_error_response(
                    ErrorCode.INTERNAL_UNEXPECTED,
                    f"No simple-helper schema registered for '{helper_type}'",
                    context={"helper_type": helper_type},
                ))
            if menu_option is not None:
                # Simple helpers have no menu — flag the misuse rather than
                # silently ignore the parameter.
                raise_tool_error(create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"menu_option is not applicable to simple helper "
                    f"'{helper_type}' (only flow helpers like 'template' "
                    f"and 'group' use menus).",
                    suggestions=["Omit menu_option for simple helpers."],
                    context={"helper_type": helper_type},
                ))
            return {
                "success": True,
                "helper_type": helper_type,
                "flow_type": _FlowType.FORM,
                "step_id": "user",
                "data_schema": schema,
                "description_placeholders": {},
            }

        flow_id = None  # Track flow_id for error context
        try:
            flow_result = await self._client.start_config_flow(helper_type)
            flow_id = flow_result.get("flow_id")
            flow_type = flow_result.get("type")

            if flow_type == _FlowType.ABORT:
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Could not get schema, flow aborted: {flow_result.get('reason')}",
                    context={"helper_type": helper_type, "details": flow_result},
                ))

            if not flow_id:
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Failed to start config flow — no flow_id returned",
                    context={"helper_type": helper_type, "details": flow_result},
                ))

            if menu_option is not None:
                return await self._get_schema_with_menu_option(
                    helper_type, menu_option, flow_id, flow_result, flow_type,
                )

            return self._build_top_level_schema(helper_type, flow_result, flow_type)

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting helper schema: {e}")
            exception_to_structured_error(e, context={"helper_type": helper_type})
        finally:
            # Always abort the introspection flow to avoid leaking it in HA memory.
            if flow_id:
                try:
                    await self._client.abort_config_flow(flow_id)
                except Exception as abort_err:
                    logger.warning(f"Failed to abort introspection flow {flow_id}: {abort_err}")

    async def _get_schema_with_menu_option(
        self,
        helper_type: str,
        menu_option: str,
        flow_id: str,
        flow_result: dict[str, Any],
        flow_type: str | None,
    ) -> dict[str, Any]:
        """Submit a menu selection and return the resulting form schema.

        Validates that the flow is a menu type, submits the menu option,
        and returns the form schema for the selected sub-type.
        """
        if flow_type != _FlowType.MENU:
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"menu_option is not applicable to '{helper_type}' "
                f"(flow type is '{flow_type}', not 'menu')",
                suggestions=["Omit menu_option for form-based helpers"],
                context={"helper_type": helper_type, "flow_type": flow_type},
            ))

        step_result = await self._client.submit_config_flow_step(
            flow_id, {"next_step_id": menu_option}
        )
        sub_flow_type = step_result.get("type")

        if sub_flow_type == _FlowType.ABORT:
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"menu_option '{menu_option}' is not valid for '{helper_type}': "
                f"{step_result.get('reason')}",
                suggestions=[f"Valid options: {flow_result.get('menu_options', [])}"],
                context={
                    "helper_type": helper_type,
                    "menu_option": menu_option,
                    "details": step_result,
                },
            ))

        if sub_flow_type != _FlowType.FORM:
            raise_tool_error(create_error_response(
                ErrorCode.INTERNAL_UNEXPECTED,
                f"Unexpected sub-flow type '{sub_flow_type}' after menu selection",
                context={
                    "helper_type": helper_type,
                    "menu_option": menu_option,
                    "details": step_result,
                },
            ))

        return {
            "success": True,
            "helper_type": helper_type,
            "flow_type": _FlowType.FORM,
            "menu_option": menu_option,
            "step_id": step_result.get("step_id"),
            "data_schema": step_result.get("data_schema", []),
            "description_placeholders": step_result.get("description_placeholders", {}),
        }

    @staticmethod
    def _build_top_level_schema(
        helper_type: str,
        flow_result: dict[str, Any],
        flow_type: str | None,
    ) -> dict[str, Any]:
        """Build the top-level schema response for a form or menu flow."""
        if flow_type == _FlowType.FORM:
            return {
                "success": True,
                "helper_type": helper_type,
                "flow_type": _FlowType.FORM,
                "step_id": flow_result.get("step_id"),
                "data_schema": flow_result.get("data_schema", []),
                "description_placeholders": flow_result.get(
                    "description_placeholders", {}
                ),
            }
        if flow_type == _FlowType.MENU:
            return {
                "success": True,
                "helper_type": helper_type,
                "flow_type": _FlowType.MENU,
                "step_id": flow_result.get("step_id"),
                "menu_options": flow_result.get("menu_options", []),
                "description_placeholders": flow_result.get(
                    "description_placeholders", {}
                ),
                "note": (
                    "This helper requires selecting from a menu first. "
                    "Include 'group_type' (or 'next_step_id') in your config "
                    "when calling ha_config_set_helper. "
                    "Call ha_get_helper_schema with menu_option=<sub-type> to inspect form fields."
                ),
            }
        raise_tool_error(create_error_response(
            ErrorCode.INTERNAL_UNEXPECTED,
            f"Unexpected flow type: {flow_type}",
            context={"helper_type": helper_type, "details": flow_result},
        ))


def register_config_entry_flow_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Config Entry Flow API tools with the MCP server."""
    register_tool_methods(mcp, ConfigEntryFlowTools(client))
