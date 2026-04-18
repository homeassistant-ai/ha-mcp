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


def _handle_form_step(
    flow_id: str,
    current_step: dict[str, Any],
    remaining_config: dict[str, Any],
) -> dict[str, Any]:
    """Validate a form step and return form data to submit.

    Raises ToolError on validation errors. Returns the filtered form data
    (menu selection keys stripped).
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

    return {
        k: v
        for k, v in remaining_config.items()
        if k not in _MENU_SELECTION_KEYS
    }


async def _handle_flow_steps(
    client: Any,
    flow_id: str,
    initial_step: dict[str, Any],
    config: dict[str, Any],
    submit_fn: Any = None,
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

    Returns:
        ``{"success": True, "entry": result}`` on success.
        Raises ToolError on any failure.
    """
    if submit_fn is None:
        submit_fn = client.submit_config_flow_step
    remaining_config = dict(config)
    current_step = initial_step
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
            logger.debug(
                f"Flow step {step_num}: menu '{menu_choice}' "
                f"(step_id={current_step.get('step_id')})"
            )
            current_step = await asyncio.wait_for(
                submit_fn(flow_id, {"next_step_id": menu_choice}),
                timeout=20.0,
            )

        elif result_type == _FlowType.FORM:
            form_data = _handle_form_step(flow_id, current_step, remaining_config)
            logger.debug(
                f"Flow step {step_num}: form submit "
                f"(step_id={current_step.get('step_id')}, keys={list(form_data.keys())})"
            )
            current_step = await asyncio.wait_for(
                submit_fn(flow_id, form_data),
                timeout=20.0,
            )
            # Clear so subsequent steps don't re-submit the same data.
            remaining_config = {}

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
        )
    except Exception:
        try:
            await asyncio.wait_for(client.abort_options_flow(flow_id), timeout=5.0)
        except Exception as abort_err:
            logger.debug(f"Failed to abort options flow {flow_id} after error: {abort_err}")
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
        result = await _handle_flow_steps(client, flow_id, flow_result, config_dict)
    except Exception:
        try:
            await asyncio.wait_for(client.abort_config_flow(flow_id), timeout=5.0)
        except Exception as abort_err:
            logger.debug(f"Failed to abort config flow {flow_id} after error: {abort_err}")
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
        helper_type: Annotated[SUPPORTED_HELPERS, Field(description="Helper type")],
        menu_option: Annotated[
            str | None,
            Field(
                description=(
                    "For menu-based helpers: the sub-type to inspect (e.g. 'sensor' or "
                    "'binary_sensor' for template). Omit to see available menu options first."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get configuration schema for a helper type.

        Returns the form fields and their types needed to create this helper.
        Use before ha_config_set_helper to understand required config.

        Two-call workflow for menu-based helpers (template, group):

          # Step 1 — discover sub-types:
          ha_get_helper_schema("template")
          → {flow_type: "menu", menu_options: ["sensor", "binary_sensor", ...]}

          # Step 2 — inspect form fields for a sub-type:
          ha_get_helper_schema("template", menu_option="sensor")
          → {flow_type: "form", menu_option: "sensor", data_schema: [{name: "state", ...}, ...]}

        For form-based helpers (min_max, utility_meter, etc.), omit menu_option.
        """
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
                    logger.debug(f"Failed to abort introspection flow {flow_id}: {abort_err}")

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
