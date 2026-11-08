"""Native context and logbook lookups for trace debugging."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ..errors import ErrorCode, create_error_response
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
)
from .helpers import raise_tool_error
from .log_sources_logbook import (
    LOGBOOK_EVENTS_CAPABILITY,
    LOGBOOK_EVENTS_COMMAND,
)
from .trace_formatting import _trace_start
from .util_helpers import is_connection_error_message

logger = logging.getLogger(__name__)

PARENT_CONTEXT_SCAN_LIMIT = 50
LOGBOOK_ROW_LIMIT = 50


@dataclass(frozen=True)
class ContextTraceMatch:
    """A trace selected by a context id."""

    run_id: str
    match: Literal["trace", "parent"]
    scanned: int
    truncated: bool
    scan_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class TraceLogbook:
    """Logbook rows for one trace run."""

    entries: list[dict[str, Any]]
    transport: Literal["component", "rest", "mixed"] | None
    warnings: list[str]
    total: int = 0
    truncated: bool = False


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


async def resolve_context_trace(
    client: Any,
    domain: str,
    item_id: str,
    context_id: str,
) -> ContextTraceMatch:
    """Resolve a trace by its own context id or a triggering parent context.

    Core's ``trace/contexts`` command is the authoritative map for a trace's
    own ``context.id``. Core exposes no parent-context index: a triggering
    event or service context only appears as the trace's ``context.parent_id``.
    When the direct map has no entry, scan the item's stored traces by start
    time, newest first, and match that parent id. The scan is bounded; the
    response says when the bound was hit or a row could not be read instead of
    pretending the trace does not exist.

    This is intentionally a public-API scan. A component bridge could read
    ``hass.data[DATA_TRACE]`` in process, but that couples a diagnostic
    fallback to private Core storage and adds a versioned command for a path
    with no public index. The bounded ``trace/list`` + ``trace/get`` read is
    the native contract Core serves; revisiting it is a design change, not a
    missing field.
    """
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
    if run_id:
        return ContextTraceMatch(str(run_id), "trace", 0, False)

    return await _resolve_parent_context(client, domain, item_id, context_id)


async def _resolve_parent_context(
    client: Any,
    domain: str,
    item_id: str,
    context_id: str,
) -> ContextTraceMatch:
    """Scan stored traces for one whose context.parent_id matches."""
    result = await client.send_websocket_message(
        {"type": "trace/list", "domain": domain, "item_id": item_id}
    )
    if not result.get("success"):
        _raise_trace_ws_failure(
            result.get("error", "Failed to list traces for context lookup"),
            {"context_id": context_id, "domain": domain, "item_id": item_id},
        )

    traces = result.get("result")
    scan_errors: list[str] = []
    if not isinstance(traces, list):
        scan_errors.append("trace/list returned no row list")
        traces = []
    candidates = [
        trace for trace in traces if isinstance(trace, dict) and trace.get("run_id")
    ]
    candidates.sort(key=_trace_start, reverse=True)
    truncated = len(candidates) > PARENT_CONTEXT_SCAN_LIMIT
    candidates = candidates[:PARENT_CONTEXT_SCAN_LIMIT]

    scanned = 0
    for trace in candidates:
        scanned += 1
        run_id = str(trace["run_id"])
        detail = await client.send_websocket_message(
            {
                "type": "trace/get",
                "domain": domain,
                "item_id": item_id,
                "run_id": run_id,
            }
        )
        if not detail.get("success"):
            error = detail.get("error")
            if is_connection_error_message(error):
                _raise_trace_ws_failure(
                    error,
                    {
                        "context_id": context_id,
                        "domain": domain,
                        "item_id": item_id,
                    },
                )
            scan_errors.append(f"{run_id}: {error}")
            continue
        trace_data = detail.get("result")
        context = trace_data.get("context") if isinstance(trace_data, dict) else None
        if isinstance(context, dict) and context.get("parent_id") == context_id:
            return ContextTraceMatch(
                run_id, "parent", scanned, truncated, tuple(scan_errors)
            )

    suggestions = [
        "Pass the trace's own `context.id`, or the context id of the event "
        "or service call that triggered it",
        "Use ha_get_automation_traces(entity_id=...) without context_id "
        "to list recent traces",
    ]
    if truncated:
        suggestions.insert(
            0,
            f"Only the newest {PARENT_CONTEXT_SCAN_LIMIT} stored traces were "
            "scanned; increase stored_traces or pass the trace's run_id",
        )
    if scan_errors:
        suggestions.insert(
            0,
            "Some stored traces could not be read; retry after checking "
            "the Home Assistant logs",
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"No trace found for context: {context_id}",
            context={
                "context_id": context_id,
                "domain": domain,
                "item_id": item_id,
                "scanned": scanned,
                "scan_truncated": truncated,
                "scan_errors": scan_errors,
            },
            suggestions=suggestions,
        )
    )


async def fetch_trace_logbook(
    client: Any,
    trace: dict[str, Any],
) -> TraceLogbook:
    """Fetch logbook rows for the run and its triggering context, component first.

    Core's ``trace/get`` no longer carries logbook entries. The native source
    is Core's logbook processor filtered by context. The run's own context
    holds the automation/script rows; a triggering event or service call is in
    the run's ``context.parent_id``, so both are queried. The component's
    ``ha_mcp_tools/logbook_events`` command runs the processor in process;
    Core's REST ``/api/logbook`` is the fallback. The trace window is widened
    by two seconds on each side because recorder rows can land just outside
    the action trace.

    Rows are capped at ``LOGBOOK_ROW_LIMIT``; ``total`` and ``truncated`` tell
    the caller what was left out.
    """
    context = trace.get("context")
    if not isinstance(context, dict):
        context = {}
    context_id = context.get("id")
    if not context_id:
        return TraceLogbook(
            [],
            None,
            ["No context id is available, so the trace logbook was not read."],
        )

    start_time, end_time = _logbook_window(trace)
    contexts: list[str] = []
    parent_id = context.get("parent_id")
    if isinstance(parent_id, str) and parent_id:
        contexts.append(parent_id)
    contexts.append(str(context_id))

    merged: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    warnings: list[str] = []
    transports: set[str] = set()
    for lookup_id in contexts:
        entries, transport, context_warnings = await _fetch_logbook_context(
            client, lookup_id, start_time, end_time
        )
        warnings.extend(context_warnings)
        if transport:
            transports.add(transport)
        for row in entries:
            key = (
                row.get("context_id"),
                row.get("when"),
                row.get("name"),
                row.get("message"),
                row.get("entity_id"),
                row.get("state"),
                row.get("domain"),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)

    total = len(merged)
    entries = merged[:LOGBOOK_ROW_LIMIT]
    transport = "mixed" if len(transports) > 1 else (next(iter(transports), None))
    return TraceLogbook(
        entries,
        transport,  # type: ignore[arg-type]
        warnings,
        total,
        total > LOGBOOK_ROW_LIMIT,
    )


async def _fetch_logbook_context(
    client: Any,
    context_id: str,
    start_time: str,
    end_time: str,
) -> tuple[list[dict[str, Any]], str | None, list[str]]:
    """Read one context's logbook, component first with a REST fallback."""
    entries, warnings = await _fetch_component_logbook(
        client, context_id, start_time, end_time
    )
    if entries is not None:
        return entries, "component", warnings

    try:
        rest_entries = await client.get_logbook(
            start_time=start_time,
            end_time=end_time,
            context_id=context_id,
        )
    except Exception as err:  # noqa: BLE001
        warnings.append(f"Could not read the trace logbook: {err}")
        return [], None, warnings

    if not isinstance(rest_entries, list):
        warnings.append("The REST logbook reply was not a row list.")
        return [], None, warnings
    return rest_entries, "rest", warnings


async def _fetch_component_logbook(
    client: Any,
    context_id: str,
    start_time: str,
    end_time: str,
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Try the component's logbook processor; return rows or warnings."""
    warnings: list[str] = []
    try:
        caps = await get_component_caps(client)
    except Exception as err:  # noqa: BLE001
        return None, [f"Could not probe the logbook component: {err}"]

    if not component_supports(caps, LOGBOOK_EVENTS_CAPABILITY):
        return None, warnings

    try:
        response = await client.send_websocket_message(
            {
                "type": LOGBOOK_EVENTS_COMMAND,
                "start_time": start_time,
                "end_time": end_time,
                "context_id": context_id,
            }
        )
    except Exception as err:  # noqa: BLE001
        return None, [f"The component logbook read failed: {err}"]

    if isinstance(response, dict) and response.get("success"):
        result = response.get("result")
        if isinstance(result, list):
            return result, warnings
        warnings.append("The component logbook reply did not contain a row list.")
        return None, warnings

    if isinstance(response, dict) and response.get("error_code") == "unknown_command":
        invalidate_caps(client)
    reason = response.get("error") if isinstance(response, dict) else None
    warning = "The component logbook read was unavailable; used REST."
    if reason:
        warning = f"{warning} ({reason})"
    warnings.append(warning)
    return None, warnings


def _logbook_window(trace: dict[str, Any]) -> tuple[str, str]:
    """Return a slightly widened start/finish for the logbook query.

    The trace's own timestamps bracket the action trace, while the recorder's
    logbook row for the triggering event can land just outside that window.
    Context filtering keeps the widened window scoped to this run.
    """
    timestamp = trace.get("timestamp")
    start = timestamp.get("start") if isinstance(timestamp, dict) else None
    finish = timestamp.get("finish") if isinstance(timestamp, dict) else None
    now = datetime.now(UTC)
    if isinstance(start, str):
        try:
            start = (datetime.fromisoformat(start) - timedelta(seconds=2)).isoformat()
        except ValueError:
            pass
    else:
        start = (now - timedelta(minutes=1)).isoformat()
    if isinstance(finish, str):
        try:
            finish = (datetime.fromisoformat(finish) + timedelta(seconds=2)).isoformat()
        except ValueError:
            pass
    else:
        finish = now.isoformat()
    return start, finish
