"""Native logbook lookups for trace debugging."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from .log_sources_logbook import LogbookLogSourceMixin

logger = logging.getLogger(__name__)

LOGBOOK_ROW_LIMIT = 50


@dataclass(frozen=True)
class TraceLogbook:
    """Logbook rows for one trace run."""

    entries: list[dict[str, Any]]
    transport: Literal["component", "rest"] | None
    warnings: list[str]
    total: int = 0
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

    total = len(entries)
    capped = entries[:LOGBOOK_ROW_LIMIT]
    return TraceLogbook(
        capped,
        transport,
        [],
        total,
        total > LOGBOOK_ROW_LIMIT,
    )
