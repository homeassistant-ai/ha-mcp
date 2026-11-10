"""Native context and logbook lookups for trace debugging."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn, cast

from ..client.rest_client import HomeAssistantConnectionError, HomeAssistantError
from ..errors import ErrorCode, create_error_response
from .component_api import component_supports, get_component_caps, invalidate_caps
from .helpers import raise_tool_error
from .log_sources_logbook import LogbookLogSourceMixin
from .util_helpers import is_connection_error_message

logger = logging.getLogger(__name__)

PARENT_CONTEXT_SCAN_LIMIT = 50
TRACE_CONTEXT_COMMAND = "ha_mcp_tools/trace_context"
TRACE_CONTEXT_CAPABILITY = "trace_context"
LOGBOOK_ROW_LIMIT = 50
# Returned by _read_candidate_context when a dropped connection ends the scan.
_SCAN_ABORTED = object()


@dataclass(frozen=True)
class ContextTraceMatch:
    """A trace selected by a context id."""

    run_id: str
    match: Literal["trace", "parent"]
    scanned: int
    truncated: bool
    scan_errors: tuple[str, ...] = ()
    trace_data: dict[str, Any] | None = None
    domain: str | None = None
    item_id: str | None = None
    other_matches: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class _ComponentContext:
    """A component scan outcome; ``match`` is None for an authoritative miss."""

    match: ContextTraceMatch | None
    scanned: int


async def _component_context(
    client: Any,
    context_id: str,
    domain: str | None = None,
    item_id: str | None = None,
) -> _ComponentContext | None:
    """Match a context in Core's live trace store through the component.

    Returns None when the component is absent or did not answer, so the caller
    keeps its public API fallback. A reply with no match is authoritative: the
    component scanned every stored trace in process.
    """
    if not component_supports(
        await get_component_caps(client), TRACE_CONTEXT_CAPABILITY
    ):
        return None
    message: dict[str, Any] = {
        "type": TRACE_CONTEXT_COMMAND,
        "context_id": context_id,
    }
    if domain is not None:
        message["domain"] = domain
    if item_id is not None:
        message["item_id"] = item_id
    try:
        result = await client.send_websocket_message(message)
    except HomeAssistantConnectionError as err:
        logger.warning("%s got no answer: %s", TRACE_CONTEXT_COMMAND, err)
        return None
    if not result.get("success"):
        if result.get("error_code") == "unknown_command":
            logger.debug("%s is not registered", TRACE_CONTEXT_COMMAND)
            invalidate_caps(client)
        else:
            logger.warning("%s failed: %s", TRACE_CONTEXT_COMMAND, result.get("error"))
        return None
    return _component_payload(result.get("result"))


def _component_payload(payload: Any) -> _ComponentContext | None:
    """Parse one component trace-context reply; None means keep the fallback."""
    if not isinstance(payload, dict):
        return None
    if payload.get("available") is False:
        # The component is loaded but Core's trace store is not set up; keep
        # the public fallback available.
        return None
    scanned = payload.get("scanned")
    scanned_count = scanned if isinstance(scanned, int) and scanned >= 0 else 0
    kind = payload.get("match")
    if kind not in ("trace", "parent"):
        return _ComponentContext(None, scanned_count)
    run_id = payload.get("run_id")
    item_domain = payload.get("domain")
    item_id_value = payload.get("item_id")
    trace_data = payload.get("trace")
    if not (
        isinstance(run_id, str)
        and isinstance(item_domain, str)
        and isinstance(item_id_value, str)
        and isinstance(trace_data, dict)
    ):
        return None
    other_matches = _normalize_other_matches(payload.get("other_matches"))
    return _ComponentContext(
        ContextTraceMatch(
            run_id,
            cast(Literal["trace", "parent"], kind),
            scanned_count,
            False,
            (),
            trace_data,
            item_domain,
            item_id_value,
            other_matches,
        ),
        scanned_count,
    )


def _normalize_other_matches(value: Any) -> tuple[dict[str, str], ...]:
    """Return a tuple of validated other-match dicts from the component reply."""
    if not isinstance(value, list):
        return ()
    result: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if not all(
            isinstance(item.get(key), str)
            for key in ("domain", "item_id", "run_id", "match")
        ):
            continue
        if item.get("match") not in ("trace", "parent"):
            continue
        result.append(
            {
                "domain": str(item["domain"]),
                "item_id": str(item["item_id"]),
                "run_id": str(item["run_id"]),
                "match": str(item["match"]),
            }
        )
    return tuple(result)


@dataclass(frozen=True)
class TraceLogbook:
    """Logbook rows for one trace run."""

    entries: list[dict[str, Any]]
    transport: Literal["component", "rest", "mixed"] | None
    warnings: list[str]
    total: int = 0
    truncated: bool = False


def _raise_trace_ws_failure(error_msg: Any, context: dict[str, Any]) -> NoReturn:
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

    """
    component = await _component_context(client, context_id, domain, item_id)
    if component is not None:
        if component.match is None:
            _raise_context_not_found(
                context_id, domain, item_id, component.scanned, False, []
            )
        return component.match

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


async def resolve_global_context(
    client: Any,
    context_id: str,
    domain: Literal["automation", "script"] | None = None,
) -> ContextTraceMatch:
    """Resolve a context against Core's global trace/contexts map.

    Core accepts ``trace/contexts`` without domain/item and returns every
    stored run's own context. When that map has no entry, a bounded
    ``trace/list`` + ``trace/get`` scan matches the run's parent context.
    """
    component = await _component_context(client, context_id, domain)
    if component is not None:
        if component.match is None:
            _raise_global_context_not_found(context_id, component.scanned, False, [])
        return component.match

    result = await client.send_websocket_message({"type": "trace/contexts"})
    if not result.get("success"):
        _raise_trace_ws_failure(
            result.get("error", "Failed to resolve trace context"),
            {"context_id": context_id},
        )
    contexts = result.get("result")
    entry = contexts.get(context_id) if isinstance(contexts, dict) else None
    if not isinstance(entry, dict) or not entry.get("run_id"):
        return await _resolve_global_parent_context(client, context_id, domain)
    entry_domain = entry.get("domain")
    if domain is not None and entry_domain != domain:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"The context belongs to a {entry_domain or 'different'} run, "
                f"not a {domain} run",
                context={"context_id": context_id, "domain": domain},
                suggestions=[
                    "Omit domain to resolve the context across both domains",
                    "Pass the matching domain for this context",
                ],
            )
        )
    return ContextTraceMatch(
        str(entry["run_id"]),
        "trace",
        0,
        False,
        (),
        None,
        entry_domain,
        entry.get("item_id"),
    )


async def _global_trace_rows(
    client: Any,
    domain: Literal["automation", "script"] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Collect trace/list rows across the requested trace domains."""
    rows: list[dict[str, Any]] = []
    scan_errors: list[str] = []
    domains = (domain,) if domain is not None else ("automation", "script")
    for scan_domain in domains:
        try:
            result = await client.send_websocket_message(
                {"type": "trace/list", "domain": scan_domain}
            )
        except HomeAssistantError as err:
            scan_errors.append(f"{scan_domain}: {err}")
            continue
        if not result.get("success"):
            scan_errors.append(
                f"{scan_domain}: {result.get('error') or 'trace/list failed'}"
            )
            continue
        entries = result.get("result")
        if not isinstance(entries, list):
            scan_errors.append(f"{scan_domain}: trace/list returned no row list")
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("run_id"):
                continue
            item_id = entry.get("item_id")
            if not isinstance(item_id, str) or not item_id:
                continue
            row = dict(entry)
            row["_domain"] = scan_domain
            rows.append(row)
    return rows, scan_errors


async def _scan_global_parent(
    client: Any,
    context_id: str,
    rows: list[dict[str, Any]],
    truncated: bool,
    scan_errors: list[str],
) -> tuple[ContextTraceMatch | None, int]:
    """Walk the bounded global rows and settle on one stored trace."""
    scanned = 0
    matches: list[tuple[str, dict[str, Any], str, str, str]] = []
    for trace in rows:
        scanned += 1
        domain = str(trace["_domain"])
        item_id = str(trace["item_id"])
        run_id = str(trace["run_id"])
        trace_data = await _read_candidate_context(
            client,
            domain,
            item_id,
            run_id,
            scan_errors,
            have_match=len(matches) > 0,
        )
        if trace_data is _SCAN_ABORTED:
            break
        if trace_data is None:
            continue
        kind = _match_kind(trace_data.get("context"), context_id)
        if kind in ("trace", "parent"):
            matches.append((run_id, trace_data, domain, item_id, kind))

    return _select_trace_match(matches, scanned, truncated, scan_errors), scanned


async def _resolve_global_parent_context(
    client: Any,
    context_id: str,
    domain: Literal["automation", "script"] | None = None,
) -> ContextTraceMatch:
    """Scan stored runs for a parent context, bounded like the per-item scan."""
    rows, scan_errors = await _global_trace_rows(client, domain)
    rows.sort(key=_trace_start, reverse=True)
    truncated = len(rows) > PARENT_CONTEXT_SCAN_LIMIT
    match, scanned = await _scan_global_parent(
        client,
        context_id,
        rows[:PARENT_CONTEXT_SCAN_LIMIT],
        truncated,
        scan_errors,
    )
    if match is None:
        if (
            not rows
            and scan_errors
            and all(is_connection_error_message(error) for error in scan_errors)
        ):
            _raise_trace_ws_failure("; ".join(scan_errors), {"context_id": context_id})
        _raise_global_context_not_found(context_id, scanned, truncated, scan_errors)
    return match


def _raise_global_context_not_found(
    context_id: str,
    scanned: int,
    truncated: bool,
    scan_errors: list[str],
) -> NoReturn:
    """Raise a not-found that keeps the global scan's bound and errors."""
    suggestions = [
        "Omit domain to scan both automations and scripts",
        "Use ha_get_automation_traces(domain=...) to list recent runs",
    ]
    if truncated:
        suggestions.insert(
            0,
            f"Only the newest {PARENT_CONTEXT_SCAN_LIMIT} stored runs across "
            + "automations and scripts were scanned; pass run_id with domain "
            + "and item_id",
        )
    if scan_errors:
        suggestions.insert(
            0,
            "Some stored traces could not be read; retry after checking "
            + "the Home Assistant logs",
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"No trace found for context: {context_id}",
            context={
                "context_id": context_id,
                "scanned": scanned,
                "scan_truncated": truncated,
                "scan_errors": scan_errors,
            },
            suggestions=suggestions,
        )
    )


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
        + "or service call that triggered it",
        "Use ha_get_automation_traces(entity_id=...) without context_id "
        + "to list recent traces",
    ]
    if truncated:
        suggestions.insert(
            0,
            f"Only the newest {PARENT_CONTEXT_SCAN_LIMIT} stored traces were "
            + "scanned; pass the trace's run_id",
        )
    if scan_errors:
        suggestions.insert(
            0,
            "Some stored traces could not be read; retry after checking "
            + "the Home Assistant logs",
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
    raise AssertionError("unreachable")


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
    matches: list[tuple[str, dict[str, Any], str, str, str]] = []
    for trace in candidates:
        scanned += 1
        run_id = str(trace["run_id"])
        trace_data = await _read_candidate_context(
            client,
            domain,
            item_id,
            run_id,
            scan_errors,
            have_match=len(matches) > 0,
        )
        if trace_data is _SCAN_ABORTED:
            break
        if trace_data is None:
            continue
        kind = _match_kind(trace_data.get("context"), context_id)
        if kind in ("trace", "parent"):
            matches.append((run_id, trace_data, domain, item_id, kind))

    return _select_trace_match(matches, scanned, truncated, scan_errors), scanned


def _select_trace_match(
    matches: list[tuple[str, dict[str, Any], str, str, str]],
    scanned: int,
    truncated: bool,
    scan_errors: list[str],
) -> ContextTraceMatch | None:
    """Select the newest match and list the rest with the component rule."""
    if not matches:
        return None

    def _start_key(match: tuple[str, dict[str, Any], str, str, str]) -> str:
        run_id, trace_data, domain, item_id, kind = match
        return _trace_start(trace_data)

    def _tie_break_key(
        match: tuple[str, dict[str, Any], str, str, str],
    ) -> tuple[int, int, str]:
        _run_id, _trace_data, domain, item_id, kind = match
        match_order = 0 if kind == "trace" else 1
        domain_order = 0 if domain == "automation" else 1
        return (match_order, domain_order, item_id)

    newest_start = max(_start_key(m) for m in matches)
    candidates = [m for m in matches if _start_key(m) == newest_start]
    selected = min(candidates, key=_tie_break_key)
    run_id, trace_data, domain, item_id, kind = selected
    other_matches = tuple(
        {
            "domain": d,
            "item_id": i,
            "run_id": r,
            "match": k,
        }
        for r, _td, d, i, k in matches
        if r != run_id or d != domain or i != item_id
    )
    return ContextTraceMatch(
        run_id,
        cast(Literal["trace", "parent"], kind),
        scanned,
        truncated,
        tuple(scan_errors),
        trace_data,
        domain,
        item_id,
        other_matches,
    )


class _TraceLogbookReader(LogbookLogSourceMixin):
    """Adapter so trace lookups use the shared component logbook helper."""

    def __init__(self, client: Any) -> None:
        self._client = client


async def fetch_trace_logbook(
    client: Any,
    trace: dict[str, Any],
) -> TraceLogbook:
    """Fetch the run's logbook through Core's own start/context query.

    The trace page asks Core for ``logbook/get_events`` with the trace's
    start time and the run's context id, and no end time (Core reads that as
    now). The component's ``ha_mcp_tools/logbook_events`` command runs the same
    processor in process; REST is the explicit no-component fallback. REST
    defaults an omitted end to ``start + 1 day``, so the fallback sends now
    explicitly to keep the same window.
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

    timestamp = trace.get("timestamp")
    start_time = timestamp.get("start") if isinstance(timestamp, dict) else None
    if not isinstance(start_time, str) or not start_time:
        return TraceLogbook(
            [],
            None,
            ["No trace start timestamp is available, so the logbook was not read."],
        )

    reader = _TraceLogbookReader(client)
    entries = await reader._fetch_logbook_component(
        {"start_time": start_time, "context_id": str(context_id)}
    )
    transport: Literal["component", "rest"] = "component"
    if entries is None:
        transport = "rest"
        entries = await client.get_logbook(
            start_time=start_time,
            end_time=datetime.now(UTC).isoformat(),
            context_id=str(context_id),
        )
    if not isinstance(entries, list):
        return TraceLogbook(
            [],
            transport,
            ["The logbook reply was not a row list."],
        )

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in entries:
        key = json.dumps(row, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    total = len(merged)
    capped = merged[:LOGBOOK_ROW_LIMIT]
    return TraceLogbook(
        capped,
        transport,
        [],
        total,
        total > LOGBOOK_ROW_LIMIT,
    )


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
