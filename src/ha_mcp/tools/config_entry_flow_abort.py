"""Aborting a flow after a failed walk, without hiding the walk's error."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import anyio

from .config_entry_flow_introspect import (
    _ABORT_TIMEOUT_S,
    _flow_gone,
    shielded_abort,
)

logger = logging.getLogger(__name__)


async def abort_failed_flow(
    abort: Callable[[str], Awaitable[Any]], flow_id: str, *, kind: str = "flow"
) -> None:
    """Abort a still-pending flow without hiding the original failure.
    ``kind`` names the flow in the log."""
    await shielded_abort(
        abort, flow_id, failure=f"Failed to abort {kind} {flow_id} after error"
    )


async def abort_failed_flow_type_only(
    abort: Callable[[str], Awaitable[Any]], flow_id: str, msg: str, *args: object
) -> None:
    """Abort a flow after a failed write.

    A failure logs ``msg % (*args, error_type)`` at warning, at debug when the
    flow is already gone: the error's type only, never its text, which may
    echo the values the flow carried.
    """
    try:
        with anyio.CancelScope(shield=True):  # see shielded_abort
            await asyncio.wait_for(abort(flow_id), timeout=_ABORT_TIMEOUT_S)
    except Exception as abort_err:  # noqa: BLE001
        logger.log(
            logging.DEBUG if _flow_gone(abort_err) else logging.WARNING,
            msg,
            *args,
            type(abort_err).__name__,
        )
