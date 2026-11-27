"""Native logbook lookups for trace debugging."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from .component_api import component_unusable_warning, get_component_caps
from .log_sources_logbook import LogbookLogSourceMixin
from .logbook_cursor import fill_logbook_page

LOGBOOK_ROW_LIMIT = 50


@dataclass(frozen=True)
class TraceLogbook:
    """Logbook rows for one trace run."""

    entries: list[dict[str, Any]]
    transport: Literal["component", "rest"] | None
    warnings: list[str]
    total: int | None = 0
    truncated: bool = False


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
    command = {
        "start_time": start_time,
        "end_time": datetime.now(UTC).isoformat(),
        "context_id": str(context_id),
    }
    request = {"order": "oldest", "limit": LOGBOOK_ROW_LIMIT, "skip": 0}
    page = await reader._fetch_logbook_component(command, request)
    if isinstance(page, dict):
        result = await fill_logbook_page(
            reader._required_component_page,
            command,
            request,
            page,
            None,
            count_ahead=False,
        )
        return TraceLogbook(
            result["entries"],
            "component",
            [],
            result["total"],
            bool(result.get("after")),
        )
    rows = await client.get_logbook(**command)
    warnings = [
        component_unusable_warning(
            await get_component_caps(client), "the trace Logbook uses REST"
        )
    ]
    if not isinstance(rows, list):
        return TraceLogbook(
            [], "rest", [*warnings, "The logbook reply was not a row list."]
        )
    return TraceLogbook(
        rows[:LOGBOOK_ROW_LIMIT],
        "rest",
        warnings,
        len(rows),
        len(rows) > LOGBOOK_ROW_LIMIT,
    )
