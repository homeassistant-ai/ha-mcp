"""Native context lookups for trace debugging."""

import logging
from dataclasses import dataclass
from typing import Any, Literal, NoReturn, cast

from ..client.rest_client import HomeAssistantConnectionError
from ..errors import ErrorCode, create_error_response
from .component_api import component_supports, get_component_caps, invalidate_caps
from .helpers import raise_tool_error
from .util_helpers import is_connection_error_message

logger = logging.getLogger(__name__)

TRACE_CONTEXT_COMMAND = "ha_mcp_tools/trace_context"
TRACE_CONTEXT_CAPABILITY = "trace_context"


@dataclass(frozen=True)
class ContextTraceMatch:
    """A trace selected by a context id.

    ``domain`` and ``item_id`` are Core's trace key for the selected run; both
    resolvers (the component and Core's ``trace/contexts``) report them with
    every match.
    """

    run_id: str
    match: Literal["trace", "parent"]
    scanned: int
    domain: str
    item_id: str
    trace_data: dict[str, Any] | None = None
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
    uses Core's own ``trace/contexts`` lookup. A reply with no match is
    authoritative: the component scanned every stored trace in process.
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
    """Parse one component trace-context reply; None means use Core's lookup."""
    if not isinstance(payload, dict):
        return None
    if payload.get("available") is False:
        # Core's trace store is not set up; Core's own lookup reports that.
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
            run_id=run_id,
            match=cast(Literal["trace", "parent"], kind),
            scanned=scanned_count,
            trace_data=trace_data,
            domain=item_domain,
            item_id=item_id_value,
            other_matches=other_matches,
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
    """Resolve a context id for one automation or script.

    The ha_mcp_tools component matches a run's own context and a triggering
    parent context across Core's live trace store. Without it, Core's
    ``trace/contexts`` command is the only source, and it holds each run's own
    ``context.id``. A context Core does not hold is reported as not found with
    a hint that the component resolves triggering (parent) contexts.
    """
    component = await _component_context(client, context_id, domain, item_id)
    if component is not None:
        if component.match is None:
            _raise_context_not_found(
                context_id,
                domain,
                item_id,
                component.scanned,
                component_checked=True,
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
    if not isinstance(entry, dict) or not entry.get("run_id"):
        _raise_context_not_found(context_id, domain, item_id, 0)
    return ContextTraceMatch(
        run_id=str(entry["run_id"]),
        match="trace",
        scanned=0,
        domain=domain,
        item_id=item_id,
    )


async def resolve_global_context(
    client: Any,
    context_id: str,
    domain: Literal["automation", "script"] | None = None,
) -> ContextTraceMatch:
    """Resolve a context id across every stored run.

    The component matches own and parent contexts in Core's live store. Without
    it, Core's ``trace/contexts`` map holds each run's own ``context.id``; a
    parent context is reported as not found with a hint that the component
    resolves it.
    """
    component = await _component_context(client, context_id, domain)
    if component is not None:
        if component.match is None:
            _raise_global_context_not_found(
                context_id,
                component.scanned,
                domain,
                component_checked=True,
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
    if (
        not isinstance(entry, dict)
        or not entry.get("run_id")
        or not isinstance(entry.get("domain"), str)
        or not isinstance(entry.get("item_id"), str)
    ):
        _raise_global_context_not_found(context_id, 0, domain)
    run_id = str(entry["run_id"])
    entry_domain = entry["domain"]
    entry_item_id = entry["item_id"]
    if domain is not None and entry_domain != domain:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"The context belongs to a {entry_domain} run, not a {domain} run",
                context={"context_id": context_id, "domain": domain},
                suggestions=[
                    "Omit domain to resolve the context across both domains",
                    "Pass the matching domain for this context",
                ],
            )
        )
    return ContextTraceMatch(
        run_id=str(run_id),
        match="trace",
        scanned=0,
        domain=entry_domain,
        item_id=entry_item_id,
    )


def _raise_global_context_not_found(
    context_id: str,
    scanned: int,
    domain: Literal["automation", "script"] | None = None,
    *,
    component_checked: bool = False,
) -> NoReturn:
    """Raise the structured not-found for an unresolvable global context id."""
    if component_checked:
        checked_scope = "the run's own and triggering (parent) contexts"
        suggestions = [
            f"The ha_mcp_tools component checked {checked_scope} in its trace store and found no match",
            "Verify the context id is the one you copied from the run",
        ]
    else:
        suggestions = [
            "Install the ha_mcp_tools component to resolve triggering (parent) contexts",
            "Pass the trace's own `context.id` to resolve a run without the component",
        ]
    if domain is not None:
        suggestions.append("Omit domain to resolve the context across both domains")
    suggestions.append("Use ha_get_automation_traces(domain=...) to list recent runs")
    raise_tool_error(
        create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"No trace found for context: {context_id}",
            context={"context_id": context_id, "scanned": scanned},
            suggestions=suggestions,
        )
    )


def _raise_context_not_found(
    context_id: str,
    domain: str,
    item_id: str,
    scanned: int,
    *,
    component_checked: bool = False,
) -> NoReturn:
    """Raise the structured not-found for an unresolvable context id."""
    if component_checked:
        checked_scope = "the run's own and triggering (parent) contexts"
        suggestions = [
            f"The ha_mcp_tools component checked {checked_scope} under those selectors and found no match",
            "Verify the context id is the one you copied from the run",
            "Use ha_get_automation_traces(entity_id=...) without context_id to list recent traces",
        ]
    else:
        suggestions = [
            "Install the ha_mcp_tools component to resolve triggering (parent) contexts",
            "Pass the trace's own `context.id` to resolve a run without the component",
            "Use ha_get_automation_traces(entity_id=...) without context_id to list recent traces",
        ]
    raise_tool_error(
        create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"No trace found for context: {context_id}",
            context={
                "context_id": context_id,
                "domain": domain,
                "item_id": item_id,
                "scanned": scanned,
            },
            suggestions=suggestions,
        )
    )


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
