"""
Config Entry Flow API tools for Home Assistant MCP server.

This module provides tools for creating and managing config entry flow-based
helpers (template, group, utility_meter, etc.) via the Config Entry Flow API.

These helpers use a multi-step Config Entry Flow rather than simple WebSocket
create/update commands. Some flows present a menu first (e.g., group asks for
group type), followed by a form for the actual configuration.
"""

import logging
from typing import Annotated, Any, Literal

from pydantic import Field

from .helpers import exception_to_structured_error, log_tool_usage
from .util_helpers import parse_json_param

logger = logging.getLogger(__name__)

# 15 helpers that use Config Entry Flow API (Issue #324)
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

# Keys that are used to specify menu selections and should not be
# forwarded as form data.
_MENU_SELECTION_KEYS = frozenset({"group_type", "next_step_id", "menu_option"})


def register_config_entry_flow_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Config Entry Flow API tools with the MCP server."""

    async def _handle_flow_steps(
        flow_id: str,
        initial_step: dict[str, Any],
        config: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Walk through a multi-step config entry flow, handling menu and form steps.

        The Config Entry Flow API can return several step types:
        - ``"menu"``: Requires selecting an option via ``{"next_step_id": "..."}``
        - ``"form"``: Requires submitting form field values
        - ``"create_entry"``: Flow completed successfully
        - ``"abort"``: Flow was aborted by HA

        Menu-based flows (e.g. the *group* helper) present a menu first, then
        a form. This function splits the caller-provided *config* dict so that
        menu steps receive only the selection key while form steps receive the
        remaining config fields.

        Args:
            flow_id: Flow ID from ``start_config_flow``
            initial_step: The response from ``start_config_flow`` describing the
                first step (type, step_id, data_schema / menu_options, etc.)
            config: Full configuration data provided by the caller.  Menu
                selection keys (``group_type``, ``next_step_id``,
                ``menu_option``) are consumed by menu steps; the rest is
                submitted on the first form step.

        Returns:
            Result dict with ``success`` flag and either ``entry`` (on success)
            or ``error`` / ``details`` (on failure).
        """
        remaining_config = dict(config)
        current_step = initial_step
        max_steps = 10

        for step_num in range(max_steps):
            result_type = current_step.get("type")

            if result_type == "create_entry":
                return {"success": True, "entry": current_step}

            elif result_type == "abort":
                return {
                    "success": False,
                    "error": f"Flow aborted: {current_step.get('reason')}",
                    "details": current_step,
                }

            elif result_type == "menu":
                # Extract the menu selection from config.
                # Callers can specify it as "group_type", "next_step_id",
                # or "menu_option".
                menu_choice = None
                for key in _MENU_SELECTION_KEYS:
                    if key in remaining_config:
                        menu_choice = remaining_config.pop(key)
                        break

                if not menu_choice:
                    menu_options = current_step.get("menu_options", [])
                    return {
                        "success": False,
                        "error": (
                            "Menu step requires a selection. "
                            "Provide 'group_type' or 'next_step_id' in config."
                        ),
                        "step_id": current_step.get("step_id"),
                        "menu_options": menu_options,
                        "suggestion": (
                            f"Add one of these to your config: {menu_options}. "
                            "Example: {\"group_type\": \"light\", \"name\": \"My Group\", ...}"
                        ),
                    }

                logger.debug(
                    f"Flow step {step_num}: menu selection '{menu_choice}'"
                )
                current_step = await client.submit_config_flow_step(
                    flow_id, {"next_step_id": menu_choice}
                )

            elif result_type == "form":
                # Submit remaining config as form data.
                # Strip any leftover menu selection keys so HA doesn't reject
                # unknown fields.
                form_data = {
                    k: v
                    for k, v in remaining_config.items()
                    if k not in _MENU_SELECTION_KEYS
                }
                logger.debug(
                    f"Flow step {step_num}: submitting form data "
                    f"(step_id={current_step.get('step_id')}, "
                    f"keys={list(form_data.keys())})"
                )
                current_step = await client.submit_config_flow_step(
                    flow_id, form_data
                )
                # After submitting form data once, clear remaining_config so
                # we don't re-submit the same data if there are further steps.
                remaining_config = {}

            else:
                return {
                    "success": False,
                    "error": f"Unexpected flow result type: {result_type}",
                    "details": current_step,
                }

        return {
            "success": False,
            "error": f"Flow exceeded {max_steps} steps",
        }

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["config"],
            "title": "Create Config Entry Helper",
        }
    )
    @log_tool_usage
    async def ha_create_config_entry_helper(
        helper_type: Annotated[
            SUPPORTED_HELPERS, Field(description="Helper type")
        ],
        config: Annotated[
            str | dict, Field(description="Helper config (JSON or dict)")
        ],
    ) -> dict[str, Any]:
        """Create Config Entry Flow helper (template, group, utility_meter, etc.).

        Supports 15 helper types that use Config Entry Flow API.
        Use ha_get_helper_schema(helper_type) to discover required config fields.

        For menu-based helpers (e.g. group), include the menu selection in config:
        - group: {"group_type": "light", "name": "My Group", "entities": [...]}
        - template: {"name": "My Template", "state": "{{ ... }}", ...}
        """
        try:
            flow_id = None  # Track flow_id for error context

            # Parse config if string
            if isinstance(config, str):
                parsed_config = parse_json_param(config)
                if not isinstance(parsed_config, dict):
                    return {
                        "success": False,
                        "error": "Config must be a dictionary/object",
                    }
                config_dict: dict[str, Any] = parsed_config
            else:
                config_dict = config

            # Start flow -- this returns the first step (menu or form)
            flow_result = await client.start_config_flow(helper_type)
            flow_id = flow_result.get("flow_id")

            if not flow_id:
                return {
                    "success": False,
                    "error": "Failed to start config flow",
                    "details": flow_result,
                }

            # Walk through flow steps, passing the initial step so
            # _handle_flow_steps knows whether to handle a menu or form first.
            result = await _handle_flow_steps(
                flow_id, flow_result, config_dict
            )

            if result.get("success"):
                entry = result["entry"].get("result", {})
                return {
                    "success": True,
                    "entry_id": entry.get("entry_id"),
                    "title": entry.get("title"),
                    "domain": helper_type,
                    "message": f"{helper_type} helper created successfully",
                }
            else:
                return result

        except Exception as e:
            error_msg = f"Error creating {helper_type} helper"
            if flow_id:
                error_msg += f" (flow_id: {flow_id})"
            logger.error(f"{error_msg}: {e}")

            context = {"helper_type": helper_type}
            if flow_id:
                context["flow_id"] = flow_id
            return exception_to_structured_error(e, context=context)

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "tags": ["config"],
            "title": "Get Helper Schema",
        }
    )
    @log_tool_usage
    async def ha_get_helper_schema(
        helper_type: Annotated[SUPPORTED_HELPERS, Field(description="Helper type")],
    ) -> dict[str, Any]:
        """Get configuration schema for a helper type.

        Returns the form fields and their types needed to create this helper.
        Use before ha_create_config_entry_helper to understand required config.

        For menu-based helpers (e.g. group), returns the available menu options.
        Call again with menu_option to get the form schema for that option.
        """
        try:
            # Start flow but don't submit anything - just get the schema
            flow_result = await client.start_config_flow(helper_type)

            flow_type = flow_result.get("type")

            # Handle different flow types
            if flow_type == "form":
                # Standard form with data_schema
                return {
                    "success": True,
                    "helper_type": helper_type,
                    "flow_type": "form",
                    "step_id": flow_result.get("step_id"),
                    "data_schema": flow_result.get("data_schema", []),
                    "description_placeholders": flow_result.get(
                        "description_placeholders", {}
                    ),
                    "errors": flow_result.get("errors", {}),
                }

            elif flow_type == "menu":
                # Menu selection (e.g., group type selection)
                return {
                    "success": True,
                    "helper_type": helper_type,
                    "flow_type": "menu",
                    "step_id": flow_result.get("step_id"),
                    "menu_options": flow_result.get("menu_options", []),
                    "description_placeholders": flow_result.get(
                        "description_placeholders", {}
                    ),
                    "note": (
                        "This helper requires selecting from a menu first. "
                        "Include 'group_type' (or 'next_step_id') in your config "
                        "when calling ha_create_config_entry_helper."
                    ),
                }

            else:
                # Unexpected flow type
                return {
                    "success": False,
                    "error": f"Unexpected flow type: {flow_type}",
                    "details": flow_result,
                }

        except Exception as e:
            logger.error(f"Error getting helper schema: {e}")
            return exception_to_structured_error(e, context={"helper_type": helper_type})
