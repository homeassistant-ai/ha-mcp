"""
Config Entry Flow API tools for Home Assistant MCP server.

This module provides tools for creating and managing config entry flow-based
helpers (template, group, utility_meter, etc.) via the Config Entry Flow API.
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


def register_config_entry_flow_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Config Entry Flow API tools with the MCP server."""

    async def _handle_flow_steps(
        flow_id: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Handle multi-step flow internally (max 10 steps).

        Args:
            flow_id: Flow ID from start_config_flow
            config: Configuration data to submit

        Returns:
            Result dict with success/error and flow details
        """
        max_steps = 10
        for step_num in range(max_steps):
            result = await client.submit_config_flow_step(flow_id, config)

            result_type = result.get("type")
            if result_type == "create_entry":
                return {"success": True, "entry": result}
            elif result_type == "abort":
                return {
                    "success": False,
                    "error": f"Flow aborted: {result.get('reason')}",
                    "details": result,
                }
            elif result_type == "form":
                # Need more input - for unified tool, this is an error
                return {
                    "success": False,
                    "error": "Multi-step flow requires additional input",
                    "step_id": result.get("step_id"),
                    "data_schema": result.get("data_schema"),
                    "suggestion": "This helper may require manual configuration through the Home Assistant UI",
                }
            else:
                # Unexpected flow result type
                return {
                    "success": False,
                    "error": f"Unexpected flow result type: {result_type}",
                    "details": result,
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

            # Start flow
            flow_result = await client.start_config_flow(helper_type)
            flow_id = flow_result.get("flow_id")

            if not flow_id:
                return {
                    "success": False,
                    "error": "Failed to start config flow",
                    "details": flow_result,
                }

            # Handle flow steps
            result = await _handle_flow_steps(flow_id, config_dict)

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
                    "note": "This helper requires selecting from a menu first. Choose an option and submit to get the actual configuration schema.",
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

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "tags": ["config"],
            "title": "Get Config Entry",
        }
    )
    @log_tool_usage
    async def ha_get_config_entry(
        entry_id: Annotated[str, Field(description="Config entry ID")],
    ) -> dict[str, Any]:
        """Get config entry details.

        Use ha_list_integrations() to find entry IDs.
        """
        try:
            result = await client.get_config_entry(entry_id)
            return {"success": True, "entry": result}
        except Exception as e:
            logger.error(f"Error getting config entry: {e}")
            return exception_to_structured_error(e, context={"entry_id": entry_id})
