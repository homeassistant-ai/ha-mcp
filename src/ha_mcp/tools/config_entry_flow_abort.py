"""Aborting a flow after a failed walk, without hiding the walk's error, and
closing a flow opened only to read it."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import anyio

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from .config_entry_flow_introspect import (
    _ABORT_TIMEOUT_S,
    abort_probe_flow,
    flow_gone,
    shielded_abort,
)
from .config_entry_flow_walker import flow_error_payload
from .helpers import exception_to_structured_error, raise_tool_error

logger = logging.getLogger(__name__)


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


async def abort_failed_options_flow(
    client: Any, flow_id: str, *, entry_id: str, reason: object
) -> Exception | None:
    """Abort an options flow after a failed walk; return the abort's error.

    The log names the error's type only, never its text.
    """
    try:
        with anyio.CancelScope(shield=True):  # see shielded_abort
            await asyncio.wait_for(
                client.abort_options_flow(flow_id), timeout=_ABORT_TIMEOUT_S
            )
    except Exception as abort_err:  # noqa: BLE001
        logger.warning(
            "Failed to abort options flow %s for entry %s "
            "(stage=abort_cleanup, reason=%s, error_type=%s)",
            flow_id,
            entry_id,
            reason,
            type(abort_err).__name__,
        )
        return abort_err
    return None


def abort_left_flow_open(abort_err: Exception | None) -> bool:
    """Whether a failed abort may have left its flow open (a 404: it is gone)."""
    return abort_err is not None and not flow_gone(abort_err)


def _raise_with_warning(flow_error: BaseException, warning: str) -> None:
    """Raise ``flow_error`` again as a structured error with ``warning`` in its
    warnings. A raw error (a lost connection, an HTTP error) becomes the error
    the tool's own handler would build from it; a cancellation passes."""
    if isinstance(flow_error, ToolError):
        payload = flow_error_payload(flow_error)
    elif isinstance(flow_error, Exception):
        payload = exception_to_structured_error(flow_error, raise_error=False)
    else:
        return
    if payload:
        payload.setdefault("warnings", []).append(warning)
        raise_tool_error(payload)


def raise_if_flow_left_open(
    flow_error: BaseException, abort_err: Exception | None, left_open: str
) -> None:
    """If the abort after ``flow_error`` is not confirmed, raise ``flow_error``
    again as a structured error with ``left_open`` in its warnings."""
    if abort_left_flow_open(abort_err):
        _raise_with_warning(flow_error, left_open)


@asynccontextmanager
async def closing_probe_flow(
    abort: Callable[[str], Awaitable[Any]],
    flow_id: str | None,
    left_open: str,
    resp: dict[str, Any],
    *,
    keep_raw_errors: bool = False,
) -> AsyncIterator[None]:
    """Abort the flow ``flow_id``, opened only to read it, when the block ends.

    If Home Assistant does not confirm the abort, ``left_open`` goes into
    ``resp``'s warnings and, when the block raised, into that error's warnings
    too: a raw error is raised again as a structured one. ``keep_raw_errors``
    lets a raw error through unchanged, for a caller that turns it into a
    warning in ``resp``.
    """
    try:
        yield
    except (Exception, asyncio.CancelledError) as flow_error:
        if flow_id and await abort_probe_flow(abort, flow_id):
            resp.setdefault("warnings", []).append(left_open)
            if not keep_raw_errors or isinstance(flow_error, ToolError):
                _raise_with_warning(flow_error, left_open)
        raise
    if flow_id and await abort_probe_flow(abort, flow_id):
        resp.setdefault("warnings", []).append(left_open)
