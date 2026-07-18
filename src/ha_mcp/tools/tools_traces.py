"""
Trace retrieval tools for debugging Home Assistant automations and scripts.

This module provides tools for retrieving execution traces from Home Assistant
to help debug automation and script issues.
"""

import json
import logging
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    safe_info,
    safe_progress,
)
from .util_helpers import is_connection_error_message

logger = logging.getLogger(__name__)


class TraceTools:
    """Trace retrieval tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_get_automation_traces",
        tags={"History & Statistics"},
        annotations={
            "openWorldHint": False,
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Automation Traces",
        },
    )
    @log_tool_usage
    async def ha_get_automation_traces(
        self,
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
                description="Maximum number of traces to return when listing (default: 10, max: 50).",
                default=10,
                ge=1,
                le=50,
            ),
        ] = 10,
        deduplicate: Annotated[
            bool,
            Field(
                description="Deduplicate variables across action steps (default: True). Set to False to include full variables at every step.",
                default=True,
            ),
        ] = True,
        detailed: Annotated[
            bool,
            Field(
                description="Include extra diagnostic data: logbook entries and context metadata (default: False). Use when standard trace lacks detail for debugging.",
                default=False,
            ),
        ] = False,
        sections: Annotated[
            str | None,
            Field(
                description=(
                    "Comma-separated list of trace sections to return. "
                    "Valid values: trigger, conditions, actions, config, error, logbook, context. "
                    "Omit to return all sections. Example: 'actions' or 'trigger,conditions'."
                ),
                default=None,
            ),
        ] = None,
        offset: Annotated[
            int,
            Field(
                description="Number of traces to skip from the start of the requested order. Use with `limit` to page through stored traces when `total_available > limit`.",
                default=0,
                ge=0,
            ),
        ] = 0,
        order: Annotated[
            Literal["newest", "oldest"],
            Field(
                description="Order traces are returned in. 'newest' (default) returns most-recent first; 'oldest' returns chronological-first.",
                default="newest",
            ),
        ] = "newest",
        ctx: Context | None = None,
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
           Use `offset` to page deeper when `has_more` is true, or `order="oldest"` to
           start from the earliest stored trace instead of the most recent.

        2. Get detailed trace (provide run_id):
           ha_get_automation_traces("automation.motion_light", run_id="1705312800.123456")
           Returns full execution details including trigger info, condition results,
           action trace with timing, and context variables.

        3. Get detailed trace with logbook (provide run_id and detailed=True):
           ha_get_automation_traces("automation.motion_light", run_id="1705312800.123456", detailed=True)
           Returns the formatted trace plus logbook entries and context metadata.
           Useful when the standard trace summary doesn't reveal enough for debugging.
           Note: script-style action paths (sequence/, numeric) are always matched
           regardless of this flag.

        4. Get full variables without deduplication (provide run_id and deduplicate=False):
           ha_get_automation_traces("automation.motion_light", run_id="1705312800.123456", deduplicate=False)
           Returns the formatted trace with full variables at every action step.

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
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid entity_id format: {automation_id}",
                        details="Entity ID must start with 'automation.' or 'script.'",
                        context={"automation_id": automation_id},
                    )
                )

            # Extract the object_id (part after the domain) as fallback
            object_id = automation_id.split(".", 1)[1]

            await safe_info(
                ctx,
                f"ha_get_automation_traces starting: id={automation_id} "
                f"run_id={run_id or '<list>'}",
            )
            await safe_progress(
                ctx,
                progress=0,
                total=3,
                message="connecting to Home Assistant WebSocket",
            )

            # Route through the shared pooled WebSocket (issue #1813) instead of
            # a dedicated connect/auth/disconnect handshake per call. Every trace
            # command below is a single request/response — no subscription or
            # streaming needs a dedicated socket — so the pooled client owns the
            # connection lifecycle. A connection-shaped failure on the fetch
            # surfaces as CONNECTION_FAILED, the same structured error the old
            # up-front connect check raised.

            # Home Assistant stores traces by unique_id, not entity_id.
            # We need to resolve entity_id -> unique_id via entity registry.
            item_id = await _resolve_trace_item_id(
                self._client, automation_id, object_id
            )

            await safe_progress(
                ctx,
                progress=1,
                total=3,
                message=f"fetching trace {'detail' if run_id else 'list'}",
            )

            if run_id:
                return await self._fetch_trace_detail(
                    domain,
                    item_id,
                    automation_id,
                    run_id,
                    deduplicate=deduplicate,
                    detailed=detailed,
                    sections=sections,
                    ctx=ctx,
                )
            return await self._fetch_trace_list(
                domain,
                item_id,
                automation_id,
                limit=limit,
                offset=offset,
                order=order,
                ctx=ctx,
            )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting traces for {automation_id}: {e}")
            exception_to_structured_error(
                e,
                context={"automation_id": automation_id},
                suggestions=[
                    "Verify the automation/script entity_id exists",
                    "Check if traces are available (automation must have run recently)",
                    "Ensure Home Assistant connection is working",
                ],
            )
            return (
                None  # exception_to_structured_error always raises; explicit for CodeQL
            )

    async def _fetch_trace_detail(
        self,
        domain: str,
        item_id: str,
        automation_id: str,
        run_id: str,
        *,
        deduplicate: bool,
        detailed: bool,
        sections: str | None,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Retrieve and format a single trace by run_id."""
        result = await self._client.send_websocket_message(
            {
                "type": "trace/get",
                "domain": domain,
                "item_id": item_id,
                "run_id": run_id,
            }
        )

        if not result.get("success"):
            _raise_trace_ws_failure(
                result.get("error", "Failed to retrieve trace"),
                {"automation_id": automation_id, "run_id": run_id},
            )

        trace_data = result.get("result", {})
        await safe_progress(ctx, progress=3, total=3, message="formatting trace")
        return _format_detailed_trace(
            automation_id,
            run_id,
            trace_data,
            deduplicate=deduplicate,
            detailed=detailed,
            sections=sections,
        )

    async def _fetch_trace_list(
        self,
        domain: str,
        item_id: str,
        automation_id: str,
        *,
        limit: int,
        offset: int,
        order: Literal["newest", "oldest"],
        ctx: Context | None,
    ) -> dict[str, Any]:
        """List recent traces, attaching diagnostics when none are stored."""
        result = await self._client.send_websocket_message(
            {
                "type": "trace/list",
                "domain": domain,
                "item_id": item_id,
            }
        )

        if not result.get("success"):
            _raise_trace_ws_failure(
                result.get("error", "Failed to list traces"),
                {"automation_id": automation_id},
            )

        traces_data = result.get("result", [])

        if not traces_data:
            await safe_progress(
                ctx,
                progress=2,
                total=3,
                message="no traces; gathering diagnostics",
            )
            diagnostics = await _gather_diagnostics(
                self._client, automation_id, domain
            )
            await safe_progress(
                ctx, progress=3, total=3, message="diagnostics complete"
            )
            return _format_trace_list(
                automation_id,
                traces_data,
                limit,
                diagnostics,
                offset=offset,
                order=order,
            )

        await safe_progress(
            ctx,
            progress=3,
            total=3,
            message=f"listed {len(traces_data)} traces",
        )
        return _format_trace_list(
            automation_id,
            traces_data,
            limit,
            offset=offset,
            order=order,
        )


def register_trace_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant trace debugging tools."""
    register_tool_methods(mcp, TraceTools(client))


def _raise_trace_ws_failure(error_msg: Any, context: dict[str, Any]) -> None:
    """Raise the structured error for a failed trace WS command.

    The pooled ``send_websocket_message`` collapses transport failures into
    ``{"success": False, "error": ...}`` — classify connection-shaped errors as
    CONNECTION_FAILED (the same code the removed dedicated-socket connect check
    raised) instead of a generic SERVICE_CALL_FAILED during an HA restart. A
    genuine trace-command failure (unknown item, no such run) keeps its
    SERVICE_CALL_FAILED shape unchanged.
    """
    if is_connection_error_message(error_msg):
        raise_tool_error(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                str(error_msg),
                context=context,
                suggestions=[
                    "Home Assistant may be restarting or unreachable — retry shortly",
                    "Check the connection to Home Assistant",
                ],
            )
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            error_msg,
            context=context,
        )
    )


async def _resolve_trace_item_id(
    client: Any, entity_id: str, fallback_object_id: str
) -> str:
    """
    Resolve entity_id to the unique_id used for trace storage.

    Home Assistant stores traces using the automation/script's unique_id,
    not the entity_id. This function looks up the unique_id from the
    entity registry and falls back to object_id if not found.

    Args:
        client: Pooled REST client exposing ``send_websocket_message``
        entity_id: Full entity_id (e.g., 'automation.morning_routine')
        fallback_object_id: Object ID to use if unique_id lookup fails

    Returns:
        The unique_id for trace lookup, or fallback_object_id
    """
    try:
        # Query entity registry to get unique_id. Best-effort: the pooled client
        # collapses a failure into ``{"success": False, ...}`` (or, for a
        # programming bug, raises) — either way we fall back to object_id and let
        # the subsequent trace fetch surface the real error.
        result = await client.send_websocket_message(
            {"type": "config/entity_registry/get", "entity_id": entity_id}
        )

        if result.get("success") and result.get("result"):
            unique_id = result["result"].get("unique_id")
            if unique_id:
                logger.debug(f"Resolved {entity_id} to unique_id: {unique_id}")
                return str(unique_id)

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
    client: Any,
    automation_id: str,
    domain: str,
) -> dict[str, Any]:
    """
    Gather diagnostic information when traces are empty.

    This helps users understand why there are no traces available for
    an automation or script.

    Args:
        client: Pooled REST client (REST reads + ``send_websocket_message``)
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
                diagnostics["trace_storage_enabled"] = await _is_trace_storage_enabled(
                    client, automation_id, attributes
                )

            diagnostics["suggestion"] = _diagnostic_suggestion(diagnostics, domain)

    except Exception as e:
        # Entity doesn't exist or error occurred
        logger.debug(f"Error getting entity state for diagnostics: {e}")
        diagnostics["suggestion"] = (
            f"Could not find {automation_id}. "
            "Verify the entity_id is correct using ha_search()."
        )

    return diagnostics


async def _is_trace_storage_enabled(
    client: Any,
    automation_id: str,
    attributes: dict[str, Any],
) -> bool:
    """Return whether stored_traces is enabled for an automation (defaults True)."""
    try:
        unique_id = attributes.get("id")
        if unique_id:
            config_result = await client.send_websocket_message(
                {"type": "automation/config", "entity_id": automation_id}
            )
            if config_result.get("success"):
                config = config_result.get("result", {})
                # stored_traces defaults to True if not specified
                stored_traces = config.get("stored_traces")
                if stored_traces is not None and stored_traces <= 0:
                    return False
    except Exception as e:
        logger.debug(f"Could not get automation config: {e}")
    return True


def _diagnostic_suggestion(diagnostics: dict[str, Any], domain: str) -> str:
    """Build a helpful hint explaining why no traces are available."""
    if not diagnostics["automation_enabled"]:
        return (
            f"The {domain} is currently disabled (state: off). "
            "Enable it to start recording traces."
        )
    if diagnostics["last_triggered"] is None:
        return (
            f"The {domain} has never been triggered. "
            "Wait for it to trigger or manually trigger it to generate traces."
        )
    if not diagnostics["trace_storage_enabled"]:
        return (
            "Trace storage is disabled for this automation. "
            "Set 'stored_traces' to a positive number in the automation config."
        )
    return (
        "Traces may have been cleared or expired. "
        "Home Assistant only keeps a limited number of recent traces."
    )


def _format_trace_list(
    automation_id: str,
    traces: list[dict[str, Any]],
    limit: int,
    diagnostics: dict[str, Any] | None = None,
    *,
    offset: int = 0,
    order: Literal["newest", "oldest"] = "newest",
) -> dict[str, Any]:
    """Format trace list for AI consumption.

    Args:
        automation_id: The automation or script entity_id
        traces: List of trace data from Home Assistant (oldest-first)
        limit: Maximum number of traces to include
        diagnostics: Optional diagnostic information when traces are empty
        offset: Number of traces to skip from the start of the requested order
        order: 'newest' (default) returns most-recent first; 'oldest' chronological
    """
    # HA's trace/list returns traces oldest-first. Pick a window from the end
    # for newest-first, or from the start for oldest-first, with offset for
    # pagination through stored traces beyond `limit`.
    if order == "newest":
        end = len(traces) - offset
        start = max(end - limit, 0)
        window = list(reversed(traces[start:end])) if end > 0 else []
    else:
        window = traces[offset : offset + limit]

    formatted_traces = []
    for trace in window:
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
        "offset": offset,
        "order": order,
        "has_more": offset + len(formatted_traces) < len(traces),
        "traces": formatted_traces,
        "hint": "Use run_id with this tool to get detailed trace information",
    }

    # Include diagnostics when traces are empty
    if diagnostics is not None and len(traces) == 0:
        result["diagnostics"] = diagnostics

    return result


def _format_detailed_trace(
    automation_id: str,
    run_id: str,
    trace: dict[str, Any],
    *,
    deduplicate: bool = True,
    detailed: bool = False,
    sections: str | None = None,
) -> dict[str, Any]:
    """Format detailed trace for AI consumption."""
    domain = "automation" if automation_id.startswith("automation.") else "script"
    result: dict[str, Any] = {
        "success": True,
        "automation_id": automation_id,
        "run_id": run_id,
        "timestamp": trace.get("timestamp"),
        "state": trace.get("state"),
    }

    raw_trace = trace.get("trace", {})
    triggers, conditions, actions = _classify_trace_steps(raw_trace, domain)

    _populate_trigger_info(result, triggers, trace)
    _populate_condition_results(result, conditions)
    _populate_action_trace(result, actions, deduplicate)

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

    # In detailed mode, include logbook entries and context metadata
    # that are essential for debugging but omitted by default to save context.
    if detailed:
        if "logbook_entries" in trace:
            result["logbook_entries"] = trace["logbook_entries"]
        if trace.get("context"):
            result["context"] = trace["context"]

    if sections:
        result = _filter_trace_sections(result, sections)

    return result


def _classify_trace_steps(
    raw_trace: dict[str, Any], domain: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Group flat trace path entries into sorted trigger/condition/action lists."""
    buckets: dict[str, list[dict[str, Any]]] = {
        "trigger": [],
        "condition": [],
        "action": [],
    }

    # Home Assistant trace data is stored as a flat dict with path keys
    # e.g. "trigger/0": [...], "action/0": [...], "action/0/1": [...]
    for path, steps in raw_trace.items():
        if not isinstance(steps, list):
            continue

        for step in steps:
            category = _categorize_step_path(path, domain)
            if category is None:
                continue
            # Create a copy to avoid modifying original
            step_info = step.copy()
            step_info["path"] = path
            buckets[category].append(step_info)

    # Sort by timestamp (if available) or path to maintain execution order
    def sort_key(item: dict[str, Any]) -> tuple[str, str]:
        return (item.get("timestamp", ""), item.get("path", ""))

    for steps_list in buckets.values():
        steps_list.sort(key=sort_key)

    return buckets["trigger"], buckets["condition"], buckets["action"]


def _categorize_step_path(path: str, domain: str) -> str | None:
    """Map a trace path key to 'trigger', 'condition', 'action', or None."""
    if path == "trigger" or path.startswith("trigger/"):
        return "trigger"
    if path == "condition" or path.startswith("condition/"):
        return "condition"
    if path == "action" or path.startswith(("action/", "sequence/")):
        return "action"
    if domain == "script" and path.split("/")[0].isdigit():
        return "action"
    return None


def _filter_trace_sections(result: dict[str, Any], sections: str) -> dict[str, Any]:
    """Filter a formatted trace down to the requested sections plus metadata."""
    # Maps user-facing section names to result dict keys.
    section_key_map = {
        "trigger": "trigger",
        "conditions": "condition_results",
        "actions": "action_trace",
        "config": "config_summary",
        "error": "error",
        "logbook": "logbook_entries",
        "context": "context",
    }
    requested = {s.strip().lower() for s in sections.split(",")}
    keep_keys = {section_key_map[s] for s in requested if s in section_key_map}
    # Always keep metadata keys
    keep_keys |= {
        "success",
        "automation_id",
        "run_id",
        "timestamp",
        "state",
        "script_execution",
    }
    return {k: v for k, v in result.items() if k in keep_keys}


def _populate_trigger_info(
    result: dict[str, Any],
    triggers: list[dict[str, Any]],
    trace: dict[str, Any],
) -> None:
    """Extract trigger information and add to result dict."""
    if triggers:
        trigger_step = triggers[0]
        trigger_vars = trigger_step.get("changed_variables", {}).get("trigger", {})
        if not trigger_vars:
            trigger_vars = trigger_step.get("variables", {}).get("trigger", {})

        result["trigger"] = {
            "platform": trigger_vars.get("platform"),
            "description": trigger_vars.get("description"),
        }
        if "to_state" in trigger_vars:
            result["trigger"]["to_state"] = trigger_vars.get("to_state", {}).get(
                "state"
            )
        if "from_state" in trigger_vars:
            result["trigger"]["from_state"] = trigger_vars.get("from_state", {}).get(
                "state"
            )
        if "entity_id" in trigger_vars:
            result["trigger"]["entity_id"] = trigger_vars["entity_id"]
        if "error" in trigger_step:
            result["trigger"]["error"] = trigger_step["error"]

    if "trigger" not in result and "trigger" in trace:
        result["trigger"] = {"description": trace["trigger"]}


def _populate_condition_results(
    result: dict[str, Any],
    conditions: list[dict[str, Any]],
) -> None:
    """Extract condition results and add to result dict."""
    if conditions:
        condition_results = []
        for cond in conditions:
            cond_result = {
                "result": cond.get("result", {}).get("result"),
                "path": cond.get("path"),
            }
            if "timestamp" in cond:
                cond_result["timestamp"] = cond["timestamp"]
            # HA 2026.7+ always records template errors on the failing step
            # (core #172917) — dropping this field would hide the reason a
            # condition evaluated to None.
            if "error" in cond:
                cond_result["error"] = cond["error"]
            condition_results.append(cond_result)
        result["condition_results"] = condition_results


def _populate_action_trace(
    result: dict[str, Any],
    actions: list[dict[str, Any]],
    deduplicate: bool,
) -> None:
    """Extract action trace with optional variable deduplication."""
    if not actions:
        return

    action_results = []
    last_vars_fingerprint: str | None = None
    for action in actions:
        action_info: dict[str, Any] = {
            "path": action.get("path"),
        }

        if "timestamp" in action:
            action_info["timestamp"] = action["timestamp"]

        action_result = action.get("result", {})
        if action_result:
            action_info["result"] = action_result

        if "error" in action:
            action_info["error"] = action["error"]

        variables = action.get("variables") or action.get("changed_variables", {})
        useful_vars, last_vars_fingerprint = _select_action_variables(
            variables, deduplicate, last_vars_fingerprint
        )
        if useful_vars is not None:
            action_info["variables"] = useful_vars

        if "child_id" in action:
            action_info["child_id"] = action["child_id"]

        action_results.append(action_info)

    result["action_trace"] = action_results


def _select_action_variables(
    variables: dict[str, Any],
    deduplicate: bool,
    last_fingerprint: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Pick the variables to record for an action step, deduplicating if enabled.

    Returns the variables to include (or None to omit) alongside the fingerprint
    to carry into the next step.
    """
    if not variables or "trigger" in variables:
        return None, last_fingerprint

    useful_vars = {k: v for k, v in variables.items() if v is not None}
    if not useful_vars:
        return None, last_fingerprint

    if not deduplicate:
        return useful_vars, last_fingerprint

    try:
        fingerprint = json.dumps(useful_vars, sort_keys=True, default=str)
    except (TypeError, ValueError):
        fingerprint = str(useful_vars)

    if fingerprint == last_fingerprint:
        return None, last_fingerprint
    return useful_vars, fingerprint
