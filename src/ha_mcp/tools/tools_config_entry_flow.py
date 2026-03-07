"""
Config Entry Flow API tools for Home Assistant MCP server.

This module provides tools for creating and managing config entry flow-based
helpers (template, group, utility_meter, etc.) via the Config Entry Flow API.
"""

import logging
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
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
        for _ in range(max_steps):
            result = await client.submit_config_flow_step(flow_id, config)

            result_type = result.get("type")
            if result_type == "create_entry":
                return {"success": True, "entry": result}
            elif result_type == "abort":
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Flow aborted: {result.get('reason')}",
                    context={"flow_id": flow_id, "details": result},
                ))
            elif result_type == "form":
                # Need more input - for unified tool, this is an error
                raise_tool_error(create_error_response(
                    ErrorCode.CONFIG_MISSING_REQUIRED_FIELDS,
                    "Multi-step flow requires additional input",
                    suggestions=[
                        "This helper may require manual configuration through the Home Assistant UI",
                        "Use ha_get_helper_schema() to discover required config fields",
                    ],
                    context={
                        "flow_id": flow_id,
                        "step_id": result.get("step_id"),
                        "data_schema": result.get("data_schema"),
                    },
                ))
            else:
                # Unexpected flow result type
                raise_tool_error(create_error_response(
                    ErrorCode.INTERNAL_UNEXPECTED,
                    f"Unexpected flow result type: {result_type}",
                    context={"flow_id": flow_id, "details": result},
                ))

        raise_tool_error(create_error_response(
            ErrorCode.TIMEOUT_OPERATION,
            f"Flow exceeded {max_steps} steps",
            context={"flow_id": flow_id, "max_steps": max_steps},
        ))

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
                    raise_tool_error(create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Config must be a dictionary/object",
                        suggestions=["Provide config as a JSON object, e.g. {\"name\": \"my_helper\"}"],
                        context={"helper_type": helper_type},
                    ))
                config_dict: dict[str, Any] = parsed_config
            else:
                config_dict = config

            # Start flow
            flow_result = await client.start_config_flow(helper_type)
            flow_id = flow_result.get("flow_id")

            if not flow_id:
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Failed to start config flow",
                    suggestions=["Check that the helper type is supported and Home Assistant is reachable"],
                    context={"helper_type": helper_type, "details": flow_result},
                ))

            # Handle flow steps
            result = await _handle_flow_steps(flow_id, config_dict)

            entry = result["entry"].get("result", {})
            return {
                "success": True,
                "entry_id": entry.get("entry_id"),
                "title": entry.get("title"),
                "domain": helper_type,
                "message": f"{helper_type} helper created successfully",
            }

        except ToolError:
            raise
        except Exception as e:
            error_msg = f"Error creating {helper_type} helper"
            if flow_id:
                error_msg += f" (flow_id: {flow_id})"
            logger.error(f"{error_msg}: {e}")

            context = {"helper_type": helper_type}
            if flow_id:
                context["flow_id"] = flow_id
            exception_to_structured_error(e, context=context)

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
                raise_tool_error(create_error_response(
                    ErrorCode.INTERNAL_UNEXPECTED,
                    f"Unexpected flow type: {flow_type}",
                    context={"helper_type": helper_type, "details": flow_result},
                ))

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting helper schema: {e}")
            exception_to_structured_error(e, context={"helper_type": helper_type})
