"""Aborting a flow after a failed walk, without hiding the walk's error."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .config_entry_flow_introspect import flow_gone, shielded_abort
from .config_entry_flow_walker import flow_error_payload
from .helpers import raise_tool_error


async def abort_failed_flow(
    abort: Callable[[str], Awaitable[Any]], flow_id: str, *, kind: str = "flow"
) -> Exception | None:
    """Abort a still-pending flow without hiding the original failure; return
    the abort's own error, if any. ``kind`` names the flow in the log."""
    return await shielded_abort(
        abort, flow_id, failure=f"Failed to abort {kind} {flow_id} after error"
    )


def abort_left_flow_open(abort_err: Exception | None) -> bool:
    """Whether a failed abort may have left its flow open (a 404: it is gone)."""
    return abort_err is not None and not flow_gone(abort_err)


def raise_if_flow_left_open(
    flow_error: BaseException, abort_err: Exception | None, left_open: str
) -> None:
    """If the abort after ``flow_error`` is not confirmed, raise a structured
    ``flow_error`` again with ``left_open`` in its warnings."""
    payload = flow_error_payload(flow_error)
    if payload and abort_left_flow_open(abort_err):
        payload.setdefault("warnings", []).append(left_open)
        raise_tool_error(payload)

