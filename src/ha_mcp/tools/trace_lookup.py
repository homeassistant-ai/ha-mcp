"""Native context lookups for trace debugging."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, NoReturn, cast

from ..client.rest_client import HomeAssistantConnectionError, HomeAssistantError
from ..errors import ErrorCode, create_error_response
from .component_api import component_supports, get_component_caps, invalidate_caps
from .helpers import raise_tool_error
from .trace_logbook import (  # noqa: F401
    LOGBOOK_ROW_LIMIT,
    TraceLogbook,
    fetch_trace_logbook,
)
from .util_helpers import is_connection_error_message

logger = logging.getLogger(__name__)

PARENT_CONTEXT_SCAN_LIMIT = 50
TRACE_CONTEXT_COMMAND = "ha_mcp_tools/trace_context"
TRACE_CONTEXT_CAPABILITY = "trace_context"
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
    fallback_scan: bool = False


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
        # Core's trace store is not set up; keep the public fallback available.
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
    The public fallback aggregates the direct map entry with a bounded scan of
    the item's stored traces so that one context id that matches several runs
    returns the same selected run and ``other_matches`` as the component path.
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

    scan_errors: list[str] = []
    if run_id:
        try:
            (
                scan_matches,
                scanned,
                truncated,
                scan_errors,
            ) = await _collect_parent_context_matches(
                client, domain, item_id, context_id
            )
        except Exception as err:  # noqa: BLE001
            scan_errors.append(f"parent context scan failed: {err}")
            scan_matches, scanned, truncated = [], 0, False
        return await _resolve_with_direct_match(
            client,
            str(run_id),
            domain,
            item_id,
            scan_matches,
            scanned,
            truncated,
            scan_errors,
        )

    (
        scan_matches,
        scanned,
        truncated,
        scan_errors,
    ) = await _collect_parent_context_matches(client, domain, item_id, context_id)
    selected = _select_trace_match(scan_matches, scanned, truncated, scan_errors)
    if selected is None:
        _raise_context_not_found(
            context_id, domain, item_id, scanned, truncated, scan_errors
        )
    return selected


async def resolve_global_context(
    client: Any,
    context_id: str,
    domain: Literal["automation", "script"] | None = None,
) -> ContextTraceMatch:
    """Resolve a context against Core's global trace/contexts map.

    Core accepts ``trace/contexts`` without domain/item and returns every
    stored run's own context. The public fallback aggregates the direct map
    entry with a bounded ``trace/list`` + ``trace/get`` scan so that one
    context id that matches several runs returns the same selected run and
    ``other_matches`` as the component path.
    """
    component = await _component_context(client, context_id, domain)
    if component is not None:
        if component.match is None:
            _raise_global_context_not_found(
                context_id, component.scanned, False, [], domain
            )
        return component.match

    result = await client.send_websocket_message({"type": "trace/contexts"})
    if not result.get("success"):
        _raise_trace_ws_failure(
            result.get("error", "Failed to resolve trace context"),
            {"context_id": context_id},
        )
    contexts = result.get("result")
    entry = contexts.get(context_id) if isinstance(contexts, dict) else None

    direct_match = await _global_direct_match(context_id, entry, domain)

    scan_errors: list[str] = []
    if direct_match is not None:
        try:
            (
                scan_matches,
                scanned,
                truncated,
                scan_errors,
                _rows,
            ) = await _collect_global_parent_matches(client, context_id, domain)
        except Exception as err:  # noqa: BLE001
            scan_errors.append(f"global context scan failed: {err}")
            scan_matches, scanned, truncated = [], 0, False
        run_id, _trace_data, entry_domain, entry_item_id, _kind, _not_triggered = (
            direct_match
        )
        return await _resolve_with_direct_match(
            client,
            run_id,
            entry_domain,
            entry_item_id,
            scan_matches,
            scanned,
            truncated,
            scan_errors,
        )

    (
        scan_matches,
        scanned,
        truncated,
        scan_errors,
        rows,
    ) = await _collect_global_parent_matches(client, context_id, domain)
    selected = _select_trace_match(scan_matches, scanned, truncated, scan_errors)
    if selected is None:
        if (
            not rows
            and scan_errors
            and all(is_connection_error_message(error) for error in scan_errors)
        ):
            _raise_trace_ws_failure("; ".join(scan_errors), {"context_id": context_id})
        _raise_global_context_not_found(
            context_id, scanned, truncated, scan_errors, domain
        )
    return selected


async def _global_direct_match(
    context_id: str,
    entry: Any,
    requested_domain: Literal["automation", "script"] | None,
) -> tuple[str, dict[str, Any] | None, str | None, str | None, str, bool] | None:
    """Return the direct-map identity for a global context.

    The actual trace data is read later by ``_resolve_with_direct_match`` so
    that a scan failure can still return the direct hit without duplicating
    the read.
    """
    if not isinstance(entry, dict) or not entry.get("run_id"):
        return None
    entry_domain = entry.get("domain")
    entry_item_id = entry.get("item_id")
    if requested_domain is not None and entry_domain != requested_domain:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"The context belongs to a {entry_domain or 'different'} run, "
                f"not a {requested_domain} run",
                context={"context_id": context_id, "domain": requested_domain},
                suggestions=[
                    "Omit domain to resolve the context across both domains",
                    "Pass the matching domain for this context",
                ],
            )
        )
    return (
        str(entry["run_id"]),
        None,
        str(entry_domain) if entry_domain else None,
        str(entry_item_id) if entry_item_id else None,
        "trace",
        False,
    )


async def _resolve_with_direct_match(
    client: Any,
    direct_run_id: str,
    direct_domain: str | None,
    direct_item_id: str | None,
    scan_matches: list[tuple[str, dict[str, Any] | None, str, str, str, bool]],
    scanned: int,
    truncated: bool,
    scan_errors: list[str],
) -> ContextTraceMatch:
    """Resolve when the direct map has a hit.

    If the direct trace data can be read, include it in the normal selection.
    If the direct trace data is unavailable, the direct match is kept selected,
    scanned matches are listed as others, and the read error is reported.
    """
    direct_trace_data: dict[str, Any] | None = None
    read_error: str | None = None
    if direct_domain and direct_item_id:
        try:
            direct_trace_data = await _read_candidate_context(
                client,
                direct_domain,
                direct_item_id,
                direct_run_id,
                scan_errors,
                have_match=bool(scan_matches),
            )
        except Exception as err:  # noqa: BLE001
            read_error = f"could not read direct trace {direct_run_id}: {err}"
            direct_trace_data = None
        if direct_trace_data is _SCAN_ABORTED:
            read_error = f"direct trace {direct_run_id} read aborted"
            direct_trace_data = None

    if direct_trace_data is not None:
        direct_match: tuple[
            str, dict[str, Any] | None, str | None, str | None, str, bool
        ] = (
            direct_run_id,
            direct_trace_data,
            direct_domain,
            direct_item_id,
            "trace",
            bool(direct_trace_data.get("not_triggered")),
        )
        all_matches = _deduplicated_matches(scan_matches, direct_match)
        selected = _select_trace_match(all_matches, scanned, truncated, scan_errors)
        if selected is not None:
            return selected
        read_error = "direct match lost during selection"

    final_errors = list(scan_errors)
    if read_error:
        final_errors.append(read_error)
    # When the direct trace data is unavailable the direct run is kept selected.
    # Do not also list it as an other match if the scan happened to include it.
    other_matches = tuple(
        {"domain": str(d), "item_id": str(i), "run_id": r, "match": k}
        for r, _td, d, i, k, _nt in scan_matches
        if d is not None and i is not None and r != direct_run_id
    )
    return ContextTraceMatch(
        direct_run_id,
        "trace",
        scanned,
        truncated,
        tuple(final_errors),
        direct_trace_data,
        direct_domain,
        direct_item_id,
        other_matches,
        fallback_scan=True,
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
    scan_errors: list[str],
) -> tuple[list[tuple[str, dict[str, Any] | None, str, str, str, bool]], int]:
    """Collect all matching stored traces across the bounded global rows."""
    scanned = 0
    matches: list[tuple[str, dict[str, Any] | None, str, str, str, bool]] = []
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
            matches.append(
                (
                    run_id,
                    trace_data,
                    domain,
                    item_id,
                    kind,
                    bool(trace.get("not_triggered")),
                )
            )

    return matches, scanned


async def _resolve_global_parent_context(
    client: Any,
    context_id: str,
    domain: Literal["automation", "script"] | None = None,
) -> ContextTraceMatch:
    """Scan stored runs for a parent context, bounded like the per-item scan."""
    (
        matches,
        scanned,
        truncated,
        scan_errors,
        rows,
    ) = await _collect_global_parent_matches(client, context_id, domain)
    match = _select_trace_match(matches, scanned, truncated, scan_errors)
    if match is None:
        if (
            not rows
            and scan_errors
            and all(is_connection_error_message(error) for error in scan_errors)
        ):
            _raise_trace_ws_failure("; ".join(scan_errors), {"context_id": context_id})
        _raise_global_context_not_found(
            context_id, scanned, truncated, scan_errors, domain
        )
    return match


async def _collect_global_parent_matches(
    client: Any,
    context_id: str,
    domain: Literal["automation", "script"] | None = None,
) -> tuple[
    list[tuple[str, dict[str, Any] | None, str, str, str, bool]],
    int,
    bool,
    list[str],
    list[dict[str, Any]],
]:
    """Return all global matches without selecting or raising."""
    rows, scan_errors = await _global_trace_rows(client, domain)
    rows.sort(key=_trace_start, reverse=True)
    truncated = len(rows) > PARENT_CONTEXT_SCAN_LIMIT
    matches, scanned = await _scan_global_parent(
        client,
        context_id,
        rows[:PARENT_CONTEXT_SCAN_LIMIT],
        scan_errors,
    )
    return matches, scanned, truncated, scan_errors, rows


def _raise_global_context_not_found(
    context_id: str,
    scanned: int,
    truncated: bool,
    scan_errors: list[str],
    domain: Literal["automation", "script"] | None = None,
) -> NoReturn:
    """Raise a not-found that keeps the global scan's bound and errors."""
    suggestions = [
        "Use ha_get_automation_traces(domain=...) to list recent runs",
    ]
    if domain is not None:
        suggestions.insert(
            0,
            "Omit domain to scan both automations and scripts",
        )
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
    matches, scanned, truncated, scan_errors = await _collect_parent_context_matches(
        client, domain, item_id, context_id
    )
    match = _select_trace_match(matches, scanned, truncated, scan_errors)
    if match is not None:
        return match

    _raise_context_not_found(
        context_id, domain, item_id, scanned, truncated, scan_errors
    )
    raise AssertionError("unreachable")


async def _collect_parent_context_matches(
    client: Any,
    domain: str,
    item_id: str,
    context_id: str,
) -> tuple[
    list[tuple[str, dict[str, Any] | None, str, str, str, bool]], int, bool, list[str]
]:
    """Return all per-item matches without selecting or raising."""
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

    matches, scanned = await _scan_for_context(
        client, domain, item_id, context_id, candidates, scan_errors
    )
    return matches, scanned, truncated, scan_errors


async def _scan_for_context(
    client: Any,
    domain: str,
    item_id: str,
    context_id: str,
    candidates: list[dict[str, Any]],
    scan_errors: list[str],
) -> tuple[list[tuple[str, dict[str, Any] | None, str, str, str, bool]], int]:
    """Collect all matching stored traces for the requested context id."""
    scanned = 0
    matches: list[tuple[str, dict[str, Any] | None, str, str, str, bool]] = []
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
            matches.append(
                (
                    run_id,
                    trace_data,
                    domain,
                    item_id,
                    kind,
                    bool(trace.get("not_triggered")),
                )
            )

    return matches, scanned


def _deduplicated_matches(
    scan_matches: list[tuple[str, dict[str, Any] | None, str, str, str, bool]],
    direct_match: tuple[str, dict[str, Any] | None, str | None, str | None, str, bool]
    | None,
) -> list[tuple[str, dict[str, Any] | None, str | None, str | None, str, bool]]:
    """Combine scan matches with a direct map entry, keeping scanned trace data."""
    matches: list[
        tuple[str, dict[str, Any] | None, str | None, str | None, str, bool]
    ] = list(scan_matches)
    if direct_match is None:
        return matches
    run_id, trace_data, domain, item_id, kind, not_triggered = direct_match
    key = (run_id, domain, item_id)
    if not any((r, d, i) == key for r, _td, d, i, _k, _nt in matches):
        matches.append((run_id, trace_data, domain, item_id, kind, not_triggered))
    return matches


async def _fetch_selected_trace_data(
    client: Any,
    selected: ContextTraceMatch,
    scan_errors: list[str],
    *,
    have_others: bool,
) -> ContextTraceMatch:
    """Fetch trace data for a match that came from the direct map."""
    domain = selected.domain
    item_id = selected.item_id
    run_id = selected.run_id
    if domain is None or item_id is None or not run_id:
        _raise_trace_ws_failure(
            f"Selected trace has missing domain/item_id ({run_id})",
            {"domain": domain, "item_id": item_id, "run_id": run_id},
        )
    trace_data = await _read_candidate_context(
        client,
        domain,
        item_id,
        run_id,
        scan_errors,
        have_match=have_others,
    )
    if trace_data is _SCAN_ABORTED or trace_data is None:
        _raise_trace_ws_failure(
            f"Could not read the selected trace ({run_id})",
            {"domain": domain, "item_id": item_id, "run_id": run_id},
        )
    return ContextTraceMatch(
        run_id,
        selected.match,
        selected.scanned,
        selected.truncated,
        tuple(scan_errors),
        trace_data,
        domain,
        item_id,
        selected.other_matches,
    )


def _select_trace_match(
    matches: Sequence[
        tuple[str, dict[str, Any] | None, str | None, str | None, str, bool]
    ],
    scanned: int,
    truncated: bool,
    scan_errors: list[str],
) -> ContextTraceMatch | None:
    """Select the newest match and list the rest with the component rule.

    A real run is preferred over a not-triggered diagnostic. Among traces of
    the same kind, the newest ``timestamp.start`` wins, with ties broken toward
    own-context matches, then automations, then ascending ``item_id``.
    """
    if not matches:
        return None

    def _start_key(
        match: tuple[str, dict[str, Any] | None, str | None, str | None, str, bool],
    ) -> str:
        _run_id, trace_data, _domain, _item_id, _kind, _not_triggered = match
        return _trace_start_only(trace_data)

    def _tie_break_key(
        match: tuple[str, dict[str, Any] | None, str | None, str | None, str, bool],
    ) -> tuple[int, int, str | None]:
        _run_id, _trace_data, domain, item_id, kind, _not_triggered = match
        match_order = 0 if kind == "trace" else 1
        domain_order = 0 if domain == "automation" else 1
        return (match_order, domain_order, item_id)

    real_matches = [m for m in matches if not m[5]]
    pool = real_matches if real_matches else matches
    newest_start = max(_start_key(m) for m in pool)
    candidates = [m for m in pool if _start_key(m) == newest_start]
    selected = min(candidates, key=_tie_break_key)
    run_id, trace_data, domain, item_id, kind, _not_triggered = selected
    other_matches = tuple(
        {
            "domain": str(d),
            "item_id": str(i),
            "run_id": r,
            "match": k,
        }
        for r, _td, d, i, k, _nt in matches
        if (r != run_id or d != domain or i != item_id)
        and d is not None
        and i is not None
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
        fallback_scan=True,
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


def _trace_start_only(trace: dict[str, Any] | None) -> str:
    """Return Core's ``timestamp.start`` only, matching the component rule."""
    if not isinstance(trace, dict):
        return ""
    timestamp = trace.get("timestamp")
    if isinstance(timestamp, dict):
        start = timestamp.get("start")
        if isinstance(start, str):
            return start
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
