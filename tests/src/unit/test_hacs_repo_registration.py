"""Unit tests for the HACS repo-registration wait helper in tools_hacs.

``wait_for_repo_registration`` backs ``ha_manage_hacs``'s add_repository
and download flows. These tests drive the real function against a mocked
WS client: post-subscribe sample, event-driven detection, the no-relist
guard for unrelated/malformed dispatches (HACS list payloads can be 2 MB+),
subscribe-failure fallback, backstop polling, queue-shutdown last-chance
lookup, and the transport-error-swallow vs programming-error-propagate
split.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

# Arbitrary repo slug for the waiter to watch; the value itself is not
# meaningful to the helper (any owner/repo string works).
WATCHED_REPO = "homeassistant-ai/ha-mcp-integration"


def _list_response_with_repo(repo_id: int = 42) -> dict:
    return {
        "success": True,
        "result": [
            {"full_name": WATCHED_REPO, "id": repo_id, "installed": False},
        ],
    }


def _list_response_empty() -> dict:
    return {"success": True, "result": []}


def _build_ws_client(
    list_responses: list[dict],
    subscribe_result: tuple[int, "asyncio.Queue"] | Exception = (1, None),
):
    """Build a MagicMock WS client whose ``send_command`` returns each list_responses entry in turn.

    ``subscribe_command`` returns ``subscribe_result`` directly (or raises if
    Exception). ``unsubscribe_command`` is a no-op AsyncMock.
    """
    ws_client = MagicMock()
    ws_client.send_command = AsyncMock(side_effect=list_responses)

    if isinstance(subscribe_result, Exception):
        ws_client.subscribe_command = AsyncMock(side_effect=subscribe_result)
    else:
        ws_client.subscribe_command = AsyncMock(return_value=subscribe_result)

    ws_client.unsubscribe_command = AsyncMock()
    return ws_client


class TestWaitForRepoRegistration:
    """Subscription-driven helper that replaces the old 10x1s blind poll.

    Lives in ``tools_hacs`` behind ``ha_manage_hacs``: ``add_repository``
    waits on it directly and the download flow reaches it via
    ``_resolve_hacs_repo_id``.
    """

    @pytest.mark.asyncio
    async def test_post_subscribe_sample_finds_repo_already_listed(self):
        """Repo already in the post-subscribe list — return without waiting."""
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        queue: asyncio.Queue = asyncio.Queue()
        ws_client = _build_ws_client(
            list_responses=[_list_response_with_repo(repo_id=42)],
            subscribe_result=(7, queue),
        )

        repo = await wait_for_repo_registration(ws_client, WATCHED_REPO)

        assert repo is not None
        assert str(repo.get("id")) == "42"
        ws_client.subscribe_command.assert_awaited_once()
        ws_client.unsubscribe_command.assert_awaited_once_with(7)

    @pytest.mark.asyncio
    async def test_event_triggers_targeted_list_lookup(self):
        """Matching dispatch event → fresh list lookup to get the full entry."""
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(
            {
                "id": 7,
                "type": "event",
                "event": {
                    "action": "registration",
                    "repository": WATCHED_REPO,
                    "repository_id": 99,
                },
            }
        )
        ws_client = _build_ws_client(
            # Post-subscribe sample: empty. Then event arrives; helper
            # re-lists to pick up the full entry.
            list_responses=[
                _list_response_empty(),
                _list_response_with_repo(repo_id=99),
            ],
            subscribe_result=(7, queue),
        )

        repo = await wait_for_repo_registration(ws_client, WATCHED_REPO)

        assert repo is not None
        assert str(repo.get("id")) == "99"
        assert ws_client.send_command.await_count == 2

    @pytest.mark.asyncio
    async def test_unrelated_event_does_not_recheck_list(self):
        """Unrelated dispatch must NOT trigger a list lookup.

        HACS' ``hacs/repositories/list`` payload can be 2 MB+ on busy
        installs; re-listing on every unrelated dispatch event would
        defeat the whole point of using the dispatcher as the signal.
        The list re-check belongs on the backstop-poll path only.
        """
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(
            {
                "id": 7,
                "type": "event",
                "event": {
                    "action": "registration",
                    "repository": "someone-else/other-repo",
                    "repository_id": 1,
                },
            }
        )
        ws_client = MagicMock()
        # send_command is called once for the post-subscribe sample,
        # then must NOT be called again for the unrelated event —
        # the test would block on the empty queue otherwise, so the
        # short backstop interval ensures the timeout fires and we
        # assert send_command was called exactly once (sample-only).
        ws_client.send_command = AsyncMock(return_value=_list_response_empty())
        ws_client.subscribe_command = AsyncMock(return_value=(7, queue))
        ws_client.unsubscribe_command = AsyncMock()

        # ``backstop_poll_interval`` deliberately set LARGER than
        # ``timeout`` so the backstop tick never fires within the
        # wait — the only ``send_command`` calls should be the
        # post-subscribe sample (1). Per-event re-listing on the
        # unrelated event would push this to 2.
        repo = await wait_for_repo_registration(
            ws_client, WATCHED_REPO, timeout=0.05, backstop_poll_interval=10.0
        )

        assert repo is None
        assert ws_client.send_command.await_count == 1, (
            f"send_command should not be called per-event; saw "
            f"{ws_client.send_command.await_count} calls"
        )

    @pytest.mark.asyncio
    async def test_subscribe_failure_falls_back_to_single_list_lookup(self):
        """If ``hacs/subscribe`` fails with a transport error, fall back."""
        from ha_mcp.client.rest_client import HomeAssistantCommandError
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        ws_client = _build_ws_client(
            list_responses=[_list_response_with_repo(repo_id=42)],
            subscribe_result=HomeAssistantCommandError("unknown_command"),
        )

        repo = await wait_for_repo_registration(ws_client, WATCHED_REPO)

        assert repo is not None
        assert str(repo.get("id")) == "42"
        ws_client.unsubscribe_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_returns_none_after_budget(self):
        """Wall-clock backstop fires when neither event nor list shows the repo."""
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        queue: asyncio.Queue = asyncio.Queue()
        ws_client = MagicMock()
        ws_client.send_command = AsyncMock(return_value=_list_response_empty())
        ws_client.subscribe_command = AsyncMock(return_value=(7, queue))
        ws_client.unsubscribe_command = AsyncMock()

        repo = await wait_for_repo_registration(
            ws_client, WATCHED_REPO, timeout=0.05, backstop_poll_interval=0.02
        )

        assert repo is None
        ws_client.unsubscribe_command.assert_awaited_once_with(7)

    @pytest.mark.asyncio
    async def test_multiple_empty_backstop_ticks_then_timeout(self):
        """N backstop ticks all return empty, then budget exhausts cleanly.

        Pins the ``was_backstop_tick`` distinction: a tick that fires
        because the wall-clock budget is about to exhaust must NOT
        burn a list call right before the next iteration would
        return None anyway. With ``timeout=0.07``, ``backstop=0.02``
        we expect three full ticks (at ~0.02, 0.04, 0.06) followed
        by a budget-cap wait of ~0.01 that must NOT re-list.

        Asserts list called exactly 4 times: 1 post-subscribe sample
        + 3 backstop ticks. A 5th call would indicate the budget-
        exhaust branch was incorrectly treated as a backstop tick.
        """
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        queue: asyncio.Queue = asyncio.Queue()  # no events ever
        ws_client = MagicMock()
        ws_client.send_command = AsyncMock(return_value=_list_response_empty())
        ws_client.subscribe_command = AsyncMock(return_value=(7, queue))
        ws_client.unsubscribe_command = AsyncMock()

        repo = await wait_for_repo_registration(
            ws_client, WATCHED_REPO, timeout=0.07, backstop_poll_interval=0.02
        )

        assert repo is None
        # Exact count would be fragile due to scheduling jitter, so
        # assert the bounds the contract guarantees:
        # - At least 2 calls (post-subscribe sample + at least one
        #   real backstop tick before budget exhaust)
        # - At most 4 calls (sample + 3 backstop ticks fitting in
        #   the 0.07s budget at 0.02s cadence)
        # A regression to "list on every event=None" would consistently
        # land above 4 due to extra budget-cap calls.
        count = ws_client.send_command.await_count
        assert 2 <= count <= 4, (
            f"Expected 2-4 list calls (sample + backstop ticks); got {count}"
        )

    @pytest.mark.asyncio
    async def test_queue_shutdown_attempts_one_last_lookup(self):
        """Mid-wait connection teardown: try one final list lookup before giving up.

        Setup primes the queue with one non-matching event so the
        wait loop is exercised (not just the post-subscribe sample).
        Then shutdown(immediate=True) causes the next ``queue.get()``
        to raise ``QueueShutDown``, triggering the last-chance lookup.
        """
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put(
            {
                "id": 7,
                "type": "event",
                "event": {
                    "action": "registration",
                    "repository": "someone-else/other-repo",
                    "repository_id": 1,
                },
            }
        )
        # Shut down so the SECOND queue.get() (after the unrelated
        # event is consumed) raises QueueShutDown.
        queue.shutdown(immediate=False)

        ws_client = _build_ws_client(
            list_responses=[
                _list_response_empty(),  # post-subscribe sample finds nothing
                _list_response_with_repo(repo_id=42),  # last-chance lookup
            ],
            subscribe_result=(7, queue),
        )

        repo = await wait_for_repo_registration(ws_client, WATCHED_REPO)

        assert repo is not None
        assert str(repo.get("id")) == "42"
        ws_client.unsubscribe_command.assert_awaited_once_with(7)

    @pytest.mark.asyncio
    async def test_last_chance_lookup_swallows_transport_error(self):
        """QueueShutDown + dead WS: list call fails → return None, no propagation."""
        from ha_mcp.client.rest_client import HomeAssistantConnectionError
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        queue: asyncio.Queue = asyncio.Queue()
        queue.shutdown(immediate=True)

        ws_client = MagicMock()
        # First call (post-subscribe sample) succeeds, second
        # (last-chance after QueueShutDown) raises — the same teardown
        # that shut the queue typically also kills the WS connection.
        ws_client.send_command = AsyncMock(
            side_effect=[
                _list_response_empty(),
                HomeAssistantConnectionError("WS torn down"),
            ]
        )
        ws_client.subscribe_command = AsyncMock(return_value=(7, queue))
        ws_client.unsubscribe_command = AsyncMock()

        # Must not propagate the connection error — callers see a
        # wait timeout (None), not a noisy stack trace.
        repo = await wait_for_repo_registration(ws_client, WATCHED_REPO)
        assert repo is None
        ws_client.unsubscribe_command.assert_awaited_once_with(7)

    @pytest.mark.asyncio
    async def test_subscribe_propagates_programming_error(self):
        """Bug-class exceptions from subscribe must NOT degrade to fallback."""
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        ws_client = MagicMock()
        # AttributeError simulates a programming bug — e.g. ws_client
        # shape drift. Must propagate, not be swallowed by an
        # ``except Exception`` and silently degraded.
        ws_client.subscribe_command = AsyncMock(
            side_effect=AttributeError("ws_client missing 'subscribe_command'")
        )
        ws_client.send_command = AsyncMock(return_value=_list_response_empty())
        ws_client.unsubscribe_command = AsyncMock()

        with pytest.raises(AttributeError):
            await wait_for_repo_registration(ws_client, WATCHED_REPO)
        ws_client.unsubscribe_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_malformed_event_payload_does_not_recheck_list(self):
        """Non-dict / empty event payloads must NOT trigger list lookups."""
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        queue: asyncio.Queue = asyncio.Queue()
        await queue.put({"id": 7, "type": "event", "event": None})
        await queue.put({"id": 7, "type": "event", "event": "not-a-dict"})
        await queue.put({"id": 7, "type": "event", "event": {}})

        ws_client = MagicMock()
        ws_client.send_command = AsyncMock(return_value=_list_response_empty())
        ws_client.subscribe_command = AsyncMock(return_value=(7, queue))
        ws_client.unsubscribe_command = AsyncMock()

        # backstop > timeout ⇒ no backstop tick fires. Only the
        # post-subscribe sample (1 call). Per-event re-listing on
        # the malformed payloads would push this to 4.
        repo = await wait_for_repo_registration(
            ws_client, WATCHED_REPO, timeout=0.05, backstop_poll_interval=10.0
        )

        assert repo is None
        assert ws_client.send_command.await_count == 1

    @pytest.mark.asyncio
    async def test_backstop_poll_rechecks_list_on_silent_dispatch(self):
        """No events at all → backstop tick re-checks list and finds the repo.

        Pins the belt-and-braces path: if HACS' dispatcher drops or
        delays the REPOSITORY event for any reason, the backstop
        timer still picks up registration via a list lookup.
        """
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        queue: asyncio.Queue = asyncio.Queue()  # never populated
        ws_client = MagicMock()
        ws_client.send_command = AsyncMock(
            side_effect=[
                _list_response_empty(),  # post-subscribe sample
                _list_response_with_repo(repo_id=42),  # backstop tick
            ]
        )
        ws_client.subscribe_command = AsyncMock(return_value=(7, queue))
        ws_client.unsubscribe_command = AsyncMock()

        # backstop_poll_interval well within the timeout so the
        # backstop tick fires before the wall-clock budget exhausts.
        repo = await wait_for_repo_registration(
            ws_client, WATCHED_REPO, timeout=1.0, backstop_poll_interval=0.05
        )

        assert repo is not None
        assert str(repo.get("id")) == "42"
        assert ws_client.send_command.await_count == 2
        ws_client.unsubscribe_command.assert_awaited_once_with(7)

    @pytest.mark.asyncio
    async def test_matching_event_with_empty_list_continues_waiting(self):
        """Dispatch claims our repo, but list lookup races and returns empty.

        Loop must NOT return None on that single failed lookup — it
        should fall through to the queue wait so a later dispatch
        catches the actual registration.
        """
        from ha_mcp.tools.tools_hacs import wait_for_repo_registration

        queue: asyncio.Queue = asyncio.Queue()
        # First dispatch: matches but the list lookup will race.
        await queue.put(
            {
                "id": 7,
                "type": "event",
                "event": {
                    "action": "registration",
                    "repository": WATCHED_REPO,
                    "repository_id": 42,
                },
            }
        )

        ws_client = MagicMock()
        # Every list call returns empty so the loop never finds the
        # repo. The waiter must keep going until the wall-clock
        # budget exhausts and return None — NOT return early on the
        # single failed post-event lookup.
        ws_client.send_command = AsyncMock(return_value=_list_response_empty())
        ws_client.subscribe_command = AsyncMock(return_value=(7, queue))
        ws_client.unsubscribe_command = AsyncMock()

        repo = await wait_for_repo_registration(
            ws_client, WATCHED_REPO, timeout=0.05, backstop_poll_interval=0.02
        )

        # Returns None on timeout (not on the single failed lookup)
        # — and unsubscribe always runs in finally.
        assert repo is None
        ws_client.unsubscribe_command.assert_awaited_once_with(7)


class TestResolveHacsRepoIdUsesWait:
    """``_resolve_hacs_repo_id`` for GitHub paths routes through the
    subscribe-based waiter so a just-added repo's registration race is
    handled event-driven rather than by blind polling."""

    @pytest.mark.asyncio
    async def test_numeric_id_short_circuits(self):
        """Pre-resolved numeric ids must NOT subscribe — just pass through."""
        from ha_mcp.tools.tools_hacs import _resolve_hacs_repo_id

        ws_client = MagicMock()
        ws_client.subscribe_command = AsyncMock()  # must not be called

        numeric_id, display_name = await _resolve_hacs_repo_id(ws_client, "441028036")

        assert numeric_id == "441028036"
        assert display_name == "441028036"
        ws_client.subscribe_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_github_path_uses_subscribe_based_wait(self):
        """Github-path identifiers route through ``wait_for_repo_registration``."""
        from ha_mcp.tools.tools_hacs import _resolve_hacs_repo_id

        queue: asyncio.Queue = asyncio.Queue()
        ws_client = MagicMock()
        ws_client.send_command = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {
                        "full_name": "piitaya/lovelace-mushroom",
                        "id": 12345,
                        "name": "Mushroom",
                    },
                ],
            }
        )
        ws_client.subscribe_command = AsyncMock(return_value=(7, queue))
        ws_client.unsubscribe_command = AsyncMock()

        numeric_id, display_name = await _resolve_hacs_repo_id(
            ws_client, "piitaya/lovelace-mushroom"
        )

        assert numeric_id == "12345"
        assert display_name == "Mushroom"
        ws_client.subscribe_command.assert_awaited_once()
        ws_client.unsubscribe_command.assert_awaited_once_with(7)
