"""Unit tests for the config-entry reload-watch primitives.

Exercises `_entry_fragments`, `_is_transient_reconfigure_state`, and
`_observe_reload_settled` directly. The full `reconfigure_config_entry` path
that drives this module end-to-end is covered in
`test_integration_reconfigure.py`.
"""

import asyncio
from typing import Any

import pytest

from ha_mcp.tools.config_entry_reload_watch import (
    _TRANSIENT_RECONFIGURE_STATES,
    _entry_fragments,
    _is_transient_reconfigure_state,
    _observe_reload_settled,
)


def _frame(entry: dict[str, Any], *, change: str | None = "updated") -> dict[str, Any]:
    """One `config_entries/subscribe` frame; `change=None` models the snapshot."""
    return {"id": 1, "type": "event", "event": [{"type": change, "entry": entry}]}


def _queue_of(*frames: dict[str, Any]) -> "asyncio.Queue[dict[str, Any]]":
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    for frame in frames:
        queue.put_nowait(frame)
    return queue


# === _entry_fragments ===


def test_entry_fragments_yields_only_the_matching_entry_id() -> None:
    """A frame can carry dispatches for entries other than the one we watch."""
    entry_a = {"entry_id": "entry-a", "state": "loaded"}
    entry_b = {"entry_id": "entry-b", "state": "loaded"}
    message = {
        "event": [
            {"type": "updated", "entry": entry_a},
            {"type": "updated", "entry": entry_b},
        ]
    }

    assert list(_entry_fragments(message, "entry-a")) == [entry_a]


def test_entry_fragments_skips_non_dict_items_in_the_event_list() -> None:
    """A malformed dispatch item must be ignored, not raise."""
    message = {"event": ["not-a-dict", None, 42]}

    assert list(_entry_fragments(message, "entry-a")) == []


def test_entry_fragments_skips_items_whose_entry_is_not_a_dict() -> None:
    """Neither a non-dict `entry` value nor a missing one is a fragment."""
    message = {
        "event": [
            {"type": "updated", "entry": "entry-a"},
            {"type": "updated"},
        ]
    }

    assert list(_entry_fragments(message, "entry-a")) == []


@pytest.mark.parametrize(
    "message",
    [
        pytest.param({}, id="missing_event_key"),
        pytest.param({"event": None}, id="event_key_is_none"),
    ],
)
def test_entry_fragments_tolerates_a_frame_with_no_event_list(
    message: dict[str, Any],
) -> None:
    """`message.get("event") or []` must handle both absent and null events."""
    assert list(_entry_fragments(message, "entry-a")) == []


def test_entry_fragments_yields_the_matching_entry_from_a_snapshot_frame() -> None:
    """The subscribe answer's snapshot carries every current entry in one frame."""
    entry_a = {"entry_id": "entry-a", "state": "loaded"}
    entry_b = {"entry_id": "entry-b", "state": "not_loaded"}
    entry_c = {"entry_id": "entry-c", "state": "loaded"}
    message = {
        "type": None,
        "event": [
            {"type": None, "entry": entry_a},
            {"type": None, "entry": entry_b},
            {"type": None, "entry": entry_c},
        ],
    }

    assert list(_entry_fragments(message, "entry-b")) == [entry_b]


# === _is_transient_reconfigure_state ===


@pytest.mark.parametrize("state", sorted(_TRANSIENT_RECONFIGURE_STATES))
def test_is_transient_reconfigure_state_is_true_for_every_transient_state(
    state: str,
) -> None:
    assert _is_transient_reconfigure_state({"state": state}) is True


@pytest.mark.parametrize(
    "state", ["loaded", "setup_retry", "setup_error", "failed_unload"]
)
def test_is_transient_reconfigure_state_is_false_for_terminal_states(
    state: str,
) -> None:
    assert _is_transient_reconfigure_state({"state": state}) is False


@pytest.mark.parametrize("state", sorted(_TRANSIENT_RECONFIGURE_STATES))
def test_a_disabled_entry_is_never_transient_even_at_a_transient_state(
    state: str,
) -> None:
    """A disabled entry sits at `not_loaded` permanently, so it is terminal."""
    entry = {"state": state, "disabled_by": "user"}

    assert _is_transient_reconfigure_state(entry) is False


# === _observe_reload_settled ===


@pytest.mark.asyncio
async def test_the_first_fragment_only_sets_a_baseline_even_at_a_terminal_state() -> (
    None
):
    """Nothing predates the first fragment, so it can only become the baseline."""
    entry_id = "entry-1"
    queue = _queue_of(_frame({"entry_id": entry_id, "state": "loaded"}))

    result = await _observe_reload_settled(queue, entry_id, timeout=0.2)

    assert result is None


@pytest.mark.asyncio
async def test_a_same_state_fragment_after_the_baseline_cannot_settle() -> None:
    """The commit's UPDATED carries new data but the pre-reload state.

    Settling on it would report the pre-reload state as observed.
    """
    entry_id = "entry-1"
    queue = _queue_of(
        _frame({"entry_id": entry_id, "state": "loaded"}),
        _frame({"entry_id": entry_id, "state": "loaded", "data": {"host": "x"}}),
    )

    result = await _observe_reload_settled(queue, entry_id, timeout=0.2)

    assert result is None


@pytest.mark.asyncio
async def test_transient_transitions_are_consumed_until_the_first_non_transient_change() -> (
    None
):
    """Only the state that finally leaves the transient set is the outcome."""
    entry_id = "entry-1"
    final_fragment = {"entry_id": entry_id, "state": "loaded"}
    queue = _queue_of(
        _frame({"entry_id": entry_id, "state": "loaded"}),
        _frame({"entry_id": entry_id, "state": "unload_in_progress"}),
        _frame({"entry_id": entry_id, "state": "not_loaded"}),
        _frame({"entry_id": entry_id, "state": "setup_in_progress"}),
        _frame(final_fragment),
    )

    result = await _observe_reload_settled(queue, entry_id, timeout=0.2)

    assert result == final_fragment


@pytest.mark.asyncio
async def test_a_stream_that_never_transitions_expires_the_budget() -> None:
    entry_id = "entry-1"
    queue = _queue_of(
        _frame({"entry_id": entry_id, "state": "loaded"}),
        _frame({"entry_id": entry_id, "state": "loaded"}),
        _frame({"entry_id": entry_id, "state": "loaded"}),
    )

    result = await _observe_reload_settled(queue, entry_id, timeout=0.2)

    assert result is None


@pytest.mark.asyncio
async def test_a_disabled_first_fragment_returns_immediately_without_waiting() -> None:
    """A disabled entry is never reloaded, so no transition is ever coming."""
    entry_id = "entry-1"
    queue = _queue_of(
        _frame({"entry_id": entry_id, "state": "not_loaded", "disabled_by": "user"})
    )

    started = asyncio.get_running_loop().time()
    result = await _observe_reload_settled(queue, entry_id, timeout=30.0)
    elapsed = asyncio.get_running_loop().time() - started

    assert result is None
    assert elapsed < 1.0, f"waited {elapsed:.1f}s for a reload that never comes"


@pytest.mark.asyncio
async def test_fragments_for_a_different_entry_id_are_ignored() -> None:
    """A subscription frame is process-wide; another entry's transition is noise."""
    entry_id = "entry-1"
    final_fragment = {"entry_id": entry_id, "state": "loaded"}
    queue = _queue_of(
        _frame({"entry_id": entry_id, "state": "setup_retry"}),
        _frame({"entry_id": "entry-other", "state": "loaded"}),
        _frame({"entry_id": "entry-other", "state": "not_loaded"}),
        _frame(final_fragment),
    )

    result = await _observe_reload_settled(queue, entry_id, timeout=0.2)

    assert result == final_fragment
