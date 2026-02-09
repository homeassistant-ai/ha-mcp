"""
Config Entry Options Flow API tools for Home Assistant MCP server.

This module provides tools for configuring existing integrations via their
options flow - the settings UI you see when clicking "Configure" on an
integration in the Home Assistant UI.
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from .helpers import exception_to_structured_error, log_tool_usage
from .util_helpers import parse_json_param

logger = logging.getLogger(__name__)


def register_config_entry_options_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Config Entry Options Flow API tools with the MCP server."""

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "tags": ["config"],
            "title": "List Config Entries",
        }
    )
    @log_tool_usage
    async def ha_config_entry_list() -> dict[str, Any]:
        """List all config entries (integrations).

        Returns entry_id, domain, title, and state for each integration.
        Use entry_id with ha_config_entry_options_start to configure an integration.
        """
        try:
            entries = await client.get_config_entries()
            return {
                "success": True,
                "count": len(entries),
                "entries": [
                    {
                        "entry_id": e.get("entry_id"),
                        "domain": e.get("domain"),
                        "title": e.get("title"),
                        "state": e.get("state"),
                        "supports_options": e.get("supports_options", False),
                    }
                    for e in entries
                ],
            }
        except Exception as e:
            logger.error(f"Error listing config entries: {e}")
            return exception_to_structured_error(e)

    @mcp.tool(
        annotations={
            "destructiveHint": False,
            "tags": ["config"],
            "title": "Start Options Flow",
        }
    )
    @log_tool_usage
    async def ha_config_entry_options_start(
        entry_id: Annotated[
            str,
            Field(description="Config entry ID (from ha_config_entry_list)"),
        ],
    ) -> dict[str, Any]:
        """Start options flow for a config entry.

        Returns flow_id and first step info (menu_options or data_schema).
        Use the flow_id with ha_config_entry_options_submit to navigate the flow.

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
    async def ha_config_entry_options_submit(
        flow_id: Annotated[
            str,
            Field(description="Flow ID from ha_config_entry_options_start"),
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
        Repeat until you're back at the main menu, then use ha_config_entry_options_finish.
        """
        try:
            # Parse data if string
            if isinstance(data, str):
                parsed_data = parse_json_param(data)
                if not isinstance(parsed_data, dict):
                    return {
                        "success": False,
                        "error": "Data must be a dictionary/object",
                    }
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
            "title": "Finish Options Flow",
        }
    )
    @log_tool_usage
    async def ha_config_entry_options_finish(
        flow_id: Annotated[
            str,
            Field(description="Flow ID to complete"),
        ],
    ) -> dict[str, Any]:
        """Complete or abort an options flow.

        Call this when you're done configuring and back at the main menu.
        """
        try:
            result = await client.finish_options_flow(flow_id)
            return {
                "success": True,
                "result": result,
            }
        except Exception as e:
            logger.error(f"Error finishing options flow {flow_id}: {e}")
            return exception_to_structured_error(e, context={"flow_id": flow_id})
