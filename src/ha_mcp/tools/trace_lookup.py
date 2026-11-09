"""Native context and logbook lookups for trace debugging."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, NoReturn, cast

from ..client.rest_client import (
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
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
    _wrapped_timeout,
)
from .util_helpers import is_connection_error_message

logger = logging.getLogger(__name__)

PARENT_CONTEXT_SCAN_LIMIT = 50
LOGBOOK_ROW_LIMIT = 50
# Returned by _read_candidate_context when a dropped connection ends the scan.
_SCAN_ABORTED = object()
# A state trigger with a ``for:`` duration fires long after the state change
# that shares the triggering context, so the parent-context read reaches back
# further than the run's own window.
PARENT_CONTEXT_LOOKBACK = timedelta(minutes=5)


@dataclass(frozen=True)
class ContextTraceMatch:
    """A trace selected by a context id."""

    run_id: str
    match: Literal["trace", "parent"]
    scanned: int
    truncated: bool
    scan_errors: tuple[str, ...] = ()
    trace_data: dict[str, Any] | None = None


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
    the native contract Core serves; a 50-trace scan measured about 50 ms on
    a local HA, and the bound keeps the worst case and response explicit.
    Revisiting it is a design change, not a missing field.
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


def _raise_context_not_found(
    context_id: str,
    domain: str,
    item_id: str,
    scanned: int,
    truncated: bool,
    scan_errors: list[str],
) -> NoReturn:
    """Raise the structured error for an unsuccessful parent-context scan."""
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
            "scanned; pass the trace's run_id",
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


def _scan_candidates(traces: Any, scan_errors: list[str]) -> list[dict[str, Any]]:
    """Return the readable trace rows newest first, recording the skipped ones."""
    if not isinstance(traces, list):
        scan_errors.append("trace/list returned no row list")
        return []
    candidates: list[dict[str, Any]] = []
    for trace in traces:
        if not isinstance(trace, dict):
            continue
        if not trace.get("run_id"):
            # A row without a run_id cannot be read, so say so instead of
            # letting the scan look like it covered every stored trace.
            scan_errors.append("trace/list row without a run_id")
            continue
        candidates.append(trace)
    candidates.sort(key=_trace_start, reverse=True)
    return candidates


def _match_kind(context: Any, context_id: str) -> Literal["trace", "parent"] | None:
    """Say how one stored trace's context matches the requested context id."""
    if not isinstance(context, dict):
        return None
    if context.get("parent_id") == context_id:
        return "parent"
    # trace/contexts can answer before a concurrent restoration finishes, so the
    # scan also honours the run's own context id.
    if context.get("id") == context_id:
        return "trace"
    return None


async def _read_candidate_context(
    client: Any,
    domain: str,
    item_id: str,
    run_id: str,
    scan_errors: list[str],
    *,
    have_match: bool = False,
) -> Any:
    """Read one stored trace, recording why it could not be inspected.

    A dropped connection aborts the scan: it raises when the scan has nothing
    to fall back on, and otherwise reports the abort so a match already read
    is returned instead of being discarded.
    """
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
            if not have_match:
                _raise_trace_ws_failure(
                    error,
                    {"domain": domain, "item_id": item_id, "run_id": run_id},
                )
            scan_errors.append(f"{run_id}: the scan stopped after a connection failure")
            return _SCAN_ABORTED
        scan_errors.append(f"{run_id}: {error}")
        return None
    trace_data = detail.get("result")
    if not isinstance(trace_data, dict):
        scan_errors.append(f"{run_id}: trace/get returned no trace detail")
        return None
    return trace_data


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
    candidates = _scan_candidates(traces, scan_errors)
    truncated = len(candidates) > PARENT_CONTEXT_SCAN_LIMIT
    candidates = candidates[:PARENT_CONTEXT_SCAN_LIMIT]

    match, scanned = await _scan_for_context(
        client, domain, item_id, context_id, candidates, truncated, scan_errors
    )
    if match is not None:
        return match

    _raise_context_not_found(
        context_id, domain, item_id, scanned, truncated, scan_errors
    )


async def _scan_for_context(
    client: Any,
    domain: str,
    item_id: str,
    context_id: str,
    candidates: list[dict[str, Any]],
    truncated: bool,
    scan_errors: list[str],
) -> tuple[ContextTraceMatch | None, int]:
    """Walk the bounded candidate list and settle on one stored trace."""
    scanned = 0
    parent_match: tuple[str, dict[str, Any]] | None = None
    fallback_match: tuple[str, dict[str, Any]] | None = None
    for trace in candidates:
        scanned += 1
        run_id = str(trace["run_id"])
        trace_data = await _read_candidate_context(
            client,
            domain,
            item_id,
            run_id,
            scan_errors,
            have_match=parent_match is not None or fallback_match is not None,
        )
        if trace_data is _SCAN_ABORTED:
            break
        if trace_data is None:
            continue
        kind = _match_kind(trace_data.get("context"), context_id)
        if kind == "trace":
            # The run that owns the requested context id is authoritative. It
            # can appear after a newer run that merely names it as a parent, so
            # the bounded scan checks every candidate before settling.
            return (
                ContextTraceMatch(
                    run_id, "trace", scanned, truncated, tuple(scan_errors), trace_data
                ),
                scanned,
            )
        if kind == "parent":
            if trace.get("not_triggered"):
                # A not-triggered diagnostic is useful, but a real run for the
                # same triggering context is the better match.
                if fallback_match is None:
                    fallback_match = (run_id, trace_data)
                continue
            if parent_match is None:
                parent_match = (run_id, trace_data)

    match = _scan_result(scanned, truncated, scan_errors, parent_match, fallback_match)
    return match, scanned


def _scan_result(
    scanned: int,
    truncated: bool,
    scan_errors: list[str],
    parent_match: tuple[str, dict[str, Any]] | None,
    fallback_match: tuple[str, dict[str, Any]] | None,
) -> ContextTraceMatch | None:
    """Choose the stored trace the bounded scan settled on, if any."""
    match = parent_match or fallback_match
    if match is None:
        return None
    run_id, trace_data = match
    return ContextTraceMatch(
        run_id, "parent", scanned, truncated, tuple(scan_errors), trace_data
    )


def _logbook_window_warnings(trace: dict[str, Any]) -> list[str]:
    """Warn about each logbook bound the window had to synthesize."""
    timestamp = trace.get("timestamp")
    raw_start = timestamp.get("start") if isinstance(timestamp, dict) else None
    raw_finish = timestamp.get("finish") if isinstance(timestamp, dict) else None
    start = _trace_datetime(raw_start)
    finish = _trace_datetime(raw_finish)
    warnings: list[str] = []
    if finish is None:
        if raw_finish:
            warnings.append(
                "The trace's finish timestamp is unreadable, so the logbook "
                "window ends at this server's clock and may exclude rows "
                "recorded after it."
            )
        else:
            warnings.append(
                "The trace has no finish timestamp, so the logbook window ends "
                "at this server's clock and may exclude rows recorded after it."
            )
    if start is None or (finish is not None and start >= finish):
        warnings.append(
            "The trace's start timestamp is unusable, so the logbook window "
            "starts one minute before the finish bound and may omit earlier rows."
        )
    return warnings


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
    # Read the run's own context first: its rows describe the run itself and
    # must survive the row cap even when the triggering context is busy.
    contexts: list[tuple[str, str]] = [(str(context_id), start_time)]
    parent_id = context.get("parent_id")
    if isinstance(parent_id, str) and parent_id:
        parent_start = (
            datetime.fromisoformat(start_time) - PARENT_CONTEXT_LOOKBACK
        ).isoformat()
        contexts.append((parent_id, parent_start))

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    warnings = _logbook_window_warnings(trace)
    transports: set[str] = set()
    for index, (lookup_id, lookup_start) in enumerate(contexts):
        entries, transport, context_warnings, timed_out = await _fetch_logbook_context(
            client, lookup_id, lookup_start, end_time
        )
        warnings.extend(context_warnings)
        if transport:
            transports.add(transport)
        for row in entries:
            # Compare the whole native row: two device events can share the
            # projected fields and timestamp and still be distinct.
            key = json.dumps(row, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            merged.append(row)
        if timed_out:
            # The recorder is already past one deadline; a second query would
            # double the wait, so stop and say which context went unread.
            skipped = [skipped_id for skipped_id, _ in contexts[index + 1 :]]
            if skipped:
                warnings.append(
                    "Skipped the logbook read for "
                    + ", ".join(skipped)
                    + " after the component read timed out."
                )
            break

    total = len(merged)
    entries = merged[:LOGBOOK_ROW_LIMIT]
    transport = cast(
        Literal["component", "rest", "mixed"] | None,
        "mixed" if len(transports) > 1 else next(iter(transports), None),
    )
    return TraceLogbook(
        entries,
        transport,
        warnings,
        total,
        total > LOGBOOK_ROW_LIMIT,
    )


async def _fetch_logbook_context(
    client: Any,
    context_id: str,
    start_time: str,
    end_time: str,
) -> tuple[list[dict[str, Any]], str | None, list[str], bool]:
    """Read one context's logbook, component first with a REST fallback."""
    entries, warnings, retry_rest = await _fetch_component_logbook(
        client, context_id, start_time, end_time
    )
    if entries is not None:
        return entries, "component", warnings, False
    if not retry_rest:
        return [], None, warnings, True

    try:
        rest_entries = await client.get_logbook(
            start_time=start_time,
            end_time=end_time,
            context_id=context_id,
        )
    except Exception as err:  # noqa: BLE001
        warnings.append(f"Could not read the trace logbook: {err}")
        return [], None, warnings, False

    if not isinstance(rest_entries, list):
        warnings.append("The REST logbook reply was not a row list.")
        return [], None, warnings, False
    return rest_entries, "rest", warnings, False


def _component_logbook_error(err: Exception) -> tuple[str, bool]:
    """Turn a component read failure into a warning and a REST-retry choice."""
    if isinstance(err, HomeAssistantCommandTimeout) or (
        isinstance(err, HomeAssistantConnectionError) and _wrapped_timeout(err)
    ):
        return f"The component logbook read timed out: {err}", False
    return f"The component logbook read failed: {err}", True


async def _fetch_component_logbook(
    client: Any,
    context_id: str,
    start_time: str,
    end_time: str,
) -> tuple[list[dict[str, Any]] | None, list[str], bool]:
    """Try the component's logbook processor.

    The third value says whether the REST fallback may run. A timeout does
    not retry: the recorder query can still be running, and re-issuing it
    over REST adds a second slow scan instead of answering faster.
    """
    warnings: list[str] = []
    try:
        caps = await get_component_caps(client)
    except Exception as err:  # noqa: BLE001
        return None, [f"Could not probe the logbook component: {err}"], True

    if not component_supports(caps, LOGBOOK_EVENTS_CAPABILITY):
        return None, warnings, True

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
        warning, retry_rest = _component_logbook_error(err)
        return None, [warning], retry_rest

    if isinstance(response, dict) and response.get("success"):
        result = response.get("result")
        if isinstance(result, list):
            return result, warnings, True
        warnings.append("The component logbook reply did not contain a row list.")
        return None, warnings, True

    if isinstance(response, dict) and response.get("error_code") == "unknown_command":
        invalidate_caps(client)
    reason = response.get("error") if isinstance(response, dict) else None
    warning = "The component logbook read was unavailable; used REST."
    if reason:
        warning = f"{warning} ({reason})"
    warnings.append(warning)
    return None, warnings, True


def _logbook_window(
    trace: dict[str, Any], now: datetime | None = None
) -> tuple[str, str]:
    """Return a bounded start/finish for the logbook query.

    The trace's own timestamps bracket the action trace, while the recorder's
    logbook row for the triggering event can land just outside that window.
    Context filtering keeps the widened window scoped to this run. A malformed
    timestamp is replaced from the other bound, and a running trace's window
    reaches ``now`` (the wall clock unless a caller passes a fixed instant), so
    both reads still receive an ordered window instead of failing.
    """
    timestamp = trace.get("timestamp")
    start = _trace_datetime(
        timestamp.get("start") if isinstance(timestamp, dict) else None
    )
    finish = _trace_datetime(
        timestamp.get("finish") if isinstance(timestamp, dict) else None
    )
    if finish is None:
        # A trace still running has no finish timestamp; its logbook keeps
        # growing, so the read must reach the present instead of one minute
        # past the start.
        current = now or datetime.now(UTC)
        finish = (
            current
            if start is None or current > start
            else start + timedelta(minutes=1)
        )
    if start is None or start >= finish:
        start = finish - timedelta(minutes=1)
    start -= timedelta(seconds=2)
    finish += timedelta(seconds=2)
    return start.isoformat(), finish.isoformat()


def _trace_datetime(value: Any) -> datetime | None:
    """Parse a trace timestamp into an aware datetime, or None when unusable."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _trace_start(trace: dict[str, Any]) -> str:
    """Return Core's start timestamp, or its finish, as a sortable string."""
    timestamp = trace.get("timestamp")
    if isinstance(timestamp, dict):
        start = timestamp.get("start")
        if isinstance(start, str):
            return start
        finish = timestamp.get("finish")
        if isinstance(finish, str):
            return finish
        return ""
    return timestamp if isinstance(timestamp, str) else ""


def _requested_sections(sections: str | None) -> set[str]:
    """Return the lower-cased section names requested by the caller."""
    requested: set[str] = set()
    if not sections:
        return requested
    for section in sections.split(","):
        normalized = section.strip().lower()
        if normalized:
            requested.add(normalized)
    return requested
