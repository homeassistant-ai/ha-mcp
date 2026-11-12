"""``ha_mcp_tools/trace_context`` — match a context in Core's live trace store.

The trace store is a Core object held in ``hass.data``. Reading it in process
matches a run's own context or a triggering parent context across every stored
automation and script in one pass, instead of one public ``trace/get`` per
stored run. A run that owns the context is the run the caller asked about, so
it is selected before any run the context merely triggered; within that rank a
real run beats a not-triggered diagnostic, and the earliest start wins. The
returned ``trace`` is Core's ``as_extended_dict()`` used by the frontend's trace
view and Download action.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import datetime
from typing import Any

from homeassistant.components import websocket_api
from homeassistant.components.trace.const import DATA_TRACE, DATA_TRACE_STORE
from homeassistant.components.trace.util import async_restore_traces
from homeassistant.components.trace.websocket_api import TRACE_DOMAINS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.json import ExtendedJSONEncoder

from .seam import PreSerializedResult

WS_TRACE_CONTEXT = "ha_mcp_tools/trace_context"
CAPABILITIES = ("trace_context",)

_TRACE_RESTORE_LOCK = "ha_mcp_tools_trace_restore_lock"


async def _trace_context_prep(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Restore saved traces, wait out an in-flight restore, and encode off-loop."""
    if DATA_TRACE not in hass.data or DATA_TRACE_STORE not in hass.data:
        payload = await hass.async_add_executor_job(
            _encode_result, msg["id"], _empty_result(scanned=0, available=False)
        )
        return {"payload": payload}
    # Core sets DATA_TRACES_RESTORED before awaiting the store load, so a
    # second caller would see the marker and scan an empty store. The lock makes
    # it wait for a restore this command started; Core's own trace commands
    # restore outside it.
    lock = hass.data.setdefault(_TRACE_RESTORE_LOCK, asyncio.Lock())
    async with lock:
        await async_restore_traces(hass)
    result = _trace_context_payload(hass, msg)
    payload = await hass.async_add_executor_job(_encode_result, msg["id"], result)
    return {"payload": payload}


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
        if bucket_domain not in TRACE_DOMAINS:
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


def _selection_key(
    candidate: tuple[Any, str, str, str],
) -> tuple[int, int, str, int, str, str]:
    """Rank one candidate: own context, real run, earliest start, then ties.

    ``timestamp.start`` is an ISO string, so its natural order is chronological.
    Remaining ties break toward automations, then ascending ``item_id``, then
    ``run_id``, so every candidate has a distinct key.
    """
    trace, domain, item_id, match = candidate
    own_order = 0 if match == "trace" else 1
    real_order = 0 if _trace_is_real(trace) else 1
    domain_order = 0 if domain == "automation" else 1
    return (
        own_order,
        real_order,
        _trace_start(trace),
        domain_order,
        item_id,
        trace.run_id,
    )


def _trace_is_real(trace: Any) -> bool:
    """True for a real run; False for a not-triggered diagnostic trace."""
    return not getattr(trace, "not_triggered", False)


def _trace_context_payload(
    hass: HomeAssistant,
    msg: dict[str, Any],
) -> dict[str, Any]:
    """Find the trace whose own or parent context matches ``context_id``.

    One context id can match several runs (for example, an automation and the
    script it calls share ``context.id``/``parent_id``). All matches are
    collected. A run that owns the context is the one the caller asked about, so
    an own-context match wins over a run the context merely triggered. Within
    that rank a real run wins over a not-triggered diagnostic, then the earliest
    ``timestamp.start`` wins, then ties break toward automations, ascending
    ``item_id`` and ascending ``run_id``. The unselected matches are returned as
    ``other_matches``.
    """
    context_id = msg["context_id"]
    scanned = 0
    matches: list[tuple[Any, str, str, str]] = []

    for domain, item_id, bucket in _trace_buckets(
        hass, msg.get("domain"), msg.get("item_id")
    ):
        for trace in bucket.all_traces():
            scanned += 1
            context = trace.context
            if context.id == context_id:
                matches.append((trace, domain, item_id, "trace"))
                continue
            if context.parent_id == context_id:
                matches.append((trace, domain, item_id, "parent"))

    if not matches:
        return _empty_result(scanned=scanned)

    selected = min(matches, key=_selection_key)
    trace, domain, item_id, match = selected
    other_matches = [
        {"domain": d, "item_id": i, "run_id": t.run_id, "match": m}
        for t, d, i, m in matches
        if t is not trace
    ]
    result = _match_result(trace, domain, item_id, match, scanned)
    if other_matches:
        result["other_matches"] = other_matches
    return result


def _do_trace_context(
    hass: HomeAssistant,
    msg: dict[str, Any],
    *,
    payload: bytes,
) -> PreSerializedResult:
    """Return the selected trace already encoded by the prep."""
    return PreSerializedResult(payload=payload)


def _encode_result(msg_id: int, result: dict[str, Any]) -> bytes:
    """Encode one result message; runs in the executor."""
    message = websocket_api.messages.result_message(msg_id, result)
    return json.dumps(
        message,
        cls=ExtendedJSONEncoder,
        allow_nan=False,
    ).encode()


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
    }
