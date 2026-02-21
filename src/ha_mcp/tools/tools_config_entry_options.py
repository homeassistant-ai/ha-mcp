"""
Config Entry Options Flow API tools for Home Assistant MCP server.

This module provides tools for configuring existing integrations via their
options flow - the settings UI you see when clicking "Configure" on an
integration in the Home Assistant UI.
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from ..errors import create_validation_error
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .util_helpers import parse_json_param

logger = logging.getLogger(__name__)


def register_config_entry_options_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Config Entry Options Flow API tools with the MCP server."""

    @mcp.tool(
        annotations={
            "destructiveHint": False,
            "tags": ["config"],
            "title": "Start Options Flow",
        }
    )
    @log_tool_usage
    async def ha_start_config_entry_options_flow(
        entry_id: Annotated[
            str,
            Field(description="Config entry ID. Use ha_get_integration() to find entries with supports_options=true."),
        ],
    ) -> dict[str, Any]:
        """Start options flow for a config entry.

        Use ha_get_integration() to discover entry IDs (look for supports_options=true).
        Returns flow_id and first step info (menu_options or data_schema).
        Use the flow_id with ha_submit_config_entry_options_step to navigate the flow.

        Flow types:
        - "menu": Use {"next_step_id": "option_name"} to navigate
        - "form": Submit form data matching data_schema fields
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
            }
        except Exception as e:
            logger.error(f"Error starting options flow for {entry_id}: {e}")
            return exception_to_structured_error(e, context={"entry_id": entry_id})

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["config"],
            "title": "Submit Options Flow Step",
        }
    )
    @log_tool_usage
    async def ha_submit_config_entry_options_step(
        flow_id: Annotated[
            str,
            Field(description="Flow ID from ha_start_config_entry_options_flow"),
        ],
        data: Annotated[
            str | dict,
            Field(
                description='Form data or menu selection as JSON. For menus: {"next_step_id": "section_name"}. For forms: field values matching data_schema.'
            ),
        ],
    ) -> dict[str, Any]:
        """Submit data for an options flow step.

        For menu steps: {"next_step_id": "option_name"}
        For form steps: {"field1": "value1", "field2": true, ...}

        Returns next step info or completion status.
        Repeat until the flow completes. Successful flows complete automatically
        when the final form step is submitted. Use ha_abort_config_entry_options_flow
        only if you need to cancel without saving.
        """
        try:
            # Parse data if string
            if isinstance(data, str):
                parsed_data = parse_json_param(data)
                if not isinstance(parsed_data, dict):
                    raise_tool_error(
                        create_validation_error(
                            "Data must be a dictionary/object", parameter="data"
                        )
                    )
                data_dict: dict[str, Any] = parsed_data
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
        except Exception as e:
            logger.error(f"Error submitting options flow step {flow_id}: {e}")
            return exception_to_structured_error(e, context={"flow_id": flow_id})

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["config"],
            "title": "Abort Options Flow",
        }
    )
    @log_tool_usage
    async def ha_abort_config_entry_options_flow(
        flow_id: Annotated[
            str,
            Field(description="Flow ID to abort"),
        ],
    ) -> dict[str, Any]:
        """Abort an in-progress options flow without saving changes.

        Successful flows complete automatically when the final form step is submitted
        via ha_submit_config_entry_options_step. Only call this tool if you need to
        cancel the flow without applying changes.
        """
        try:
            result = await client.abort_options_flow(flow_id)
            return {
                "success": True,
                "result": result,
            }
        except Exception as e:
            logger.error(f"Error aborting options flow {flow_id}: {e}")
            return exception_to_structured_error(e, context={"flow_id": flow_id})
