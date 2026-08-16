"""Observing a config entry's post-reconfigure reload.

Home Assistant queues the reload with ``async_create_task`` and returns, so a
read-back can land either mid-reload or before it starts. Only a change stream
opened ahead of the commit can tell a finished reload from one that has not
begun; this module owns that stream and the rule for what counts as settled.
"""

import asyncio
import logging
from collections.abc import Iterator
from typing import Any

from ..client.websocket_client import get_websocket_client

logger = logging.getLogger(__name__)

# States HA passes THROUGH while reloading. ``unload_in_progress`` is the first
# one an enabled domain entry enters and exists only from HA 2025.x, so
# treating it as terminal reports a good reload as unverified on current cores.
_TRANSIENT_RECONFIGURE_STATES = frozenset(
    {"not_loaded", "setup_in_progress", "unload_in_progress"}
)

#: Wall-clock budget for observing the post-commit reload settle.
_RELOAD_SETTLE_TIMEOUT = 20.0
_ENTRY_SUBSCRIBE_TIMEOUT = 10.0
WS_CONFIG_ENTRIES_SUBSCRIBE = "config_entries/subscribe"


def _is_transient_reconfigure_state(entry: dict[str, Any]) -> bool:
    """Return whether HA is still transitioning the entry after a flow.

    A disabled entry sits at ``not_loaded`` permanently, so that one is
    terminal rather than transient.
    """
    if entry.get("disabled_by"):
        return False
    return entry.get("state") in _TRANSIENT_RECONFIGURE_STATES


async def _subscribe_entry_changes(client: Any) -> tuple[Any, Any] | None:
    """Open a config-entry change stream, or ``None`` if unavailable.

    Home Assistant dispatches ``SIGNAL_CONFIG_ENTRY_CHANGED`` from
    ``ConfigEntry._async_set_state``, so ``config_entries/subscribe`` pushes
    EVERY state transition with the full entry fragment. Subscribing before
    the flow starts is what makes the post-commit reload observable instead of
    sampled: the queue is registered before the frame is sent, so no
    transition between the commit and our first read can be missed.
    """
    try:
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        sub_id, queue = await ws.subscribe_command(
            WS_CONFIG_ENTRIES_SUBSCRIBE, timeout=_ENTRY_SUBSCRIBE_TIMEOUT
        )
    except Exception as exc:
        # Degrade to polling rather than failing the reconfigure; the caller
        # records which mechanism actually ran.
        logger.warning(
            "%s unavailable (%r); falling back to polled verification",
            WS_CONFIG_ENTRIES_SUBSCRIBE,
            exc,
        )
        return None
    return ws, (sub_id, queue)


def _entry_fragments(message: Any, entry_id: str) -> Iterator[dict[str, Any]]:
    """Yield this entry's fragments from one subscription frame.

    A frame carries a list: one item per dispatch, or every current entry for
    the snapshot the subscribe answers with.
    """
    for item in message.get("event") or []:
        if not isinstance(item, dict):
            continue
        entry = item.get("entry")
        if isinstance(entry, dict) and entry.get("entry_id") == entry_id:
            yield entry


async def _observe_reload_settled(
    queue: Any, entry_id: str, *, timeout: float = _RELOAD_SETTLE_TIMEOUT
) -> dict[str, Any] | None:
    """Consume entry-change events until this entry reaches a settled state.

    Returns the settled fragment, or ``None`` if the budget expired without
    one, in which case the caller falls back to polling.

    Only a state CHANGE may settle this. Two fragments predate the reload and
    both carry the pre-reload state: the snapshot ``config_entries/subscribe``
    answers with, and the ``UPDATED`` dispatched when the values are committed
    (before ``async_schedule_reload`` runs). Settling on either would report
    the pre-reload state as observed. Neither ``type`` nor ``modified_at`` can
    order them — every state change dispatches the same ``UPDATED``, and the
    commit bumps ``modified_at`` — so the first fragment sets a baseline and
    only a departure from it counts.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    baseline: str | None = None
    have_baseline = False
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return None
        try:
            message = await asyncio.wait_for(queue.get(), timeout=remaining)
        except TimeoutError:
            return None
        for entry in _entry_fragments(message, entry_id):
            state = entry.get("state")
            if not have_baseline:
                have_baseline = True
                baseline = state
                if entry.get("disabled_by"):
                    # A disabled entry is never reloaded: async_unload returns
                    # early at not_loaded without setting state, and
                    # async_reload skips setup while disabled_by is set. No
                    # transition is coming, so stop now rather than burn the
                    # whole settle budget before falling back to polling.
                    return None
                continue
            if state == baseline:
                # Same state, new payload — the commit fragment, not the
                # reload's outcome.
                continue
            baseline = state
            if _is_transient_reconfigure_state(entry):
                # The reload is under way; keep reading for its outcome.
                continue
            return entry
