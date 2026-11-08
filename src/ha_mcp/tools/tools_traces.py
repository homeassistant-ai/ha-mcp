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
from .trace_diagnostics import _gather_diagnostics
from .trace_lookup import (
    ContextTraceMatch,
    TraceLogbook,
    _raise_trace_ws_failure,
    _requested_sections,
    _trace_start,
    fetch_trace_logbook,
    resolve_context_trace,
)

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
        entity_id: Annotated[
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
                    "Context id to resolve to its trace. Accepts the trace "
                    "run's own `context.id` or the context id of the event or "
                    "service call that triggered it. Mutually exclusive with "
                    "run_id."
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
                    "Valid values: trigger, conditions, actions, config, error, "
                    "logbook, context. 'config' returns the full historical "
                    "config; 'logbook' reads Core's logbook for the run. Omit "
                    "to return all default sections; the full config still "
                    "requires sections='config'. Example: 'actions' or "
                    "'trigger,conditions'."
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
        3. Resolve a trace by context (provide context_id): accepts the trace
           run's own `context.id` or the context id of the event or service
           call that triggered it. The response reports `context_match` as
           `trace` or `parent`.
           ha_get_automation_traces("automation.motion_light", context_id="01J...")
        4. Add logbook entries and context metadata: detailed=True. Script-style
           action paths (sequence/, numeric) are always matched regardless of
           this flag.
        5. Variables at every step (trigger sets and null entries are still
           omitted): deduplicate=False.

        RESPONSE FIELDS:
        - `traces` carries Core's short dict fields unchanged, plus the
          `execution` alias for `script_execution`.
        - `entity_id`, `run_id`, `timestamp`, `state`, `last_step`, `domain`,
          `item_id`, `not_triggered` identify the run. `timestamp` is Core's
          `{start, finish}` object, not a single string.
        - `context_match` is `trace` for the run's own context or `parent` for
          a triggering context; `context_scanned` and
          `context_scan_truncated` describe the bounded parent scan, and
          `context_scan_errors` lists stored traces it could not read.
        - `config_summary` is the compact alias/mode view. Pass
          `sections="config"` for the full historical `config`; it is not
          returned by default.
        - `logbook_entries`, `logbook_transport`, `logbook_total` and
          `logbook_truncated` describe the run's logbook rows.
          `logbook_transport` is `component`, `rest`, or `mixed`.
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
                        "Pass the trace's own `context.id` or the triggering "
                        "event/service context id",
                        "Omit context_id to list recent traces",
                    ],
                    context={"entity_id": entity_id},
                )

            # Determine domain from entity_id
            if entity_id.startswith("automation."):
                domain = "automation"
            elif entity_id.startswith("script."):
                domain = "script"
            else:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid entity_id format: {entity_id}",
                        details="Entity ID must start with 'automation.' or 'script.'",
                        context={"entity_id": entity_id},
                    )
                )

            # Extract the object_id (part after the domain) as fallback
            object_id = entity_id.split(".", 1)[1]

            logger.debug(
                f"ha_get_automation_traces starting: id={entity_id} "
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
            item_id = await _resolve_trace_item_id(self._client, entity_id, object_id)

            resolved_run_id = run_id
            context_match: ContextTraceMatch | None = None
            if context_id is not None:
                context_match = await resolve_context_trace(
                    self._client, domain, item_id, context_id
                )
                resolved_run_id = context_match.run_id

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
                    entity_id,
                    resolved_run_id,
                    deduplicate=deduplicate,
                    detailed=detailed,
                    sections=sections,
                    ctx=ctx,
                )
                if context_match is not None:
                    _add_context_match(detail, context_id, context_match)
                return detail
            return await self._fetch_trace_list(
                domain,
                item_id,
                entity_id,
                limit=limit,
                offset=offset,
                order=order,
                ctx=ctx,
            )

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error getting traces for {entity_id}: {e}")
            exception_to_structured_error(
                e,
                context={"entity_id": entity_id},
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
        entity_id: str,
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
                {"entity_id": entity_id, "run_id": run_id},
            )

        trace_data = result.get("result", {})
        logbook: TraceLogbook | None = None
        if detailed or "logbook" in _requested_sections(sections):
            logbook = await fetch_trace_logbook(self._client, trace_data)
        await safe_progress(ctx, progress=3, total=3, message="formatting trace")
        formatted = _format_detailed_trace(
            entity_id,
            run_id,
            trace_data,
            deduplicate=deduplicate,
            detailed=detailed,
            sections=sections,
            logbook=logbook,
        )
        if logbook is not None and logbook.warnings:
            formatted.setdefault("warnings", []).extend(logbook.warnings)
        return formatted

    async def _fetch_trace_list(
        self,
        domain: str,
        item_id: str,
        entity_id: str,
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
                {"entity_id": entity_id},
            )

        traces_data = result.get("result", [])

        if not traces_data:
            await safe_progress(
                ctx,
                progress=2,
                total=3,
                message="no traces; gathering diagnostics",
            )
            diagnostics = await _gather_diagnostics(self._client, entity_id, domain)
            await safe_progress(
                ctx, progress=3, total=3, message="diagnostics complete"
            )
            return _format_trace_list(
                entity_id,
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
            entity_id,
            traces_data,
            limit,
            offset=offset,
            order=order,
        )


def register_trace_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant trace debugging tools."""
    register_tool_methods(mcp, TraceTools(client))


def _add_context_match(
    detail: dict[str, Any],
    context_id: str | None,
    match: ContextTraceMatch,
) -> None:
    """Attach the context resolution metadata to a detailed trace."""
    detail["context_id"] = context_id
    detail["context_match"] = match.match
    detail["context_scanned"] = match.scanned
    if match.truncated:
        detail["context_scan_truncated"] = True
    if match.scan_errors:
        detail["context_scan_errors"] = list(match.scan_errors)


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


def _format_trace_list(
    entity_id: str,
    traces: list[dict[str, Any]],
    limit: int,
    diagnostics: dict[str, Any] | None = None,
    *,
    offset: int = 0,
    order: Literal["newest", "oldest"] = "newest",
) -> dict[str, Any]:
    """Format trace list for AI consumption.

    Args:
        entity_id: The automation or script entity_id
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
        "entity_id": entity_id,
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
    logbook: TraceLogbook | None = None,
) -> None:
    """Add logbook entries and context when detailed or explicitly requested."""
    if detailed or "logbook" in requested:
        entries = (
            logbook.entries if logbook is not None else trace.get("logbook_entries")
        )
        if entries is not None:
            result["logbook_entries"] = entries
        if logbook is not None and logbook.transport is not None:
            result["logbook_transport"] = logbook.transport
        if logbook is not None:
            result["logbook_total"] = logbook.total
            result["logbook_truncated"] = logbook.truncated
    if (detailed or "context" in requested) and trace.get("context"):
        result["context"] = trace["context"]


def _format_detailed_trace(
    entity_id: str,
    run_id: str,
    trace: dict[str, Any],
    *,
    deduplicate: bool = True,
    detailed: bool = False,
    sections: str | None = None,
    logbook: TraceLogbook | None = None,
) -> dict[str, Any]:
    """Format detailed trace for AI consumption."""
    domain = "automation" if entity_id.startswith("automation.") else "script"
    result: dict[str, Any] = {
        "success": True,
        "entity_id": entity_id,
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

    # Add a compact summary of the run's raw config by default. The full
    # historical config is opt-in through sections="config" because triggers,
    # conditions and actions can be large.
    requested = _requested_sections(sections)
    config = trace.get("config")
    if config is not None:
        result["config_summary"] = {
            "alias": config.get("alias"),
            "mode": config.get("mode"),
        }
        if "config" in requested:
            result["config"] = config

    # Check for overall error
    if trace.get("error"):
        result["error"] = trace["error"]

    # Add script execution info if present
    if trace.get("script_execution"):
        result["script_execution"] = trace["script_execution"]

    # In detailed mode, include logbook entries that Home Assistant supplies
    # and the run's context metadata. Either can be requested explicitly
    # through `sections` without turning on detailed mode.
    _add_optional_detail(
        result,
        trace,
        detailed=detailed,
        requested=requested,
        logbook=logbook,
    )

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
        "entity_id",
        "run_id",
        "timestamp",
        "state",
        "script_execution",
        "last_step",
        "blueprint_inputs",
        "domain",
        "item_id",
        "not_triggered",
        "logbook_transport",
        "logbook_total",
        "logbook_truncated",
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
