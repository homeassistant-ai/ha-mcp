"""Observing a config entry's post-reconfigure reload.

Home Assistant queues the reload with ``async_create_task`` and returns, so a
read-back can land either mid-reload or before it starts. Only a change stream
opened ahead of the commit can tell a finished reload from one that has not
begun; this module owns that stream and the rule for what counts as settled.
"""

import asyncio
import logging
from collections.abc import Iterator
from datetime import datetime
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
#: Budget for everything before the commit is visible — the baseline fragment
#: and then the ``modified_at`` bump. A resubmit of identical values never
#: bumps it, and stalling the full settle budget for that costs the caller time
#: the poll would have spent answering.
_COMMIT_VISIBLE_TIMEOUT = 5.0
WS_CONFIG_ENTRIES_SUBSCRIBE = "config_entries/subscribe"


def _modified_at(entry: dict[str, Any]) -> float | None:
    """Epoch seconds from a fragment's ``modified_at``, or None if unusable.

    ``as_json_fragment`` — what ``config_entries/subscribe`` pushes and what
    the REST read returns — emits ``modified_at.timestamp()``, a JSON number.
    Only ``as_dict``, the ``.storage`` serializer, writes an ISO string, which
    is why the storage fixtures carry one; the ``str`` branch is tolerance,
    not the shape this stream sends.
    """
    raw = entry.get("modified_at")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw).timestamp()
        except ValueError:
            return None
    return None


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


class _EntryFragmentReader:
    """Sequential reader over one entry's fragments from a subscription queue.

    Buffers within a frame: the subscribe snapshot arrives as a single frame
    carrying every entry, so a frame can hold more than one relevant fragment.
    """

    def __init__(self, queue: Any, entry_id: str) -> None:
        self._queue = queue
        self._entry_id = entry_id
        self._buffer: list[dict[str, Any]] = []

    async def next(self, deadline: float) -> dict[str, Any] | None:
        """Next fragment for this entry, or None once the deadline passes."""
        loop = asyncio.get_running_loop()
        while not self._buffer:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                message = await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except TimeoutError:
                return None
            self._buffer.extend(_entry_fragments(message, self._entry_id))
        return self._buffer.pop(0)


def _is_post_commit(entry: dict[str, Any], baseline: float) -> bool:
    """Whether this fragment was written after the reconfigure committed."""
    modified = _modified_at(entry)
    return modified is not None and modified > baseline


async def _observe_reload_settled(
    queue: Any, entry_id: str, *, timeout: float = _RELOAD_SETTLE_TIMEOUT
) -> dict[str, Any] | None:
    """Consume entry-change events until the POST-COMMIT reload settles.

    Returns the settled fragment, or ``None`` if nothing attributable arrived
    in budget, in which case the caller falls back to polling.

    The subscription opens before the flow, so the queue holds every fragment
    from the whole flow window — including transitions with nothing to do with
    this reconfigure. An entry in ``setup_retry``, the state a user
    reconfigures to fix, always has a retry pending, and one firing mid-flow is
    a real state change whose outcome is not the reconfigure's.

    ``modified_at`` separates them: ``_async_set_state`` never touches it,
    ``async_update_entry`` bumps it at the commit, and ``as_json_fragment``
    carries it. A fragment still at the baseline value predates the commit; the
    first larger value IS the commit, which carries the new data and the OLD
    state and so never settles either. Only after that does a state change mean
    the reload — and by then no foreign retry can interfere, because
    ``async_schedule_reload`` cancels the pending one before queueing it.

    Resubmitting identical values is the deliberate gap: ``async_update_entry``
    returns early on ``not changed``, so nothing bumps ``modified_at`` and
    nothing is attributable. Give up once ``_COMMIT_VISIBLE_TIMEOUT`` passes
    rather than stalling the whole settle budget for an answer the poll has.
    """
    loop = asyncio.get_running_loop()
    reader = _EntryFragmentReader(queue, entry_id)
    commit_deadline = loop.time() + min(_COMMIT_VISIBLE_TIMEOUT, timeout)
    settle_deadline = loop.time() + timeout

    first = await reader.next(commit_deadline)
    if first is None or first.get("disabled_by"):
        # A disabled entry is never reloaded: async_unload returns early at
        # not_loaded without setting state, and async_reload skips setup while
        # disabled_by is set. No transition is coming.
        return None
    baseline = _modified_at(first)
    if baseline is None:
        return None

    state_at_commit: str | None = None
    while True:
        entry = await reader.next(commit_deadline)
        if entry is None:
            return None
        if _is_post_commit(entry, baseline):
            state_at_commit = entry.get("state")
            break

    while True:
        entry = await reader.next(settle_deadline)
        if entry is None:
            return None
        state = entry.get("state")
        if state == state_at_commit:
            continue
        state_at_commit = state
        if _is_transient_reconfigure_state(entry):
            # The reload is under way; keep reading for its outcome.
            continue
        return entry
