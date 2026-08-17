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

#: Timestamps bracketing a commit: the subscribe snapshot's baseline value,
#: and the value `async_update_entry` bumps `modified_at` to.
_BEFORE = 1786953600.0
_AFTER = 1786953605.0


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
async def test_a_baseline_with_nothing_after_it_never_commits_and_times_out() -> None:
    """Nothing follows the baseline within budget, so it can never commit."""
    entry_id = "entry-1"
    queue = _queue_of(
        _frame({"entry_id": entry_id, "state": "loaded", "modified_at": _BEFORE})
    )

    result = await _observe_reload_settled(queue, entry_id, timeout=0.2)

    assert result is None


@pytest.mark.asyncio
async def test_a_pre_commit_state_change_cannot_settle_even_to_a_terminal_state() -> (
    None
):
    """A foreign transition sharing the baseline's `modified_at` predates the commit.

    `setup_retry` always has a pending retry; one firing mid-flow must not be
    mistaken for the reconfigure's own outcome.
    """
    entry_id = "entry-1"
    queue = _queue_of(
        _frame({"entry_id": entry_id, "state": "setup_retry", "modified_at": _BEFORE}),
        _frame({"entry_id": entry_id, "state": "setup_error", "modified_at": _BEFORE}),
    )

    result = await _observe_reload_settled(queue, entry_id, timeout=0.2)

    assert result is None


@pytest.mark.asyncio
async def test_the_commit_fragment_itself_cannot_settle() -> None:
    """The commit carries new data but the pre-reload state.

    Settling on it would report the pre-reload state as observed.
    """
    entry_id = "entry-1"
    queue = _queue_of(
        _frame({"entry_id": entry_id, "state": "loaded", "modified_at": _BEFORE}),
        _frame(
            {
                "entry_id": entry_id,
                "state": "loaded",
                "modified_at": _AFTER,
                "data": {"host": "x"},
            }
        ),
    )

    result = await _observe_reload_settled(queue, entry_id, timeout=0.2)

    assert result is None


@pytest.mark.asyncio
async def test_the_first_non_transient_state_change_after_commit_settles() -> None:
    """Only a state change AFTER the commit is the reload's own outcome."""
    entry_id = "entry-1"
    final_fragment = {"entry_id": entry_id, "state": "loaded", "modified_at": _AFTER}
    queue = _queue_of(
        _frame({"entry_id": entry_id, "state": "setup_retry", "modified_at": _BEFORE}),
        _frame({"entry_id": entry_id, "state": "setup_retry", "modified_at": _AFTER}),
        _frame(final_fragment),
    )

    result = await _observe_reload_settled(queue, entry_id, timeout=0.2)

    assert result == final_fragment


@pytest.mark.asyncio
async def test_post_commit_transient_states_are_consumed_until_the_first_non_transient_change() -> (
    None
):
    """Only the state that finally leaves the transient set is the outcome."""
    entry_id = "entry-1"
    final_fragment = {"entry_id": entry_id, "state": "loaded", "modified_at": _AFTER}
    queue = _queue_of(
        _frame({"entry_id": entry_id, "state": "setup_retry", "modified_at": _BEFORE}),
        _frame({"entry_id": entry_id, "state": "setup_retry", "modified_at": _AFTER}),
        _frame(
            {"entry_id": entry_id, "state": "unload_in_progress", "modified_at": _AFTER}
        ),
        _frame({"entry_id": entry_id, "state": "not_loaded", "modified_at": _AFTER}),
        _frame(
            {"entry_id": entry_id, "state": "setup_in_progress", "modified_at": _AFTER}
        ),
        _frame(final_fragment),
    )

    result = await _observe_reload_settled(queue, entry_id, timeout=0.2)

    assert result == final_fragment


@pytest.mark.asyncio
async def test_a_stream_that_never_transitions_after_commit_expires_the_budget() -> (
    None
):
    entry_id = "entry-1"
    queue = _queue_of(
        _frame({"entry_id": entry_id, "state": "loaded", "modified_at": _BEFORE}),
        _frame({"entry_id": entry_id, "state": "loaded", "modified_at": _AFTER}),
        _frame({"entry_id": entry_id, "state": "loaded", "modified_at": _AFTER}),
    )

    result = await _observe_reload_settled(queue, entry_id, timeout=0.2)

    assert result is None


@pytest.mark.asyncio
async def test_no_modified_at_bump_within_the_commit_timeout_gives_up_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resubmit of identical values never bumps `modified_at`.

    The internal commit-visibility budget must cut the wait short instead of
    stalling for the whole (much larger) caller-supplied timeout.
    """
    monkeypatch.setattr(
        "ha_mcp.tools.config_entry_reload_watch._COMMIT_VISIBLE_TIMEOUT", 0.25
    )
    entry_id = "entry-1"
    queue = _queue_of(
        _frame({"entry_id": entry_id, "state": "loaded", "modified_at": _BEFORE})
    )

    started = asyncio.get_running_loop().time()
    result = await _observe_reload_settled(queue, entry_id, timeout=2.0)
    elapsed = asyncio.get_running_loop().time() - started

    assert result is None
    assert elapsed < 1.0, f"waited {elapsed:.1f}s despite the short commit timeout"


@pytest.mark.parametrize(
    "modified_at",
    [
        pytest.param(None, id="modified_at_missing"),
        pytest.param("not-a-timestamp", id="modified_at_unparseable"),
    ],
)
@pytest.mark.asyncio
async def test_a_baseline_with_no_usable_modified_at_returns_none(
    modified_at: str | None,
) -> None:
    """Without a comparable baseline nothing can be attributed to the commit.

    Followed by a stand-in commit and a settle fragment, so a parser that
    wrongly accepted the baseline would return one of them instead of running
    out of stream and reaching None by the other route.
    """
    entry_id = "entry-1"
    fragment: dict[str, Any] = {"entry_id": entry_id, "state": "loaded"}
    if modified_at is not None:
        fragment["modified_at"] = modified_at
    queue = _queue_of(
        _frame(fragment),
        _frame({"entry_id": entry_id, "state": "loaded", "modified_at": _AFTER}),
        _frame({"entry_id": entry_id, "state": "setup_retry"}),
    )

    result = await _observe_reload_settled(queue, entry_id, timeout=0.2)

    assert result is None


@pytest.mark.asyncio
async def test_a_disabled_first_fragment_returns_immediately_without_waiting() -> None:
    """A disabled entry is never reloaded, so no transition is ever coming."""
    entry_id = "entry-1"
    queue = _queue_of(
        _frame(
            {
                "entry_id": entry_id,
                "state": "not_loaded",
                "modified_at": _BEFORE,
                "disabled_by": "user",
            }
        )
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
    final_fragment = {"entry_id": entry_id, "state": "loaded", "modified_at": _AFTER}
    queue = _queue_of(
        _frame({"entry_id": entry_id, "state": "setup_retry", "modified_at": _BEFORE}),
        _frame({"entry_id": "entry-other", "state": "loaded", "modified_at": _AFTER}),
        _frame(
            {"entry_id": "entry-other", "state": "not_loaded", "modified_at": _AFTER}
        ),
        _frame({"entry_id": entry_id, "state": "setup_retry", "modified_at": _AFTER}),
        _frame(final_fragment),
    )

    result = await _observe_reload_settled(queue, entry_id, timeout=0.2)

    assert result == final_fragment


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(1786953600.0, 1786953605.0, id="epoch_float_as_ha_sends"),
        pytest.param(1786953600, 1786953605, id="epoch_int"),
        pytest.param(
            "2026-08-16T10:00:00+00:00", "2026-08-16T10:00:05+00:00", id="iso_tolerance"
        ),
    ],
)
async def test_the_commit_gate_reads_the_shape_home_assistant_actually_sends(
    before: object, after: object
) -> None:
    """`as_json_fragment` emits `modified_at.timestamp()`, a JSON number.

    A str-only parser rejects every real fragment, so the baseline is never
    established and the whole observation silently degrades to polling.
    """
    entry = {"entry_id": "e1", "state": "loaded", "modified_at": before}
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    for frag in (
        dict(entry),
        {**entry, "modified_at": after},
        {**entry, "modified_at": after, "state": "setup_in_progress"},
        {**entry, "modified_at": after, "state": "setup_retry"},
    ):
        queue.put_nowait({"event": [{"type": "updated", "entry": frag}]})

    settled = await _observe_reload_settled(queue, "e1", timeout=0.5)

    assert settled is not None, "the commit gate rejected the real fragment shape"
    assert settled["state"] == "setup_retry"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw", [None, True, [], {}, "not-a-timestamp"], ids=lambda v: repr(v)[:14]
)
async def test_an_unusable_modified_at_degrades_rather_than_guessing(
    raw: object,
) -> None:
    """Nothing can be attributed to the commit, so the poll is the honest answer.

    The stand-in commit and settle fragments behind the bad baseline are what
    make this bite: with a single fragment a parser that wrongly ACCEPTS the
    baseline also returns None, just from running out of stream.
    """
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    for frag in (
        {"entry_id": "e1", "state": "loaded", "modified_at": raw},
        # Would be read as the commit by a parser that accepted the baseline.
        {"entry_id": "e1", "state": "loaded", "modified_at": _AFTER},
        # And this would then settle it. Post-commit fragments are not
        # re-checked for modified_at, so it needs none.
        {"entry_id": "e1", "state": "setup_retry"},
    ):
        queue.put_nowait({"event": [{"type": "updated", "entry": frag}]})

    assert await _observe_reload_settled(queue, "e1", timeout=0.25) is None
