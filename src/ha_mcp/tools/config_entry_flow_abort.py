"""Aborting a flow after a failed walk, without hiding the walk's error, and
closing a flow opened only to read it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from .config_entry_flow_introspect import abort_probe_flow, flow_gone, shielded_abort
from .config_entry_flow_walker import flow_error_payload
from .helpers import raise_tool_error


def flow_left_open(kind: str, flow_id: str) -> str:
    """The warning for a ``kind`` flow whose abort Home Assistant did not confirm."""
    unconfirmed = (
        f"Home Assistant did not confirm that the {kind} flow {flow_id} was closed."
    )
    if kind == "setup":
        return (
            f"{unconfirmed} While it stays open (until it ends or Home Assistant "
            "restarts), a new setup of the same device or account can abort "
            "with already_in_progress."
        )
    return f"{unconfirmed} It stays open until it ends or Home Assistant restarts."


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


def _raise_with_warning(flow_error: BaseException, warning: str) -> None:
    """Raise a structured ``flow_error`` again with ``warning`` in its warnings;
    do nothing for any other error."""
    if payload := flow_error_payload(flow_error):
        payload.setdefault("warnings", []).append(warning)
        raise_tool_error(payload)


def raise_if_flow_left_open(
    flow_error: BaseException, abort_err: Exception | None, left_open: str
) -> None:
    """If the abort after ``flow_error`` is not confirmed, raise a structured
    ``flow_error`` again with ``left_open`` in its warnings."""
    if abort_left_flow_open(abort_err):
        _raise_with_warning(flow_error, left_open)


@asynccontextmanager
async def closing_probe_flow(
    abort: Callable[[str], Awaitable[Any]],
    flow_id: str | None,
    left_open: str,
    resp: dict[str, Any],
) -> AsyncIterator[None]:
    """Abort the flow ``flow_id``, opened only to read it, when the block ends.

    If Home Assistant does not confirm the abort, ``left_open`` goes into
    ``resp``'s warnings and, when the block raised a structured error, into
    that error's warnings too.
    """
    try:
        yield
    except (Exception, asyncio.CancelledError) as flow_error:
        if flow_id and await abort_probe_flow(abort, flow_id):
            resp.setdefault("warnings", []).append(left_open)
            _raise_with_warning(flow_error, left_open)
        raise
    if flow_id and await abort_probe_flow(abort, flow_id):
        resp.setdefault("warnings", []).append(left_open)
