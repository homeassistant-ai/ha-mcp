"""The flow-type vocabulary, a menu step's choices and the shielded flow aborts."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any

import anyio

logger = logging.getLogger(__name__)

# How long cleanup waits for Home Assistant to abort a flow.
_ABORT_TIMEOUT_S = 5.0


def _flow_gone(err: BaseException) -> bool:
    """Whether ``err`` is Core's 404 for a flow that no longer exists: it
    already finished (abort, create_entry)."""
    return getattr(err, "status_code", None) == 404


async def shielded_abort(
    abort: Callable[[str], Awaitable[Any]],
    flow_id: str,
    *,
    failure: str,
    level: int = logging.WARNING,
) -> None:
    """Abort ``flow_id``.

    Shielded: a cancellation arrives through an anyio cancel scope, which
    would cancel the abort itself at its first await. A failure is logged as
    ``failure`` at ``level``, at debug when the flow is already gone.
    """
    try:
        with anyio.CancelScope(shield=True):
            await asyncio.wait_for(abort(flow_id), timeout=_ABORT_TIMEOUT_S)
    except Exception as abort_err:
        logger.log(
            logging.DEBUG if _flow_gone(abort_err) else level,
            "%s: %s",
            failure,
            abort_err,
            exc_info=True,
        )


async def abort_probe_flow(
    abort: Callable[[str], Awaitable[Any]], flow_id: str, *, quiet: bool = False
) -> None:
    """Abort a flow opened only to read it, shielded: a cancelled read still
    closes it. A failure logs at warning, at debug when ``quiet``."""
    await shielded_abort(
        abort,
        flow_id,
        failure=f"flow {flow_id} abort failed",
        level=logging.DEBUG if quiet else logging.WARNING,
    )


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


class _FlowType(StrEnum):
    """HA config flow result type strings."""

    FORM = "form"
    MENU = "menu"
    ABORT = "abort"
    CREATE_ENTRY = "create_entry"


def menu_choices(step: dict[str, Any]) -> list[str]:
    """A MENU step's branch names: a list of step ids, or the keys of a
    ``{step_id: label}`` dict (HA allows both); malformed ones drop."""
    options = step.get("menu_options")
    if isinstance(options, dict):
        options = list(options)
    if not isinstance(options, list):
        return []
    return [opt for opt in options if isinstance(opt, str)]
