"""
Trace retrieval tools for debugging Home Assistant automations and scripts.

This module provides tools for retrieving execution traces from Home Assistant
to help debug automation and script issues.
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from .helpers import get_connected_ws_client, log_tool_usage

logger = logging.getLogger(__name__)


def register_trace_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant trace debugging tools."""

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_get_automation_traces(
        automation_id: Annotated[
            str,
            Field(
                description="Automation or script entity_id (e.g., 'automation.motion_light' or 'script.morning_routine')"
            ),
        ],
        run_id: Annotated[
            str | None,
            Field(
                description="Specific trace run_id to retrieve detailed trace. Omit to list recent traces.",
                default=None,
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                description="Maximum number of traces to return when listing (default: 10, max: 50)",
                default=10,
                ge=1,
                le=50,
            ),
        ] = 10,
    ) -> dict[str, Any]:
        """
        Retrieve execution traces for automations and scripts to debug issues.

        Traces show what happened during automation/script runs:
        - What triggered the automation
        - Which conditions passed or failed
        - What actions were executed
        - Any errors that occurred
        - Variable values during execution

        USAGE MODES:

        1. List recent traces (omit run_id):
           ha_get_automation_traces("automation.motion_light")
           Returns a summary of recent execution runs with timestamps, triggers, and status.

        2. Get detailed trace (provide run_id):
           ha_get_automation_traces("automation.motion_light", run_id="1705312800.123456")
           Returns full execution details including trigger info, condition results,
           action trace with timing, and context variables.

        DEBUGGING EXAMPLES:

        Automation not triggering:
        - Check if traces exist (automation may not be triggered)
        - Look at trigger info to see what event was received

        Automation runs but conditions fail:
        - Get detailed trace to see condition_results
        - Each condition shows whether it passed (true) or failed (false)

        Unexpected behavior in actions:
        - Get detailed trace to see action_trace
        - Shows each action step with result and any errors
        - For 'choose' actions, shows which branch was taken

        Template debugging:
        - Detailed trace shows evaluated template values in context
        - Trigger variables available under trigger_variables

        NOTES:
        - Traces are stored for a limited time by Home Assistant
        - Works for both automations and scripts (use full entity_id)
        - The 'state' field shows: 'stopped' (completed), 'running', or error state
        """
        try:
            # Determine domain from entity_id
            if automation_id.startswith("automation."):
                domain = "automation"
            elif automation_id.startswith("script."):
                domain = "script"
            else:
                return {
                    "success": False,
                    "error": f"Invalid entity_id format: {automation_id}",
                    "hint": "Entity ID must start with 'automation.' or 'script.'",
                }

            # Extract the object_id (part after the domain)
            object_id = automation_id.split(".", 1)[1]

            # Connect to WebSocket
            ws_client, error = await get_connected_ws_client(
                client.base_url, client.token
            )
            if error or ws_client is None:
                return error or {
                    "success": False,
                    "error": "Failed to connect to WebSocket",
                }

            try:
                if run_id:
                    # Get specific trace details
                    result = await ws_client.send_command(
                        "trace/get",
                        domain=domain,
                        item_id=object_id,
                        run_id=run_id,
                    )

                    if not result.get("success"):
                        return {
                            "success": False,
                            "automation_id": automation_id,
                            "run_id": run_id,
                            "error": result.get("error", "Failed to retrieve trace"),
                        }

                    trace_data = result.get("result", {})
                    return _format_detailed_trace(automation_id, run_id, trace_data)
                else:
                    # List recent traces
                    result = await ws_client.send_command(
                        "trace/list",
                        domain=domain,
                        item_id=object_id,
                    )

                    if not result.get("success"):
                        return {
                            "success": False,
                            "automation_id": automation_id,
                            "error": result.get("error", "Failed to list traces"),
                        }

                    traces_data = result.get("result", [])
                    return _format_trace_list(automation_id, traces_data, limit)

            finally:
                await ws_client.disconnect()

        except Exception as e:
            logger.error(f"Error getting traces for {automation_id}: {e}")
            return {
                "success": False,
                "automation_id": automation_id,
                "error": str(e),
                "suggestions": [
                    "Verify the automation/script entity_id exists",
                    "Check if traces are available (automation must have run recently)",
                    "Ensure Home Assistant connection is working",
                ],
            }


def _format_trace_list(
    automation_id: str, traces: list[dict[str, Any]], limit: int
) -> dict[str, Any]:
    """Format trace list for AI consumption."""
    formatted_traces = []

    for trace in traces[:limit]:
        # Extract key information from trace
        trace_info: dict[str, Any] = {
            "run_id": trace.get("run_id"),
            "timestamp": trace.get("timestamp"),
            "state": trace.get("state"),
        }

        # Extract trigger description if available
        trigger_str = trace.get("trigger")
        if trigger_str:
            trace_info["trigger"] = trigger_str

        # Check for errors
        error = trace.get("error")
        if error:
            trace_info["error"] = error

        # Add script-specific execution duration
        if "script_execution" in trace:
            trace_info["execution"] = trace.get("script_execution")

        formatted_traces.append(trace_info)

    return {
        "success": True,
        "automation_id": automation_id,
        "trace_count": len(formatted_traces),
        "total_available": len(traces),
        "traces": formatted_traces,
        "hint": "Use run_id with this tool to get detailed trace information",
    }


def _format_detailed_trace(
    automation_id: str, run_id: str, trace: dict[str, Any]
) -> dict[str, Any]:
    """Format detailed trace for AI consumption."""
    result: dict[str, Any] = {
        "success": True,
        "automation_id": automation_id,
        "run_id": run_id,
        "timestamp": trace.get("timestamp"),
        "state": trace.get("state"),
    }

    # Extract trigger information
    trigger_trace = trace.get("trace", {}).get("trigger", [])
    if trigger_trace:
        trigger_step = trigger_trace[0]
        trigger_vars = trigger_step.get("variables", {}).get("trigger", {})
        result["trigger"] = {
            "platform": trigger_vars.get("platform"),
            "description": trigger_vars.get("description"),
        }
        # Add state change details if present (use safe dictionary access)
        if "to_state" in trigger_vars:
            result["trigger"]["to_state"] = trigger_vars.get("to_state", {}).get("state")
        if "from_state" in trigger_vars:
            result["trigger"]["from_state"] = trigger_vars.get("from_state", {}).get("state")
        if "entity_id" in trigger_vars:
            result["trigger"]["entity_id"] = trigger_vars["entity_id"]

    # Extract condition results
    condition_trace = trace.get("trace", {}).get("condition", [])
    if condition_trace:
        condition_results = []
        for cond in condition_trace:
            cond_result = {
                "result": cond.get("result", {}).get("result"),
            }
            # Try to get condition type from the path
            if "path" in cond:
                cond_result["path"] = cond["path"]
            condition_results.append(cond_result)
        result["condition_results"] = condition_results

    # Extract action trace
    action_trace = trace.get("trace", {}).get("action", [])
    if action_trace:
        action_results = []
        for action in action_trace:
            action_info: dict[str, Any] = {
                "path": action.get("path"),
            }

            # Add timing information
            if "timestamp" in action:
                action_info["timestamp"] = action["timestamp"]

            # Extract action result
            action_result = action.get("result", {})
            if action_result:
                action_info["result"] = action_result

            # Check for errors
            if "error" in action:
                action_info["error"] = action["error"]

            # Extract variables if they contain useful debugging info
            variables = action.get("variables", {})
            if variables and "trigger" not in variables:  # Skip trigger vars (already shown)
                # Only include non-empty variable sets
                useful_vars = {k: v for k, v in variables.items() if v is not None}
                if useful_vars:
                    action_info["variables"] = useful_vars

            action_results.append(action_info)

        result["action_trace"] = action_results

    # Add context with trigger variables for template debugging
    config = trace.get("config", {})
    if config:
        # Include config summary for context
        result["config_summary"] = {
            "alias": config.get("alias"),
            "mode": config.get("mode", "single"),
        }

    # Check for overall error
    if trace.get("error"):
        result["error"] = trace["error"]

    # Add script execution info if present
    if trace.get("script_execution"):
        result["script_execution"] = trace["script_execution"]

    return result
