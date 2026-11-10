"""``ha_mcp_tools/trace_context`` — match a context in Core's live trace store.

The trace store is a Core object held in ``hass.data``. Reading it in process
matches a run's own context or a triggering parent context across every stored
automation and script in one pass, instead of one public ``trace/get`` per
stored run. The returned ``trace`` is Core's ``as_extended_dict()`` used by the
frontend's trace view and Download action.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from homeassistant.components import websocket_api
from homeassistant.components.trace.const import DATA_TRACE, DATA_TRACE_STORE
from homeassistant.components.trace.util import async_restore_traces
from homeassistant.components.trace.websocket_api import (
    TRACE_DOMAINS as _TRACE_DOMAINS,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import ExtendedJSONEncoder

from .seam import PreSerializedResult

WS_TRACE_CONTEXT = "ha_mcp_tools/trace_context"
CAPABILITIES = ("trace_context",)


async def _trace_context_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Restore saved traces before the synchronous store read."""
    if DATA_TRACE not in hass.data or DATA_TRACE_STORE not in hass.data:
        return {"available": False}
    await async_restore_traces(hass)
    return {"available": True}


def _trace_buckets(
    hass: HomeAssistant,
    domain: str | None,
    item_id: str | None,
) -> Iterator[tuple[str, str, Any]]:
    """Yield the stored trace buckets matching the optional selectors."""
    for key, bucket in hass.data.get(DATA_TRACE, {}).items():
        if bucket is None:
            continue
        bucket_domain, _, bucket_item = key.partition(".")
        if bucket_domain not in _TRACE_DOMAINS:
            continue
        if domain and bucket_domain != domain:
            continue
        if item_id and bucket_item != item_id:
            continue
        yield bucket_domain, bucket_item, bucket


def _trace_start(trace: Any) -> str:
    """Return a sortable start timestamp from Core's short dict."""
    timestamp = trace.as_short_dict().get("timestamp") or {}
    start = timestamp.get("start")
    if isinstance(start, datetime):
        return start.isoformat()
    return "" if start is None else str(start)


def _trace_context_payload(
    hass: HomeAssistant,
    msg: dict[str, Any],
    *,
    available: bool,
) -> dict[str, Any]:
    """Find the trace whose own or parent context matches ``context_id``."""
    if not available:
        return _empty_result(scanned=0, available=False)

    context_id = msg["context_id"]
    scanned = 0
    parent_runs: list[tuple[Any, str, str]] = []
    parent_not_triggered: list[tuple[Any, str, str]] = []

    for domain, item_id, bucket in _trace_buckets(
        hass, msg.get("domain"), msg.get("item_id")
    ):
        for trace in bucket.all_traces():
            scanned += 1
            context = trace.context
            if context.id == context_id:
                return _match_result(trace, domain, item_id, "trace", scanned)
            if context.parent_id != context_id:
                continue
            candidate = (trace, domain, item_id)
            if trace.not_triggered:
                parent_not_triggered.append(candidate)
            else:
                parent_runs.append(candidate)

    if parent_runs:
        trace, domain, item_id = max(
            parent_runs, key=lambda item: _trace_start(item[0])
        )
        return _match_result(trace, domain, item_id, "parent", scanned)
    if parent_not_triggered:
        trace, domain, item_id = max(
            parent_not_triggered, key=lambda item: _trace_start(item[0])
        )
        return _match_result(trace, domain, item_id, "parent", scanned)
    return _empty_result(scanned=scanned)


def _do_trace_context(
    hass: HomeAssistant,
    msg: dict[str, Any],
    *,
    available: bool,
) -> PreSerializedResult:
    """Return the selected trace with Core's trace-view JSON encoding."""
    payload = _trace_context_payload(hass, msg, available=available)
    message = websocket_api.messages.result_message(msg["id"], payload)
    return PreSerializedResult(
        json.dumps(
            message,
            cls=ExtendedJSONEncoder,
            allow_nan=False,
        ).encode()
    )


def _match_result(
    trace: Any,
    domain: str,
    item_id: str,
    match: str,
    scanned: int,
) -> dict[str, Any]:
    """The wire result for one matched trace."""
    return {
        "match": match,
        "run_id": trace.run_id,
        "domain": domain,
        "item_id": item_id,
        "trace": trace.as_extended_dict(),
        "scanned": scanned,
        "truncated": False,
        "errors": [],
    }


def _empty_result(*, scanned: int, available: bool = True) -> dict[str, Any]:
    """The wire result when no stored trace matches."""
    return {
        "available": available,
        "match": None,
        "run_id": None,
        "domain": None,
        "item_id": None,
        "trace": None,
        "scanned": scanned,
        "truncated": False,
        "errors": [],
    }
