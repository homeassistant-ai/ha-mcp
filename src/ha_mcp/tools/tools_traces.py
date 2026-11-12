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

from ..client.rest_client import HomeAssistantError
from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    extract_tool_error_message,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    safe_progress,
    validate_identifier_not_empty,
)
from .tool_hints import read_only_hints
from .trace_diagnostics import _gather_diagnostics
from .trace_logbook import TraceLogbook, fetch_trace_logbook
from .trace_lookup import (
    ContextTraceMatch,
    _raise_trace_ws_failure,
    _requested_sections,
    resolve_context_trace,
    resolve_global_context,
)

logger = logging.getLogger(__name__)


def _validate_trace_request(
    entity_id: str | None,
    domain: Literal["automation", "script"] | None,
    item_id: str | None,
    context_id: str | None,
    run_id: str | None,
) -> None:
    """Validate the selector combination before any trace command."""
    if entity_id is not None:
        validate_identifier_not_empty(
            entity_id,
            "entity_id",
            suggestions=[
                "Pass an automation.* or script.* entity_id",
                "Omit entity_id with domain to list across a domain",
            ],
        )
    if item_id is not None:
        validate_identifier_not_empty(
            item_id,
            "item_id",
            suggestions=[
                "Use Core's item_id from a trace/list row or context resolution",
                "Pass an entity_id instead when you have one",
            ],
        )
        if entity_id is not None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "entity_id and item_id are mutually exclusive",
                    context={"entity_id": entity_id, "item_id": item_id},
                    suggestions=[
                        "Pass entity_id for a normal automation/script lookup",
                        "Pass domain and item_id for a Core trace key",
                    ],
                )
            )
        if domain is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "domain is required with item_id",
                    context={"item_id": item_id},
                    suggestions=[
                        "Pass domain='automation' or 'script' with item_id",
                    ],
                )
            )
    if context_id is not None:
        validate_identifier_not_empty(
            context_id,
            "context_id",
            suggestions=[
                "Pass the trace's own `context.id` or the triggering "
                + "event/service context id",
                "Omit context_id to list recent traces",
            ],
            context={"entity_id": entity_id},
        )
    if (
        run_id is not None
        and entity_id is None
        and not (domain is not None and item_id is not None)
    ):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "run_id needs entity_id, or domain with item_id",
                context={"run_id": run_id},
                suggestions=[
                    "Pass the automation/script entity_id with run_id",
                    "Pass domain and Core's item_id from a list row",
                    "Use context_id alone to resolve a run",
                ],
            )
        )
    if entity_id is None and context_id is None and domain is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Pass entity_id, domain, or context_id",
                suggestions=[
                    "Pass an entity_id for one automation/script",
                    "Pass domain='automation' or 'script' to list across a domain",
                    "Pass context_id to resolve a run without an entity_id",
                ],
            )
        )


def _entity_domain(entity_id: str) -> Literal["automation", "script"]:
    if entity_id.startswith("automation."):
        return "automation"
    if entity_id.startswith("script."):
        return "script"
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"Invalid entity_id format: {entity_id}",
            details="Entity ID must start with 'automation.' or 'script.'",
            context={"entity_id": entity_id},
        )
    )
    raise AssertionError("unreachable")


async def _resolve_trace_target(
    client: Any,
    entity_id: str | None,
    domain: Literal["automation", "script"] | None,
    item_id: str | None,
    context_id: str | None,
    run_id: str | None,
) -> tuple[str | None, str, str | None, str | None, ContextTraceMatch | None]:
    """Resolve selectors to entity/domain/item/run and optional context match."""
    _validate_trace_request(entity_id, domain, item_id, context_id, run_id)
    context_match: ContextTraceMatch | None = None
    resolved_run_id = run_id
    resolved_item_id: str | None = item_id
    domain_name: str = domain or "automation"
    if entity_id is not None:
        entity_domain = _entity_domain(entity_id)
        if domain is not None and domain != entity_domain:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "domain does not match entity_id",
                    context={"entity_id": entity_id, "domain": domain},
                )
            )
        domain_name = entity_domain
        object_id = entity_id.split(".", 1)[1]
        resolved_item_id = await _resolve_trace_item_id(client, entity_id, object_id)
        if context_id is not None:
            context_match = await resolve_context_trace(
                client, domain_name, resolved_item_id, context_id
            )
            resolved_run_id = context_match.run_id
    elif context_id is not None and resolved_item_id is not None:
        context_match = await resolve_context_trace(
            client, domain_name, resolved_item_id, context_id
        )
        resolved_run_id = context_match.run_id
    elif context_id is not None:
        context_match = await resolve_global_context(client, context_id, domain)
        domain_name = context_match.domain
        resolved_item_id = context_match.item_id
        resolved_run_id = context_match.run_id
    return entity_id, domain_name, resolved_item_id, resolved_run_id, context_match


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
            str | None,
            Field(
                description=(
                    "Automation or script entity_id. Omit when resolving a "
                    "context-only lookup."
                ),
                default=None,
            ),
        ] = None,
        domain: Annotated[
            Literal["automation", "script"] | None,
            Field(
                description=(
                    "List recent traces across every automation or script when "
                    "entity_id is omitted. Required with item_id."
                ),
                default=None,
            ),
        ] = None,
        item_id: Annotated[
            str | None,
            Field(
                description=(
                    "Core's trace item id from a list row or context "
                    "resolution. Use with domain and run_id when no entity_id "
                    "is available."
                ),
                default=None,
            ),
        ] = None,
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
                    "run's own `context.id` or, with the ha_mcp_tools "
                    "component, the context id of the event or service call "
                    "that triggered it. Mutually exclusive with run_id."
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
                description="Include the run's logbook entries in the detail response.",
                default=False,
            ),
        ] = False,
        sections: Annotated[
            str | None,
            Field(
                description=(
                    "Comma-separated top-level keys from Core's extended trace "
                    "dict to return, plus 'logbook' for the run's logbook. "
                    "Available keys depend on the run; examples: 'trace', "
                    "'config', 'context', 'error', 'logbook'. A requested name "
                    "this run lacks is reported in 'warnings'. Omit, or pass an "
                    "empty string, to return Core's full extended dict."
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

        Do not use this tool for general log history or recorder statistics;
        use `ha_get_logs` for logs and `ha_get_history` for history/statistics.

        USAGE MODES:
        1. List recent traces (omit run_id and context_id).
           ha_get_automation_traces("automation.motion_light")
           ha_get_automation_traces(domain="automation")
        2. Get a detailed trace (provide run_id): returns Core's extended trace
           dict unchanged, including config, raw step trace, context, errors
           and variables. Pass `sections` to keep only named top-level keys
           (plus 'logbook' for the run's logbook); a requested name the run
           lacks is reported in `warnings` and the run's own keys are listed.
           An empty `sections` string means omitted.
           ha_get_automation_traces("automation.motion_light", run_id="1705312800.123456")
           ha_get_automation_traces(domain="automation", item_id="uid", run_id="...")
        3. Resolve a trace by context (provide context_id): the response
           selects Core's run for that context and reports `context_match` as
           `trace` or `parent`. With the ha_mcp_tools component,
           `context_other_matches` lists the other runs that matched the
           context id (own or triggering parent); Core's own `trace/contexts`
           keeps one run per context id. A context Core does not hold is
           reported as not found with a hint that the component resolves
           triggering (parent) contexts.
           ha_get_automation_traces(context_id="01J...")

        RESPONSE FIELDS:
        - A detail response is Core's extended `trace/get` dict: `config`,
          `trace`, `context`, `timestamp`, `state`, `error`,
          `script_execution`, `blueprint_inputs` and any run-specific fields.
        - `success`, `run_id`, `domain` and `item_id` identify the run.
          `entity_id` is present when the caller supplied it; a context-only
          lookup returns Core's `domain`/`item_id` instead of inventing one.
        - `context_match` is `trace` or `parent`; `context_scanned` reports
          how many stored runs the ha_mcp_tools component inspected while
          resolving the context id, and is 0 when Core's own-context map
          answered.
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

            logger.debug(
                f"ha_get_automation_traces starting: id={entity_id or '<domain>'} "
                f"run_id={run_id or '<list>'} context_id={context_id or '<none>'}",
            )
            await safe_progress(
                ctx,
                progress=0,
                total=3,
                message="resolving trace target",
            )
            (
                entity_id,
                domain_name,
                item_id,
                resolved_run_id,
                context_match,
            ) = await _resolve_trace_target(
                self._client, entity_id, domain, item_id, context_id, run_id
            )
            if resolved_run_id is None and (_requested_sections(sections) or detailed):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "sections and detailed require a resolved run",
                        context={
                            "resolved_run_id": resolved_run_id,
                            "sections": sections,
                            "detailed": detailed,
                        },
                        suggestions=[
                            "Pass run_id to select a specific trace",
                            "Pass context_id to resolve a run",
                            "Omit sections and detailed when listing traces",
                        ],
                    )
                )

            # Every trace command below is a single request/response with no
            # subscription or streaming, so the shared pooled WebSocket owns the
            # connection lifecycle. A connection-shaped failure on the fetch
            # surfaces as CONNECTION_FAILED.

            await safe_progress(
                ctx,
                progress=1,
                total=3,
                message=(f"fetching trace {'detail' if resolved_run_id else 'list'}"),
            )

            if resolved_run_id:
                detail = await self._fetch_trace_detail(
                    domain_name,
                    item_id or "",
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
            result = await self._fetch_trace_list(
                domain_name,
                item_id,
                entity_id,
                limit=limit,
                offset=offset,
                order=order,
                ctx=ctx,
            )
            return result

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
        entity_id: str | None,
        run_id: str,
        *,
        detailed: bool,
        sections: str | None,
        ctx: Context | None,
        fallback_trace: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retrieve and format a single trace by run_id.

        ``fallback_trace`` is the component's extended trace dict for the
        matched run, already in hand from the context resolution. Use it
        instead of issuing a separate ``trace/get`` read.
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
                    {
                        "entity_id": entity_id,
                        "domain": domain,
                        "item_id": item_id,
                        "run_id": run_id,
                    },
                )
            trace_data = result.get("result", {})

        requested = _requested_sections(sections)
        logbook: TraceLogbook | None = None
        # An explicit section list decides on its own: detailed=True must not
        # spend a slow recorder query on rows the caller then filters out.
        if "logbook" in requested or (detailed and not requested):
            try:
                logbook = await fetch_trace_logbook(self._client, trace_data)
            except (ToolError, HomeAssistantError) as err:
                # The run's trace is already in hand; a recorder/logbook
                # failure must not discard it.
                message = (
                    extract_tool_error_message(err)
                    if isinstance(err, ToolError)
                    else str(err)
                )
                logbook = TraceLogbook([], None, [f"logbook unavailable: {message}"])
        await safe_progress(ctx, progress=3, total=3, message="formatting trace")
        formatted = _format_detailed_trace(
            entity_id,
            run_id,
            trace_data,
            domain=domain,
            item_id=item_id or None,
            sections=sections,
            logbook=logbook,
        )
        if logbook is not None and logbook.warnings:
            formatted.setdefault("warnings", []).extend(logbook.warnings)
        return formatted

    async def _fetch_trace_list(
        self,
        domain: str,
        item_id: str | None,
        entity_id: str | None,
        *,
        limit: int,
        offset: int,
        order: Literal["newest", "oldest"],
        ctx: Context | None,
    ) -> dict[str, Any]:
        """List recent traces, attaching diagnostics when none are stored."""
        message: dict[str, Any] = {"type": "trace/list", "domain": domain}
        if item_id is not None:
            message["item_id"] = item_id
        result = await self._client.send_websocket_message(message)

        if not result.get("success"):
            _raise_trace_ws_failure(
                result.get("error", "Failed to list traces"),
                {"entity_id": entity_id, "domain": domain, "item_id": item_id},
            )

        traces_data = result.get("result", [])
        list_warnings: list[str] = []
        if not traces_data and item_id is not None and entity_id is None:
            list_warnings.append(
                f"No traces found for {domain}/{item_id}; "
                "the item_id may be incorrect or the automation/script has not run."
            )

        if not traces_data and entity_id is not None:
            await safe_progress(
                ctx,
                progress=2,
                total=3,
                message="no traces; gathering diagnostics",
            )
            diagnostics, diagnostics_warnings = await _gather_diagnostics(
                self._client, entity_id, domain
            )
            list_warnings.extend(diagnostics_warnings)
            await safe_progress(
                ctx, progress=3, total=3, message="diagnostics complete"
            )
            return _format_trace_list(
                entity_id,
                traces_data,
                limit,
                diagnostics,
                domain=domain,
                offset=offset,
                order=order,
                warnings=list_warnings,
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
            domain=domain,
            offset=offset,
            order=order,
            warnings=list_warnings,
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
    if match.other_matches:
        detail["context_other_matches"] = list(match.other_matches)


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


def _trace_start(trace: dict[str, Any]) -> str:
    """Return Core's start timestamp, the sort key for ``trace/list`` rows."""
    return str(trace["timestamp"]["start"])


def _format_trace_list(
    entity_id: str | None,
    traces: list[dict[str, Any]],
    limit: int,
    diagnostics: dict[str, Any] | None = None,
    *,
    domain: str | None = None,
    offset: int = 0,
    order: Literal["newest", "oldest"] = "newest",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Format trace list for AI consumption.

    Args:
        entity_id: The automation or script entity_id, or None for a domain list
        traces: List of trace data from Home Assistant (sorted here by Core's
            start timestamp before paging)
        limit: Maximum number of traces to include
        diagnostics: Optional diagnostic information when traces are empty
        domain: The requested domain for a domain-wide list
        offset: Number of traces to skip from the start of the requested order
        order: 'newest' (default) returns most-recent first; 'oldest' chronological
    """
    # HA's trace/list returns runs first, then not-triggered traces, so list
    # position is not chronological. Sort by Core's own start timestamp before
    # picking the newest or oldest window.
    ordered = sorted(traces, key=_trace_start)
    if order == "newest":
        end = len(ordered) - offset
        start = max(end - limit, 0)
        window = list(reversed(ordered[start:end])) if end > 0 else []
    else:
        window = ordered[offset : offset + limit]

    # Pass Core's short dict through unchanged instead of projecting a
    # field allowlist; native keys such as script_execution stay as-is.
    formatted_traces = [dict(trace) for trace in window]

    result: dict[str, Any] = {
        "success": True,
        "trace_count": len(formatted_traces),
        "total_available": len(traces),
        "offset": offset,
        "order": order,
        "has_more": offset + len(formatted_traces) < len(traces),
        "traces": formatted_traces,
        "hint": (
            "Use run_id with domain and item_id (or entity_id) to get "
            "detailed trace information"
        ),
    }
    if entity_id is not None:
        result["entity_id"] = entity_id
    if domain is not None:
        result["domain"] = domain

    # Include diagnostics when traces are empty
    if diagnostics is not None and len(traces) == 0:
        result["diagnostics"] = diagnostics

    if warnings:
        result["warnings"] = list(warnings)

    return result


_TRACE_METADATA_KEYS = frozenset(
    {
        "success",
        "run_id",
        "domain",
        "item_id",
        "entity_id",
        "warnings",
        "logbook_entries",
        "logbook_transport",
        "logbook_total",
        "logbook_truncated",
    }
)

# Keys the tool adds around Core's extended dict rather than keys of the run.
# Core's own ``run_id``, ``domain`` and ``item_id`` stay in the run's key list.
_TOOL_ADDED_SECTION_KEYS = _TRACE_METADATA_KEYS - {"run_id", "domain", "item_id"}


def _format_detailed_trace(
    entity_id: str | None,
    run_id: str,
    trace: dict[str, Any],
    *,
    domain: str | None = None,
    item_id: str | None = None,
    sections: str | None = None,
    logbook: TraceLogbook | None = None,
) -> dict[str, Any]:
    """Return Core's extended trace dict with the tool's metadata.

    ``trace`` passes through unchanged unless ``sections`` keeps only the
    named top-level keys; a requested name this run lacks adds a ``warnings``
    line that lists the run's own keys.
    """
    result = dict(trace)
    result["success"] = True
    result["run_id"] = run_id
    if domain is not None:
        result["domain"] = domain
    if item_id is not None:
        result["item_id"] = item_id
    if entity_id is not None:
        result["entity_id"] = entity_id
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
    """Keep requested top-level Core keys; warn about names this run lacks."""
    requested = _requested_sections(sections)
    if not requested:
        return result
    # ``logbook`` is the tool's name for the fetched ``logbook_*`` fields, which
    # Core's extended dict does not contain.
    available = set(result)
    if "logbook_entries" in result:
        available.add("logbook")
    missing = sorted(requested - available)
    if missing:
        # The run's own keys, as Core's extended dict names them; the logbook
        # fields are the tool's name for the requested ``logbook`` section.
        run_keys = sorted(set(result) - _TOOL_ADDED_SECTION_KEYS)
        if "logbook_entries" in result:
            run_keys.append("logbook")
        result.setdefault("warnings", []).append(
            "Trace section(s) not in this run: "
            + ", ".join(missing)
            + ". This run has: "
            + ", ".join(run_keys)
        )
    keep = requested | _TRACE_METADATA_KEYS
    return {key: value for key, value in result.items() if key in keep}
