"""Core's install progress of a Marketplace repository, as MCP progress.

While a repository installs, the Marketplace dispatches
``{"repository": <full name>, "progress": 10..90 | false}`` on its
``marketplace_repository_install_progress`` signal, which
``marketplace/subscribe`` streams. The panel's install dialog subscribes before
it sends the install and shows the number; progress is a nicety there and
here: a subscription that cannot be opened, or a report that fails, leaves the
install alone.
"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from ha_mcp._vendor.fastmcp import Context

from .hacs_backend import LIST
from .helpers import safe_progress

logger = logging.getLogger(__name__)

_SIGNAL = "marketplace_repository_install_progress"
# Progress must not hold up the install it reports on.
_SUBSCRIBE_TIMEOUT = 5.0
# Reports still queued when the install answered, before they are dropped.
_DRAIN_TIMEOUT = 1.0
_END = object()


async def _subscribe(ws_client: Any) -> tuple[int, asyncio.Queue[Any]] | None:
    try:
        subscription: tuple[
            int, asyncio.Queue[Any]
        ] = await ws_client.subscribe_command(
            "marketplace/subscribe",
            signal=_SIGNAL,
            wait_timeout=_SUBSCRIBE_TIMEOUT,
        )
    except Exception as err:  # noqa: BLE001
        logger.warning("No install progress, the subscription failed: %r", err)
        return None
    return subscription


async def _full_name(ws_client: Any, repository_id: str) -> str | None:
    """The name Core's progress carries, for a repository given by id."""
    rows = (await ws_client.send_command(LIST)).get("result") or []
    return next(
        (str(r.get("full_name")) for r in rows if str(r.get("id")) == repository_id),
        None,
    )


async def _forward(
    ws_client: Any,
    ctx: Context,
    queue: asyncio.Queue[Any],
    repository_id: str,
    full_name: str | None,
) -> None:
    while (message := await queue.get()) is not _END:
        event = message.get("event") or {}
        progress = event.get("progress")
        if isinstance(progress, bool) or not isinstance(progress, int):
            continue
        if full_name is None:
            full_name = await _full_name(ws_client, repository_id)
            if full_name is None:
                logger.warning(
                    "No install progress: repository %s is not in the "
                    "Marketplace's list, which names what its progress is for",
                    repository_id,
                )
                return
        if event.get("repository") == full_name:
            await safe_progress(
                ctx, progress=progress, total=100, message=f"Installing {full_name}"
            )


@contextlib.asynccontextmanager
async def install_progress(
    ws_client: Any,
    ctx: Context | None,
    repository_id: str,
    full_name: str | None,
) -> AsyncIterator[None]:
    """Forward the repository's install progress while the block runs."""
    subscription = await _subscribe(ws_client) if ctx is not None else None
    if ctx is None or subscription is None:
        yield
        return
    sub_id, queue = subscription
    forwarder = asyncio.create_task(
        _forward(ws_client, ctx, queue, repository_id, full_name)
    )
    try:
        yield
    finally:
        try:
            # Core sends an install's last progress before its answer, so
            # what is queued now is all of it; releasing drops the queue.
            queue.put_nowait(_END)
            await asyncio.wait({forwarder}, timeout=_DRAIN_TIMEOUT)
            forwarder.cancel()
            [ended] = await asyncio.gather(forwarder, return_exceptions=True)
            # A cancel is the normal end; it is no Exception.
            if isinstance(ended, Exception):
                logger.warning("Install progress stopped: %r", ended)
        finally:
            await ws_client.release_subscription(sub_id)
