"""
Trace retrieval tools for debugging Home Assistant automations and scripts.

This module provides tools for retrieving execution traces from Home Assistant
to help debug automation and script issues.
"""

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
        detailed: Annotated[
            bool,
            Field(
                description="Include the run's logbook entries and context metadata.",
                default=False,
            ),
        ] = False,
        sections: Annotated[
            str | None,
            Field(
                description=(
                    "Comma-separated top-level keys from Core's extended trace "
                    "dict to return, plus 'logbook' for the run's logbook. "
                    "Valid keys depend on the run; examples: 'trace', 'config', "
                    "'context', 'error', 'logbook'. Unknown names are rejected. "
                    "Omit to return Core's full extended dict."
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
        which actions executed, any errors, and variable values during
        execution. Home Assistant keeps only the most recent runs of each
        automation or script (its stored_traces setting), so older runs are
        not available.

        USAGE MODES:
        1. List recent traces (omit run_id and context_id): returns Core's
           short dicts unchanged. Use `offset` to page deeper when `has_more`
           is true.
           ha_get_automation_traces("automation.motion_light")
        2. Get a detailed trace (provide run_id): returns Core's extended trace
           dict unchanged, including config, raw step trace, context, errors
           and variables.
           ha_get_automation_traces("automation.motion_light", run_id="1705312800.123456")
        3. Resolve a trace by context (provide context_id): the response
           reports `context_match` as `trace` or `parent`.
           ha_get_automation_traces("automation.motion_light", context_id="01J...")
        4. Add the run's logbook: detailed=True or sections="logbook".

        Do not use this tool for general log history or recorder statistics;
        use `ha_get_logs` for logs and `ha_get_history` for history/statistics.

        RESPONSE FIELDS:
        - A detail response is Core's extended `trace/get` dict: `config`,
          `trace`, `context`, `timestamp`, `state`, `error`,
          `script_execution`, `blueprint_inputs` and any run-specific fields.
        - `success`, `entity_id` and `run_id` identify the response.
        - `context_match` is `trace` or `parent`; `context_scanned`,
          `context_scan_truncated` and `context_scan_errors` describe the
          public fallback scan.
        - `logbook_entries`, `logbook_transport`, `logbook_total` and
          `logbook_truncated` describe the run's logbook rows.
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
                        + "event/service context id",
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
                    detailed=detailed,
                    sections=sections,
                    ctx=ctx,
                    fallback_trace=(
                        context_match.trace_data if context_match is not None else None
                    ),
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
        detailed: bool,
        sections: str | None,
        ctx: Context | None,
        fallback_trace: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retrieve and format a single trace by run_id.

        ``fallback_trace`` is the exact ``trace/get`` result the context
        resolution already read. Use it instead of issuing a second read.
        """
        if fallback_trace is not None:
            trace_data = fallback_trace
        else:
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

        requested = _requested_sections(sections)
        logbook: TraceLogbook | None = None
        # An explicit section list decides on its own: detailed=True must not
        # spend a slow recorder query on rows the caller then filters out.
        if "logbook" in requested or (detailed and not sections):
            logbook = await fetch_trace_logbook(self._client, trace_data)
        await safe_progress(ctx, progress=3, total=3, message="formatting trace")
        formatted = _format_detailed_trace(
            entity_id,
            run_id,
            trace_data,
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


_TRACE_SECTION_KEYS = frozenset(
    {
        "config",
        "trace",
        "blueprint_inputs",
        "context",
        "error",
        "state",
        "script_execution",
        "timestamp",
        "unique_id",
        "last_step",
        "domain",
        "item_id",
        "not_triggered",
        "logbook",
    }
)


def _format_detailed_trace(
    entity_id: str,
    run_id: str,
    trace: dict[str, Any],
    *,
    sections: str | None = None,
    logbook: TraceLogbook | None = None,
) -> dict[str, Any]:
    """Return Core's extended trace dict unchanged, with tool metadata."""
    result = dict(trace)
    result["success"] = True
    result["entity_id"] = entity_id
    result["run_id"] = run_id
    if logbook is not None:
        result["logbook_entries"] = logbook.entries
        if logbook.transport is not None:
            result["logbook_transport"] = logbook.transport
        result["logbook_total"] = logbook.total
        result["logbook_truncated"] = logbook.truncated
    if sections:
        result = _filter_trace_sections(result, sections)
    return result


def _filter_trace_sections(result: dict[str, Any], sections: str) -> dict[str, Any]:
    """Keep requested top-level Core keys; reject names Core does not define."""
    requested = _requested_sections(sections)
    known = _TRACE_SECTION_KEYS | set(result)
    unknown = sorted(requested - known)
    if unknown:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Unknown trace section(s): " + ", ".join(unknown),
                context={"sections": sections, "unknown": unknown},
                suggestions=[
                    "Use top-level Core extended-dict keys such as trace, config, context, error",
                    "Add 'logbook' to request the run's logbook",
                ],
            )
        )
    keep = requested | {"success", "entity_id", "run_id", "warnings"}
    if "logbook" in requested:
        keep |= {
            "logbook_entries",
            "logbook_transport",
            "logbook_total",
            "logbook_truncated",
        }
    return {key: value for key, value in result.items() if key in keep}
