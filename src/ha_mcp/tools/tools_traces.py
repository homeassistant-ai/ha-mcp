"""
Trace retrieval tools for debugging Home Assistant automations and scripts.

This module provides tools for retrieving execution traces from Home Assistant
to help debug automation and script issues.
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from ..client.websocket_client import HomeAssistantWebSocketClient
from .helpers import get_connected_ws_client, log_tool_usage

logger = logging.getLogger(__name__)


def register_trace_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant trace debugging tools."""

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["trace"], "title": "Get Automation Traces"})
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

            # Extract the object_id (part after the domain) as fallback
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
                # Home Assistant stores traces by unique_id, not entity_id.
                # We need to resolve entity_id -> unique_id via entity registry.
                item_id = await _resolve_trace_item_id(
                    ws_client, automation_id, object_id
                )

                if run_id:
                    # Get specific trace details
                    result = await ws_client.send_command(
                        "trace/get",
                        domain=domain,
                        item_id=item_id,
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
                        item_id=item_id,
                    )

                    if not result.get("success"):
                        return {
                            "success": False,
                            "automation_id": automation_id,
                            "error": result.get("error", "Failed to list traces"),
                        }

                    traces_data = result.get("result", [])

                    # If traces are empty, gather diagnostic information
                    if not traces_data:
                        diagnostics = await _gather_diagnostics(
                            ws_client, client, automation_id, domain
                        )
                        return _format_trace_list(
                            automation_id, traces_data, limit, diagnostics
                        )

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


async def _resolve_trace_item_id(
    ws_client: Any, entity_id: str, fallback_object_id: str
) -> str:
    """
    Resolve entity_id to the unique_id used for trace storage.

    Home Assistant stores traces using the automation/script's unique_id,
    not the entity_id. This function looks up the unique_id from the
    entity registry and falls back to object_id if not found.

    Args:
        ws_client: Connected WebSocket client
        entity_id: Full entity_id (e.g., 'automation.morning_routine')
        fallback_object_id: Object ID to use if unique_id lookup fails

    Returns:
        The unique_id for trace lookup, or fallback_object_id
    """
    try:
        # Query entity registry to get unique_id
        result = await ws_client.send_command(
            "config/entity_registry/get",
            entity_id=entity_id,
        )

        if result.get("success") and result.get("result"):
            unique_id = result["result"].get("unique_id")
            if unique_id:
                logger.debug(
                    f"Resolved {entity_id} to unique_id: {unique_id}"
                )
                return unique_id

        # Fallback to object_id if no unique_id found
        logger.debug(
            f"No unique_id found for {entity_id}, using object_id: {fallback_object_id}"
        )
        return fallback_object_id

    except Exception as e:
        # On any error, fall back to object_id
        logger.warning(
            f"Failed to resolve unique_id for {entity_id}: {e}, "
            f"using object_id: {fallback_object_id}"
        )
        return fallback_object_id


async def _gather_diagnostics(
    ws_client: HomeAssistantWebSocketClient,
    client: Any,
    automation_id: str,
    domain: str,
) -> dict[str, Any]:
    """
    Gather diagnostic information when traces are empty.

    This helps users understand why there are no traces available for
    an automation or script.

    Args:
        ws_client: Connected WebSocket client
        client: REST API client
        automation_id: Full entity_id (e.g., 'automation.motion_light')
        domain: Either 'automation' or 'script'

    Returns:
        Dictionary containing diagnostic information:
        - automation_exists: Whether the entity exists
        - automation_enabled: Whether the automation is enabled (on/off state)
        - trace_storage_enabled: Whether trace storage is enabled for this item
        - last_triggered: Last trigger timestamp if available
        - suggestion: Helpful hint based on the diagnostics
    """
    diagnostics: dict[str, Any] = {
        "automation_exists": False,
        "automation_enabled": False,
        "trace_storage_enabled": True,  # Default assumption
        "last_triggered": None,
        "suggestion": "",
    }

    try:
        # Get entity state to check existence and enabled status
        entity_state = await client.get_entity_state(automation_id)

        if entity_state:
            diagnostics["automation_exists"] = True

            # Check if enabled (state is 'on' for automations, 'off' is disabled)
            state = entity_state.get("state", "unknown")
            diagnostics["automation_enabled"] = state == "on"

            # Get last_triggered from attributes
            attributes = entity_state.get("attributes", {})
            last_triggered = attributes.get("last_triggered")
            if last_triggered:
                diagnostics["last_triggered"] = last_triggered

            # Check if tracing is stored - only for automations
            # (scripts always store traces when enabled)
            if domain == "automation":
                # Try to get automation config to check stored_traces setting
                try:
                    unique_id = attributes.get("id")
                    if unique_id:
                        config_result = await ws_client.send_command(
                            "automation/config",
                            entity_id=automation_id,
                        )
                        if config_result.get("success"):
                            config = config_result.get("result", {})
                            # stored_traces defaults to True if not specified
                            stored_traces = config.get("stored_traces")
                            if stored_traces is not None and stored_traces <= 0:
                                diagnostics["trace_storage_enabled"] = False
                except Exception as e:
                    logger.debug(f"Could not get automation config: {e}")

            # Generate suggestion based on diagnostics
            suggestions = []

            if not diagnostics["automation_enabled"]:
                suggestions.append(
                    f"The {domain} is currently disabled (state: off). "
                    "Enable it to start recording traces."
                )
            elif diagnostics["last_triggered"] is None:
                suggestions.append(
                    f"The {domain} has never been triggered. "
                    "Wait for it to trigger or manually trigger it to generate traces."
                )
            elif not diagnostics["trace_storage_enabled"]:
                suggestions.append(
                    "Trace storage is disabled for this automation. "
                    "Set 'stored_traces' to a positive number in the automation config."
                )
            else:
                suggestions.append(
                    "Traces may have been cleared or expired. "
                    "Home Assistant only keeps a limited number of recent traces."
                )

            diagnostics["suggestion"] = " ".join(suggestions)

    except Exception as e:
        # Entity doesn't exist or error occurred
        logger.debug(f"Error getting entity state for diagnostics: {e}")
        diagnostics["suggestion"] = (
            f"Could not find {automation_id}. "
            "Verify the entity_id is correct using ha_search_entities()."
        )

    return diagnostics


def _format_trace_list(
    automation_id: str,
    traces: list[dict[str, Any]],
    limit: int,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Format trace list for AI consumption.

    Args:
        automation_id: The automation or script entity_id
        traces: List of trace data from Home Assistant
        limit: Maximum number of traces to include
        diagnostics: Optional diagnostic information when traces are empty
    """
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

    result: dict[str, Any] = {
        "success": True,
        "automation_id": automation_id,
        "trace_count": len(formatted_traces),
        "total_available": len(traces),
        "traces": formatted_traces,
        "hint": "Use run_id with this tool to get detailed trace information",
    }

    # Include diagnostics when traces are empty
    if diagnostics is not None and len(traces) == 0:
        result["diagnostics"] = diagnostics

    return result


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

    raw_trace = trace.get("trace", {})

    # Initialize lists
    triggers = []
    conditions = []
    actions = []

    # Home Assistant trace data is stored as a flat dict with path keys
    # e.g. "trigger/0": [...], "action/0": [...], "action/0/1": [...]
    for path, steps in raw_trace.items():
        if not isinstance(steps, list):
            continue

        for step in steps:
            # Create a copy to avoid modifying original
            step_info = step.copy()
            step_info["path"] = path

            if path == "trigger" or path.startswith("trigger/"):
                triggers.append(step_info)
            elif path == "condition" or path.startswith("condition/"):
                conditions.append(step_info)
            elif path == "action" or path.startswith("action/"):
                actions.append(step_info)

    # Sort by timestamp (if available) or path to maintain execution order
    def sort_key(item):
        return (item.get("timestamp", ""), item.get("path", ""))

    triggers.sort(key=sort_key)
    conditions.sort(key=sort_key)
    actions.sort(key=sort_key)

    # Extract trigger information
    if triggers:
        trigger_step = triggers[0]
        trigger_vars = trigger_step.get("changed_variables", {}).get("trigger", {})
        # Sometimes variables are in 'variables' key, sometimes 'changed_variables'
        if not trigger_vars:
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

    # If no trigger info found in traces, try to get it from the top-level trigger field if present
    # (some HA versions might populate this)
    if "trigger" not in result and "trigger" in trace:
        result["trigger"] = {"description": trace["trigger"]}

    # Extract condition results
    if conditions:
        condition_results = []
        for cond in conditions:
            cond_result = {
                "result": cond.get("result", {}).get("result"),
                "path": cond.get("path"),
            }
            if "timestamp" in cond:
                cond_result["timestamp"] = cond["timestamp"]
            condition_results.append(cond_result)
        result["condition_results"] = condition_results

    # Extract action trace
    if actions:
        action_results = []
        for action in actions:
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
            # Check both 'variables' and 'changed_variables'
            variables = action.get("variables") or action.get("changed_variables", {})
            if variables and "trigger" not in variables:  # Skip trigger vars (already shown)
                # Only include non-empty variable sets
                useful_vars = {k: v for k, v in variables.items() if v is not None}
                if useful_vars:
                    action_info["variables"] = useful_vars

            # Add child execution info (for nested scripts/automations)
            if "child_id" in action:
                action_info["child_id"] = action["child_id"]

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
