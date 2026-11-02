"""Aborting a flow after a failed walk, without hiding the walk's error."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import anyio

from .config_entry_flow_introspect import _ABORT_TIMEOUT_S
from .config_entry_flow_walker import flow_error_payload
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)


async def _abort_flow_best_effort(client: Any, flow_id: str) -> Exception | None:
    """Abort a still-pending flow without hiding the original failure; return
    the abort's own error, if any.

    Shielded: a cancellation arrives through an anyio cancel scope, which
    would cancel the abort itself at its first await.
    """
    try:
        with anyio.CancelScope(shield=True):
            await asyncio.wait_for(
                client.abort_config_flow(flow_id), timeout=_ABORT_TIMEOUT_S
            )
    except Exception as abort_err:  # noqa: BLE001
        logger.warning("Failed to abort flow %s after error: %s", flow_id, abort_err)
        return abort_err
    return None


def abort_left_flow_open(abort_err: Exception | None) -> bool:
    """Whether a failed abort may have left its flow open (a 404: it is gone)."""
    return abort_err is not None and getattr(abort_err, "status_code", None) != 404


async def _abort_after_flow_error(
    client: Any, flow_id: str, flow_error: BaseException, left_open: str
) -> None:
    """Abort ``flow_id`` after ``flow_error``; if the abort is not confirmed,
    raise a structured ``flow_error`` again with ``left_open`` in its warnings."""
    abort_err = await _abort_flow_best_effort(client, flow_id)
    payload = flow_error_payload(flow_error)
    if payload and abort_left_flow_open(abort_err):
        payload.setdefault("warnings", []).append(left_open)
        raise_tool_error(payload)


async def _abort_subentry_flow_best_effort(client: Any, flow_id: str) -> None:
    """Abort a pending subentry flow without hiding the original failure."""
    try:
        with anyio.CancelScope(shield=True):
            await asyncio.wait_for(
                client.abort_config_subentry_flow(flow_id), timeout=_ABORT_TIMEOUT_S
            )
    except Exception as abort_err:  # noqa: BLE001
        logger.warning(
            "Failed to abort config subentry flow %s after error: %s",
            flow_id,
            abort_err,
        )
