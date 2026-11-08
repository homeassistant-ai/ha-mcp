"""
Trace retrieval tools for debugging Home Assistant automations and scripts.

This module provides tools for retrieving execution traces from Home Assistant
to help debug automation and script issues.
"""

import json
import logging
from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    safe_progress,
    validate_identifier_not_empty,
)
from .tool_hints import read_only_hints
from .trace_formatting import _requested_sections, _trace_start
from .util_helpers import is_connection_error_message

logger = logging.getLogger(__name__)


class TraceTools:
    """Trace retrieval tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_get_automation_traces",
        tags={"History & Statistics"},
        annotations=read_only_hints("Get Automation Traces", open_world=False),
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
                description="Specific trace run_id to retrieve detailed trace.",
                default=None,
            ),
        ] = None,
        context_id: Annotated[
            str | None,
            Field(
                description=(
                    "The trace run's own context id, as returned in a detailed "
                    "trace's `context.id`. Mutually exclusive with run_id. "
                    "Parent/triggering contexts are not accepted."
                ),
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
                description="Deduplicate variables across action steps. Set to False to record the "
                "variables at every step; steps whose variables carry 'trigger', and "
                "null-valued entries, are omitted either way.",
                default=True,
            ),
        ] = True,
        detailed: Annotated[
            bool,
            Field(
                description="Include extra diagnostic data: logbook entries and context metadata.",
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
                description="Order traces are returned in. 'newest' returns most-recent first; "
                "'oldest' returns chronological-first.",
                default="newest",
            ),
        ] = "newest",
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Get execution traces for automations and scripts to debug issues.

        Traces show what triggered a run, which conditions passed or failed,
        which actions executed (for 'choose', which branch was taken), any
        errors, and variable values during execution. Home Assistant keeps
        only the most recent runs of each automation or script (its
        stored_traces setting), so older runs are not available. The 'state'
        field shows 'stopped' (completed), 'running', or an error state.

        USAGE MODES:
        1. List recent traces (omit run_id and context_id): returns a summary
           of recent runs with timestamps, triggers, and status. Use `offset`
           to page deeper when `has_more` is true.
           ha_get_automation_traces("automation.motion_light")
        2. Get a detailed trace (provide run_id): full execution details
           including trigger info, condition results, action trace with timing,
           and context variables.
           ha_get_automation_traces("automation.motion_light", run_id="1705312800.123456")
        3. Resolve a trace by its own context id (provide context_id): the same
           detail view, selected by the `context.id` from a detailed trace.
           Parent/triggering contexts are not accepted.
           ha_get_automation_traces("automation.motion_light", context_id="01J...")
        4. Add logbook entries and context metadata: detailed=True. Script-style
           action paths (sequence/, numeric) are always matched regardless of
           this flag.
        5. Variables at every step (trigger sets and null entries are still
           omitted): deduplicate=False.
        """
        try:
            if run_id is not None and context_id is not None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "run_id and context_id are mutually exclusive",
                        context={"run_id": run_id, "context_id": context_id},
                        suggestions=[
                            "Pass run_id to fetch a specific stored trace",
                            "Pass context_id to resolve the trace for a context",
                        ],
                    )
                )
            if context_id is not None:
                context_id = validate_identifier_not_empty(
                    context_id,
                    "context_id",
                    suggestions=[
                        "Pass the trace's own `context.id` from a detailed trace",
                        "Omit context_id to list recent traces",
                    ],
                    context={"automation_id": automation_id},
                )

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

            logger.debug(
                f"ha_get_automation_traces starting: id={automation_id} "
                f"run_id={run_id or '<list>'} context_id={context_id or '<none>'}",
            )
            await safe_progress(
                ctx,
                progress=0,
                total=3,
                message="resolving trace target",
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

            resolved_run_id = run_id
            if context_id is not None:
                resolved_run_id = await _resolve_run_id_for_context(
                    self._client, domain, item_id, context_id
                )

            await safe_progress(
                ctx,
                progress=1,
                total=3,
                message=(f"fetching trace {'detail' if resolved_run_id else 'list'}"),
            )

            if resolved_run_id:
                detail = await self._fetch_trace_detail(
                    domain,
                    item_id,
                    automation_id,
                    resolved_run_id,
                    deduplicate=deduplicate,
                    detailed=detailed,
                    sections=sections,
                    ctx=ctx,
                )
                if context_id is not None:
                    detail["context_id"] = context_id
                return detail
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
        except Exception as e:  # noqa: BLE001
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
            diagnostics = await _gather_diagnostics(self._client, automation_id, domain)
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

    Since #1947 a dead transport raises out of ``send_websocket_message``
    rather than arriving here, so this path sees the failures Home Assistant
    answered with. Connection-shaped wording can still reach it (an error frame
    or a message threaded through a response body), so it stays classified as
    CONNECTION_FAILED rather than a generic SERVICE_CALL_FAILED. A genuine
    trace-command failure (unknown item, no such run) keeps its
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

    except Exception as e:  # noqa: BLE001
        # On any error, fall back to object_id
        logger.warning(
            f"Failed to resolve unique_id for {entity_id}: {e}, "
            f"using object_id: {fallback_object_id}"
        )
        return fallback_object_id


async def _resolve_run_id_for_context(
    client: Any,
    domain: str,
    item_id: str,
    context_id: str,
) -> str:
    """Resolve a Core context id to the run_id of the trace that owns it."""
    result = await client.send_websocket_message(
        {"type": "trace/contexts", "domain": domain, "item_id": item_id}
    )
    if not result.get("success"):
        _raise_trace_ws_failure(
            result.get("error", "Failed to resolve trace context"),
            {"context_id": context_id, "domain": domain, "item_id": item_id},
        )

    contexts = result.get("result")
    entry = contexts.get(context_id) if isinstance(contexts, dict) else None
    run_id = entry.get("run_id") if isinstance(entry, dict) else None
    if not run_id:
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"No trace found for context: {context_id}",
                context={
                    "context_id": context_id,
                    "domain": domain,
                    "item_id": item_id,
                },
                suggestions=[
                    "Pass the `context.id` from a detailed trace of this "
                    "automation or script",
                    "Use ha_get_automation_traces(automation_id=...) "
                    "without context_id to list recent traces",
                    "Parent/triggering contexts identify the caller, not the "
                    "trace run; use the trace's own context.id",
                ],
            )
        )
    return str(run_id)


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

            # Automations are disabled when their state is off. A script's
            # off state means idle, not disabled; only an unavailable script
            # is treated as unable to run.
            state = entity_state.get("state", "unknown")
            diagnostics["automation_enabled"] = (
                state != "unavailable" if domain == "script" else state == "on"
            )

            # Get last_triggered from attributes
            attributes = entity_state.get("attributes", {})
            last_triggered = attributes.get("last_triggered")
            if last_triggered:
                diagnostics["last_triggered"] = last_triggered

            # Both automations and scripts expose the same trace config shape.
            diagnostics["trace_storage_enabled"] = await _is_trace_storage_enabled(
                client, automation_id, domain
            )

            diagnostics["suggestion"] = _diagnostic_suggestion(diagnostics, domain)

    except Exception as e:  # noqa: BLE001
        # Entity doesn't exist or error occurred
        logger.debug(f"Error getting entity state for diagnostics: {e}")
        diagnostics["suggestion"] = (
            f"Could not find {automation_id}. "
            "Verify the entity_id is correct using ha_search()."
        )

    return diagnostics


async def _is_trace_storage_enabled(
    client: Any,
    entity_id: str,
    domain: str,
) -> bool:
    """Return whether stored_traces is enabled for an automation or script."""
    try:
        config_result = await client.send_websocket_message(
            {"type": f"{domain}/config", "entity_id": entity_id}
        )
        if config_result.get("success"):
            # Core returns {"result": {"config": raw_config}} for both
            # automation/config and script/config; the stored_traces setting
            # lives under raw_config["trace"].
            result = config_result.get("result")
            raw_config = result.get("config") if isinstance(result, dict) else None
            trace_config = (
                raw_config.get("trace") if isinstance(raw_config, dict) else None
            )
            stored_traces = (
                trace_config.get("stored_traces")
                if isinstance(trace_config, dict)
                else None
            )
            if stored_traces is not None:
                try:
                    stored_count = int(stored_traces)
                except (TypeError, ValueError):
                    stored_count = None
                if stored_count is not None and stored_count <= 0:
                    return False
    except Exception as e:  # noqa: BLE001
        logger.debug(f"Could not get {domain} config: {e}")
    return True


def _diagnostic_suggestion(diagnostics: dict[str, Any], domain: str) -> str:
    """Build a helpful hint explaining why no traces are available."""
    if not diagnostics["automation_enabled"]:
        return (
            f"The {domain} is currently disabled (state: off). "
            "Enable it to start recording traces."
        )
    if not diagnostics["trace_storage_enabled"]:
        return (
            f"Trace storage is disabled for this {domain}. "
            f"Set 'stored_traces' to a positive number in the {domain} config."
        )
    if diagnostics["last_triggered"] is None:
        return (
            f"The {domain} has never been triggered. "
            "Wait for it to trigger or manually trigger it to generate traces."
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
        traces: List of trace data from Home Assistant (sorted here by Core's
            start timestamp before paging)
        limit: Maximum number of traces to include
        diagnostics: Optional diagnostic information when traces are empty
        offset: Number of traces to skip from the start of the requested order
        order: 'newest' (default) returns most-recent first; 'oldest' chronological
    """
    # HA's trace/list returns runs first, then not-triggered traces, so list
    # position is not chronological. Sort by Core's own start timestamp before
    # picking the newest or oldest window. Drop malformed rows before sorting
    # so one bad element cannot take down the whole page.
    traces = [trace for trace in traces if isinstance(trace, dict)]
    ordered = sorted(traces, key=_trace_start)
    if order == "newest":
        end = len(ordered) - offset
        start = max(end - limit, 0)
        window = list(reversed(ordered[start:end])) if end > 0 else []
    else:
        window = ordered[offset : offset + limit]

    formatted_traces = []
    for trace in window:
        # Pass Core's short dict through instead of projecting a field
        # allowlist; keep the concise execution alias for existing callers.
        trace_info = dict(trace)
        if "script_execution" in trace_info:
            trace_info["execution"] = trace_info["script_execution"]
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


def _add_core_summary_fields(result: dict[str, Any], trace: dict[str, Any]) -> None:
    """Copy Core's extended-dict summary fields into the tool result."""
    for key in ("last_step", "blueprint_inputs", "domain", "item_id"):
        if key in trace:
            result[key] = trace[key]
    if trace.get("not_triggered"):
        result["not_triggered"] = True


def _add_optional_detail(
    result: dict[str, Any],
    trace: dict[str, Any],
    *,
    detailed: bool,
    requested: set[str],
) -> None:
    """Add logbook entries and context when detailed or explicitly requested."""
    if (detailed or "logbook" in requested) and "logbook_entries" in trace:
        result["logbook_entries"] = trace["logbook_entries"]
    if (detailed or "context" in requested) and trace.get("context"):
        result["context"] = trace["context"]


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

    raw_trace = trace.get("trace") or {}
    triggers, conditions, actions = _classify_trace_steps(raw_trace, domain)

    _populate_trigger_info(result, triggers, trace)
    _populate_condition_results(result, conditions)
    _populate_action_trace(result, actions, deduplicate)

    # Pass Core's extended-dict summary fields through. They describe the run
    # independently of the action trace and were previously dropped.
    _add_core_summary_fields(result, trace)

    # Add the run's raw config from Core. It is historical (the config used by
    # this run), not the automation's current config, and it is the native
    # field the trace editor shows when debugging.
    config = trace.get("config")
    if config is not None:
        result["config"] = config

    # Check for overall error
    if trace.get("error"):
        result["error"] = trace["error"]

    # Add script execution info if present
    if trace.get("script_execution"):
        result["script_execution"] = trace["script_execution"]

    requested = _requested_sections(sections)

    # In detailed mode, include logbook entries that Home Assistant supplies
    # and the run's context metadata. Either can be requested explicitly
    # through `sections` without turning on detailed mode.
    _add_optional_detail(result, trace, detailed=detailed, requested=requested)

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
    if domain == "script" and path.split("/", maxsplit=1)[0].isdigit():
        return "action"
    return None


def _filter_trace_sections(result: dict[str, Any], sections: str) -> dict[str, Any]:
    """Filter a formatted trace down to the requested sections plus metadata."""
    # Maps user-facing section names to result dict keys.
    section_key_map = {
        "trigger": "trigger",
        "conditions": "condition_results",
        "actions": "action_trace",
        "config": "config",
        "error": "error",
        "logbook": "logbook_entries",
        "context": "context",
    }
    requested = _requested_sections(sections)
    keep_keys = {section_key_map[s] for s in requested if s in section_key_map}
    # Always keep metadata keys
    keep_keys |= {
        "success",
        "automation_id",
        "run_id",
        "timestamp",
        "state",
        "script_execution",
        "last_step",
        "blueprint_inputs",
        "domain",
        "item_id",
        "not_triggered",
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
            result["trigger"]["to_state"] = (trigger_vars.get("to_state") or {}).get(
                "state"
            )
        if "from_state" in trigger_vars:
            result["trigger"]["from_state"] = (
                trigger_vars.get("from_state") or {}
            ).get("state")
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
