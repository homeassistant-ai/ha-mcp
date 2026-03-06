"""
Config Entry Options Flow tools for Home Assistant MCP server.

This module provides tools for configuring existing integrations via their
options flow — equivalent to clicking "Configure" on an integration in the
Home Assistant UI.

Typical workflow:
1. ``ha_get_integration()`` → find entry IDs, check ``supports_options``
2. ``ha_start_options_flow(entry_id)`` → get flow_id + first step schema
3. ``ha_submit_options_flow_step(flow_id, data)`` → navigate menu/form steps
4. Repeat step 3 until type == "create_entry" (options saved automatically)
5. Call ``ha_abort_options_flow(flow_id)`` only to cancel without saving
"""

import logging
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .util_helpers import parse_json_param

logger = logging.getLogger(__name__)


def register_config_entry_options_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Config Entry Options Flow tools with the MCP server."""

    @mcp.tool(
        annotations={
            "destructiveHint": False,
            "tags": ["config"],
            "title": "Start Options Flow",
        }
    )
    @log_tool_usage
    async def ha_start_options_flow(
        entry_id: Annotated[
            str,
            Field(
                description=(
                    "Config entry ID to configure. "
                    "Use ha_get_integration() to find entry IDs "
                    "(look for supports_options=true)."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Start an options flow to configure an existing integration.

        Returns flow_id and the first step (type, step_id, data_schema or menu_options).
        Use ha_submit_options_flow_step() to navigate steps.

        IMPORTANT: If you only want to inspect the schema without saving,
        call ha_abort_options_flow(flow_id) when done. Leaving a flow open
        may block new options flows for the same entry on some integrations.
        """
        try:
            result = await client.start_options_flow(entry_id)
            return {
                "success": True,
                "flow_id": result.get("flow_id"),
                "step_id": result.get("step_id"),
                "type": result.get("type"),
                "menu_options": result.get("menu_options"),
                "data_schema": result.get("data_schema"),
                "description_placeholders": result.get("description_placeholders"),
                "errors": result.get("errors"),
            }
        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error starting options flow for {entry_id}: {e}")
            exception_to_structured_error(
                e,
                context={"entry_id": entry_id},
                suggestions=[
                    "Use ha_get_integration() to confirm the entry exists",
                    "Check that supports_options=true for this entry",
                ],
            )

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["config"],
            "title": "Submit Options Flow Step",
        }
    )
    @log_tool_usage
    async def ha_submit_options_flow_step(
        flow_id: Annotated[
            str,
            Field(description="Flow ID from ha_start_options_flow or a previous step"),
        ],
        data: Annotated[
            str | dict,
            Field(
                description=(
                    "Step data as JSON object. "
                    "For menu steps: {\"next_step_id\": \"option_name\"}. "
                    "For form steps: field values matching data_schema."
                )
            ),
        ],
    ) -> dict[str, Any]:
        """Submit data for an options flow step.

        For menu steps: {"next_step_id": "option_name"}
        For form steps: {"field1": "value1", "field2": true, ...}

        Repeat until type == "create_entry" — options are saved automatically
        when the final form step is submitted. Use ha_abort_options_flow()
        only if you need to cancel without saving.
        """
        try:
            if isinstance(data, str):
                parsed = parse_json_param(data)
                if not isinstance(parsed, dict):
                    raise_tool_error(create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Data must be a JSON object",
                        suggestions=["Provide data as {\"key\": \"value\"} pairs"],
                        context={"flow_id": flow_id},
                    ))
                data_dict: dict[str, Any] = parsed
            else:
                data_dict = data

            result = await client.submit_options_flow_step(flow_id, data_dict)
            return {
                "success": True,
                "flow_id": result.get("flow_id"),
                "step_id": result.get("step_id"),
                "type": result.get("type"),
                "menu_options": result.get("menu_options"),
                "data_schema": result.get("data_schema"),
                "description_placeholders": result.get("description_placeholders"),
                "errors": result.get("errors"),
            }
        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error submitting options flow step {flow_id}: {e}")
            exception_to_structured_error(
                e,
                context={"flow_id": flow_id},
                suggestions=[
                    "Verify flow_id is still valid (flows expire on HA restart)",
                    "Use ha_start_options_flow() to start a new flow",
                ],
            )

    @mcp.tool(
        annotations={
            "destructiveHint": False,
            "tags": ["config"],
            "title": "Abort Options Flow",
        }
    )
    @log_tool_usage
    async def ha_abort_options_flow(
        flow_id: Annotated[
            str,
            Field(description="Flow ID to abort (from ha_start_options_flow)"),
        ],
    ) -> dict[str, Any]:
        """Abort an in-progress options flow without saving changes.

        Use this to cancel a flow started by ha_start_options_flow() when
        you don't want to save the configuration changes. Options flows that
        complete normally (type == "create_entry") do not need to be aborted.
        """
        try:
            await client.abort_options_flow(flow_id)
            return {"success": True, "flow_id": flow_id, "message": "Flow aborted"}
        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error aborting options flow {flow_id}: {e}")
            exception_to_structured_error(
                e,
                context={"flow_id": flow_id},
                suggestions=[
                    "Flow may have already completed or expired — this is safe to ignore",
                ],
            )
