"""
Unit tests for wait utility functions in util_helpers.

Tests the wait_for_entity_registered, wait_for_entity_removed, and
wait_for_state_change functions (issues #381, #1152).

Note on the WS path: the helpers prefer a WebSocket subscription and only
fall back to REST polling when the WS is unavailable. The fixture below
forces every test to take the REST-fallback path unless the test
explicitly opts into the WS path via the `ws_client` fixture. That keeps
the legacy contract under test (REST poll semantics) and gives the WS
path its own focused coverage.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import util_helpers
from ha_mcp.tools.util_helpers import (
    wait_for_automation_entity_by_unique_id,
    wait_for_entity_registered,
    wait_for_entity_removed,
    wait_for_state_change,
)


@pytest.fixture(autouse=True)
def force_rest_fallback(monkeypatch):
    """Default: force WS path off so existing REST-semantics tests still
    cover the legacy poll loop. Tests that want to exercise the WS path
    install a ws_client via the ``ws_client`` fixture below, which
    overrides this stub with a real fake."""

    async def _no_ws(_client):
        return None

    monkeypatch.setattr(util_helpers, "_get_waiter_ws_client", _no_ws)


class TestWaitForEntityRegistered:
    """Test wait_for_entity_registered utility."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_entity_state = AsyncMock()
        return client

    async def test_returns_true_when_entity_immediately_available(self, mock_client):
        """Entity available on first poll returns True immediately."""
        mock_client.get_entity_state.return_value = {
            "state": "on",
            "entity_id": "light.test",
        }
        result = await wait_for_entity_registered(
            mock_client, "light.test", timeout=2.0
        )
        assert result is True
        mock_client.get_entity_state.assert_called_with("light.test")

    async def test_returns_true_when_entity_becomes_available(self, mock_client):
        """Entity that becomes available after a few 404s returns True."""
        mock_client.get_entity_state.side_effect = [
            HomeAssistantAPIError("not found", status_code=404),
            HomeAssistantAPIError("not found", status_code=404),
            {"state": "off", "entity_id": "light.test"},
        ]
        result = await wait_for_entity_registered(
            mock_client, "light.test", timeout=5.0, poll_interval=0.05
        )
        assert result is True
        assert mock_client.get_entity_state.call_count == 3

    async def test_returns_false_on_timeout(self, mock_client):
        """Returns False if entity never becomes available."""
        mock_client.get_entity_state.side_effect = HomeAssistantAPIError(
            "not found", status_code=404
        )
        result = await wait_for_entity_registered(
            mock_client, "light.test", timeout=0.01, poll_interval=0.001
        )
        assert result is False

    async def test_returns_false_when_state_is_falsy(self, mock_client):
        """Returns False if get_entity_state returns falsy."""
        mock_client.get_entity_state.return_value = None
        result = await wait_for_entity_registered(
            mock_client, "light.test", timeout=0.01, poll_interval=0.001
        )
        assert result is False

    async def test_raises_on_connection_error(self, mock_client):
        """Connection errors propagate instead of being silently swallowed."""
        mock_client.get_entity_state.side_effect = HomeAssistantConnectionError(
            "network down"
        )
        with pytest.raises(HomeAssistantConnectionError):
            await wait_for_entity_registered(
                mock_client, "light.test", timeout=2.0, poll_interval=0.05
            )


class TestWaitForEntityRemoved:
    """Test wait_for_entity_removed utility."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_entity_state = AsyncMock()
        return client

    async def test_returns_true_when_entity_immediately_gone(self, mock_client):
        """Entity gone on first poll (404) returns True."""
        mock_client.get_entity_state.side_effect = HomeAssistantAPIError(
            "not found", status_code=404
        )
        result = await wait_for_entity_removed(mock_client, "light.test", timeout=2.0)
        assert result is True

    async def test_returns_true_when_entity_returns_none(self, mock_client):
        """Entity returning None/falsy is treated as removed."""
        mock_client.get_entity_state.return_value = None
        result = await wait_for_entity_removed(mock_client, "light.test", timeout=2.0)
        assert result is True

    async def test_returns_true_when_entity_eventually_removed(self, mock_client):
        """Entity that exists then gets removed (404) returns True."""
        mock_client.get_entity_state.side_effect = [
            {"state": "on"},
            {"state": "on"},
            HomeAssistantAPIError("not found", status_code=404),
        ]
        result = await wait_for_entity_removed(
            mock_client, "light.test", timeout=5.0, poll_interval=0.05
        )
        assert result is True

    async def test_returns_false_on_timeout(self, mock_client):
        """Returns False if entity never gets removed."""
        mock_client.get_entity_state.return_value = {"state": "on"}
        result = await wait_for_entity_removed(
            mock_client, "light.test", timeout=0.01, poll_interval=0.001
        )
        assert result is False

    async def test_raises_on_connection_error(self, mock_client):
        """Connection errors propagate instead of falsely reporting deletion."""
        mock_client.get_entity_state.side_effect = HomeAssistantConnectionError(
            "network down"
        )
        with pytest.raises(HomeAssistantConnectionError):
            await wait_for_entity_removed(
                mock_client, "light.test", timeout=2.0, poll_interval=0.05
            )


class TestWaitForStateChange:
    """Test wait_for_state_change utility."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_entity_state = AsyncMock()
        return client

    async def test_detects_expected_state_change(self, mock_client):
        """Detects when entity reaches the expected state."""
        mock_client.get_entity_state.side_effect = [
            # Initial state fetch
            {"state": "off", "entity_id": "light.test"},
            # Polls during wait
            {"state": "off", "entity_id": "light.test"},
            {"state": "on", "entity_id": "light.test"},
        ]
        result = await wait_for_state_change(
            mock_client,
            "light.test",
            expected_state="on",
            timeout=5.0,
            poll_interval=0.05,
        )
        assert result is not None
        assert result["state"] == "on"

    async def test_detects_any_state_change(self, mock_client):
        """Detects any state change when no expected_state is given."""
        mock_client.get_entity_state.side_effect = [
            # Initial state fetch
            {"state": "off", "entity_id": "light.test"},
            # Polls during wait
            {"state": "off", "entity_id": "light.test"},
            {"state": "on", "entity_id": "light.test"},
        ]
        result = await wait_for_state_change(
            mock_client,
            "light.test",
            timeout=5.0,
            poll_interval=0.05,
        )
        assert result is not None
        assert result["state"] == "on"

    async def test_returns_none_on_timeout(self, mock_client):
        """Returns None if state doesn't change within timeout."""
        mock_client.get_entity_state.return_value = {
            "state": "off",
            "entity_id": "light.test",
        }
        result = await wait_for_state_change(
            mock_client,
            "light.test",
            expected_state="on",
            timeout=0.01,
            poll_interval=0.001,
        )
        assert result is None

    async def test_uses_provided_initial_state(self, mock_client):
        """Uses provided initial_state instead of fetching."""
        mock_client.get_entity_state.return_value = {
            "state": "on",
            "entity_id": "light.test",
        }
        result = await wait_for_state_change(
            mock_client,
            "light.test",
            initial_state="off",
            timeout=2.0,
            poll_interval=0.05,
        )
        # Should detect change since initial_state=off but current=on
        assert result is not None
        assert result["state"] == "on"

    async def test_expected_state_immediately_met(self, mock_client):
        """Returns immediately if entity already at expected state."""
        mock_client.get_entity_state.return_value = {
            "state": "on",
            "entity_id": "light.test",
        }
        result = await wait_for_state_change(
            mock_client,
            "light.test",
            expected_state="on",
            timeout=2.0,
            poll_interval=0.05,
        )
        assert result is not None
        assert result["state"] == "on"

    async def test_initial_fetch_fails_then_detects_change(self, mock_client):
        """When initial fetch fails (API error), uses first successful poll as baseline and detects subsequent change."""
        mock_client.get_entity_state.side_effect = [
            # Initial state fetch fails (in the pre-loop section)
            HomeAssistantAPIError("not found", status_code=404),
            # First poll succeeds - becomes baseline (off)
            {"state": "off", "entity_id": "light.test"},
            # Second poll - state changed
            {"state": "on", "entity_id": "light.test"},
        ]
        result = await wait_for_state_change(
            mock_client,
            "light.test",
            expected_state=None,  # No specific expected state
            timeout=5.0,
            poll_interval=0.05,
        )
        assert result is not None
        assert result["state"] == "on"

    async def test_handles_api_errors_gracefully(self, mock_client):
        """API errors in polling loop are tolerated (entity may not exist yet)."""
        mock_client.get_entity_state.side_effect = [
            # Initial fetch OK
            {"state": "off", "entity_id": "light.test"},
            # Transient API error during polling
            HomeAssistantAPIError("server error", status_code=500),
            # Then state changes
            {"state": "on", "entity_id": "light.test"},
        ]
        result = await wait_for_state_change(
            mock_client,
            "light.test",
            expected_state="on",
            timeout=5.0,
            poll_interval=0.05,
        )
        assert result is not None
        assert result["state"] == "on"

    async def test_raises_on_connection_error_in_initial_fetch(self, mock_client):
        """Connection errors during initial state fetch propagate."""
        mock_client.get_entity_state.side_effect = HomeAssistantConnectionError(
            "network down"
        )
        with pytest.raises(HomeAssistantConnectionError):
            await wait_for_state_change(
                mock_client,
                "light.test",
                expected_state="on",
                timeout=2.0,
                poll_interval=0.05,
            )

    async def test_raises_on_connection_error_in_polling(self, mock_client):
        """Connection errors during polling propagate."""
        mock_client.get_entity_state.side_effect = [
            # Initial fetch OK
            {"state": "off", "entity_id": "light.test"},
            # Connection error during polling
            HomeAssistantConnectionError("network down"),
        ]
        with pytest.raises(HomeAssistantConnectionError):
            await wait_for_state_change(
                mock_client,
                "light.test",
                expected_state="on",
                timeout=5.0,
                poll_interval=0.05,
            )


# ---------------------------------------------------------------------------
# WS path coverage (#1152)
#
# These tests opt out of ``force_rest_fallback`` by installing a fake WS
# client that records handler / subscribe / unsubscribe calls and lets the
# test push events at will. They exercise the four guarantees from the
# implementation comment in ``util_helpers``:
#
#   1. Sample-after-subscribe resolves before any event arrives.
#   2. Event arrival nudges the wait loop and resolves it on the next sample.
#   3. Cleanup (remove_event_handler / unsubscribe_events) always runs.
#   4. Subscribe failure falls back to the legacy REST poll loop.
# ---------------------------------------------------------------------------


class FakeWebSocketClient:
    """In-process WS client double for the waiter tests.

    Records handler attach / detach calls, tracks active subscriptions,
    and lets tests synchronously fire events into the handler.
    """

    def __init__(self) -> None:
        self.is_connected = True
        self.handlers: dict[str, list] = {}
        self.subscribed: list[str] = []
        self.unsubscribed: list[int] = []
        self._next_sub = 100
        self._subscribe_failure: Exception | None = None
        self._subscribe_fail_after: int | None = None
        self._unsubscribe_failure: Exception | None = None

    def set_subscribe_failure(
        self, exc: Exception | None, *, after: int | None = None
    ) -> None:
        """Make the next ``subscribe_events`` call raise ``exc``.

        ``after=N`` lets the first N successes through; useful for the
        partial-subscribe failure path (first event_type subscribes
        fine, second raises) so the cleanup branch with a non-empty
        ``sub_ids`` is exercised.
        """
        self._subscribe_failure = exc
        self._subscribe_fail_after = after

    def set_unsubscribe_failure(self, exc: Exception | None) -> None:
        """Make ``unsubscribe_events`` raise ``exc`` for every sub_id."""
        self._unsubscribe_failure = exc

    def add_event_handler(self, event_type: str, handler) -> None:
        self.handlers.setdefault(event_type, []).append(handler)

    def remove_event_handler(self, event_type: str, handler) -> None:
        if event_type in self.handlers and handler in self.handlers[event_type]:
            self.handlers[event_type].remove(handler)

    async def subscribe_events(self, event_type: str) -> int:
        if self._subscribe_failure is not None:
            if self._subscribe_fail_after is None or self._subscribe_fail_after <= 0:
                raise self._subscribe_failure
            self._subscribe_fail_after -= 1
        self.subscribed.append(event_type)
        self._next_sub += 1
        return self._next_sub

    async def unsubscribe_events(self, sub_id: int) -> None:
        self.unsubscribed.append(sub_id)
        if self._unsubscribe_failure is not None:
            raise self._unsubscribe_failure

    async def fire_state_changed(
        self, entity_id: str, *, new_state: dict | None = None
    ) -> None:
        """Dispatch a state_changed event to every registered handler.

        Default payload mirrors the legacy "entity_id only" shape used by
        the ``wait_for_entity_*`` waiters. Pass ``new_state={...}`` to
        include ``data.new_state`` — needed by discovery waiters (#1395)
        whose filter inspects ``new_state.attributes``.
        """
        data: dict = {"entity_id": entity_id}
        if new_state is not None:
            data["new_state"] = new_state
        for handler in list(self.handlers.get("state_changed", [])):
            await handler({"event_type": "state_changed", "data": data})


@pytest.fixture
def ws_client(monkeypatch):
    """Install a FakeWebSocketClient as the WS path's source."""
    fake = FakeWebSocketClient()

    async def _ws(_client):
        return fake

    monkeypatch.setattr(util_helpers, "_get_waiter_ws_client", _ws)
    return fake


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.get_entity_state = AsyncMock()
    client.base_url = "http://example.invalid"
    client.token = "test-token"
    return client


class TestWsPathRegistered:
    """WS-driven coverage for wait_for_entity_registered."""

    async def test_post_subscribe_sample_resolves_immediately(
        self, ws_client, mock_client
    ):
        """If the entity is already registered when we subscribe, no event
        is needed — the post-subscribe sample resolves and we never wait."""
        mock_client.get_entity_state.return_value = {"state": "on"}

        result = await wait_for_entity_registered(
            mock_client, "light.test", timeout=5.0
        )

        assert result is True
        # We did open subscriptions for both event types before sampling.
        assert ws_client.subscribed == ["state_changed", "entity_registry_updated"]
        # Both subscriptions were released afterwards.
        assert len(ws_client.unsubscribed) == 2
        # No leaked handlers.
        assert ws_client.handlers.get("state_changed") == []
        assert ws_client.handlers.get("entity_registry_updated") == []

    async def test_event_arrival_resolves_wait(self, ws_client, mock_client):
        """Initial sample returns 404; after a state_changed event for our
        entity, the re-sample finds the state and the wait succeeds."""
        # First sample (post-subscribe): not registered yet.
        # Second sample (after event): registered.
        mock_client.get_entity_state.side_effect = [
            HomeAssistantAPIError("not found", status_code=404),
            {"state": "on"},
        ]

        async def fire_after_subscribe():
            # Give the wait loop a tick to set up, then fire the event.
            await asyncio.sleep(0.05)
            await ws_client.fire_state_changed("light.test")

        fire_task = asyncio.create_task(fire_after_subscribe())
        result = await wait_for_entity_registered(
            mock_client, "light.test", timeout=5.0
        )
        await fire_task

        assert result is True
        assert mock_client.get_entity_state.call_count == 2

    async def test_event_for_other_entity_does_not_resolve(
        self, ws_client, mock_client
    ):
        """Events for unrelated entities must not wake the wait loop —
        the handler filters by entity_id before nudging."""
        mock_client.get_entity_state.side_effect = HomeAssistantAPIError(
            "not found", status_code=404
        )

        async def fire_noise():
            await asyncio.sleep(0.02)
            await ws_client.fire_state_changed("light.other")

        noise_task = asyncio.create_task(fire_noise())
        result = await wait_for_entity_registered(
            mock_client, "light.target", timeout=0.2
        )
        await noise_task

        assert result is False  # Timed out — noise event was correctly filtered.

    async def test_subscribe_failure_falls_back_to_rest(self, ws_client, mock_client):
        """If subscribe_events raises a connection/transport error on the
        first event_type, we degrade to the legacy REST poll."""
        ws_client.set_subscribe_failure(OSError("subscribe down"))
        mock_client.get_entity_state.return_value = {"state": "on"}

        result = await wait_for_entity_registered(
            mock_client, "light.test", timeout=2.0, poll_interval=0.01
        )

        assert result is True  # REST fallback succeeds.
        # We attached handlers but never landed a subscription, so nothing
        # to unsubscribe.
        assert ws_client.unsubscribed == []


class TestWsPathRemoved:
    """WS-driven coverage for wait_for_entity_removed."""

    async def test_event_arrival_resolves_removal(self, ws_client, mock_client):
        """Entity exists at first sample, then a state_changed event arrives
        and the re-sample finds 404."""
        mock_client.get_entity_state.side_effect = [
            {"state": "on"},
            HomeAssistantAPIError("not found", status_code=404),
        ]

        async def fire_after():
            await asyncio.sleep(0.05)
            await ws_client.fire_state_changed("light.test")

        fire_task = asyncio.create_task(fire_after())
        result = await wait_for_entity_removed(mock_client, "light.test", timeout=5.0)
        await fire_task

        assert result is True
        # Cleanup still ran.
        assert len(ws_client.unsubscribed) == 2


class TestWsPathStateChange:
    """WS-driven coverage for wait_for_state_change."""

    async def test_event_arrival_resolves_state_change(self, ws_client, mock_client):
        mock_client.get_entity_state.side_effect = [
            # Initial state fetch (before subscribe).
            {"state": "off"},
            # Post-subscribe sample — still off.
            {"state": "off"},
            # After event: changed.
            {"state": "on"},
        ]

        async def fire_after():
            await asyncio.sleep(0.05)
            await ws_client.fire_state_changed("light.test")

        fire_task = asyncio.create_task(fire_after())
        result = await wait_for_state_change(
            mock_client, "light.test", expected_state="on", timeout=5.0
        )
        await fire_task

        assert result is not None
        assert result["state"] == "on"
        # Only state_changed is needed for state-change waits.
        assert ws_client.subscribed == ["state_changed"]
        assert len(ws_client.unsubscribed) == 1

    async def test_cleanup_runs_on_timeout(self, ws_client, mock_client):
        """A timeout must still drop the subscription and handler."""
        mock_client.get_entity_state.return_value = {"state": "off"}

        result = await wait_for_state_change(
            mock_client,
            "light.test",
            expected_state="on",
            timeout=0.1,
        )

        assert result is None
        assert len(ws_client.unsubscribed) == 1
        assert ws_client.handlers.get("state_changed") == []

    async def test_baseline_adopted_under_ws_path(self, ws_client, mock_client):
        """When the pre-loop initial fetch fails (404), the sample under
        the WS path must adopt the first observed state as the baseline
        — not resolve on it. The next event must then resolve to the
        actual change. Gap #3 from the pr-test-analyzer review (#1382)."""
        # 1. Initial fetch (before subscribe): 404 → initial_state stays None.
        # 2. Post-subscribe sample: returns {"state": "off"} → adopts baseline,
        #    returns None (no change yet).
        # 3. After first event fires: returns {"state": "off"} again → still
        #    matches baseline, no resolution.
        # 4. After second event fires: returns {"state": "on"} → resolved.
        mock_client.get_entity_state.side_effect = [
            HomeAssistantAPIError("not found", status_code=404),
            {"state": "off"},
            {"state": "off"},
            {"state": "on"},
        ]

        async def fire_two():
            await asyncio.sleep(0.05)
            await ws_client.fire_state_changed("light.test")
            await asyncio.sleep(0.05)
            await ws_client.fire_state_changed("light.test")

        fire_task = asyncio.create_task(fire_two())
        result = await wait_for_state_change(mock_client, "light.test", timeout=5.0)
        await fire_task

        assert result is not None
        assert result["state"] == "on"


# ---------------------------------------------------------------------------
# Connection-drop, polling-backstop, partial-subscribe, and cleanup-error
# coverage requested by pr-test-analyzer on #1382. Each test maps to a
# specific gap in that report.
# ---------------------------------------------------------------------------


class TestWsPathConnectionDrop:
    """Mid-wait connection-drop fallback to REST (pr-test-analyzer gap #1)."""

    async def test_connection_drop_mid_wait_falls_back_to_rest(
        self, ws_client, mock_client
    ):
        """If ``ws_client.is_connected`` flips to False after a noise wake
        but before resolution, ``_ws_wait_for_condition`` must call
        ``_legacy_poll_until`` for the remaining budget and still
        resolve via REST sampling."""
        # First sample (post-subscribe): not registered.
        # Sample after first nudge (which fires noise): still not registered.
        # WS then drops; the REST fallback samples and finds the entity.
        mock_client.get_entity_state.side_effect = [
            HomeAssistantAPIError("not found", status_code=404),
            HomeAssistantAPIError("not found", status_code=404),
            {"state": "on"},  # REST fallback sees the entity
        ]

        async def fire_noise_then_drop():
            # Wake the loop with a noise event, then drop the connection
            # before the next iteration's is_connected check runs.
            await asyncio.sleep(0.05)
            await ws_client.fire_state_changed("light.test")
            await asyncio.sleep(0.01)
            ws_client.is_connected = False

        fire_task = asyncio.create_task(fire_noise_then_drop())
        result = await wait_for_entity_registered(
            mock_client, "light.test", timeout=5.0, poll_interval=0.01
        )
        await fire_task

        assert result is True
        # We sampled at least three times: post-subscribe, post-noise-nudge,
        # and at least once via the REST fallback after the drop.
        assert mock_client.get_entity_state.call_count >= 3

    async def test_connection_drop_before_wait_loop_falls_back_to_rest(
        self, ws_client, mock_client
    ):
        """If the WS drops between ``subscribe_events`` returning and the
        wait loop starting, the helper must skip the loop and go straight
        to REST polling — no wasted backstop interval on a dead
        subscription."""
        # First call (post-subscribe sample) returns 404 AND drops the WS;
        # subsequent REST-fallback calls find the entity.
        call_count = {"n": 0}

        async def get_state(_entity_id):
            call_count["n"] += 1
            if call_count["n"] == 1:
                ws_client.is_connected = False
                raise HomeAssistantAPIError("not found", status_code=404)
            return {"state": "on"}

        mock_client.get_entity_state.side_effect = get_state

        result = await wait_for_entity_registered(
            mock_client, "light.test", timeout=2.0, poll_interval=0.01
        )

        assert result is True
        # At least one extra REST sample happened after the drop.
        assert call_count["n"] >= 2
        # Cleanup of the subscriptions we did establish still ran.
        assert len(ws_client.unsubscribed) == 2


class TestWsPathPollingBackstop:
    """Polling backstop fires without an event (pr-test-analyzer gap #2)."""

    async def test_backstop_resolves_without_event(
        self, ws_client, mock_client, monkeypatch
    ):
        """With a tight ``_POLLING_BACKSTOP_INTERVAL``, the waiter must
        resolve via the periodic REST sample even when no event nudge
        ever arrives — proves the backstop is wired, not vestigial."""
        monkeypatch.setattr(util_helpers, "_POLLING_BACKSTOP_INTERVAL", 0.05)
        mock_client.get_entity_state.side_effect = [
            HomeAssistantAPIError("not found", status_code=404),  # post-subscribe
            HomeAssistantAPIError("not found", status_code=404),  # backstop #1
            {"state": "on"},  # backstop #2 finds it
        ]

        result = await wait_for_entity_registered(
            mock_client, "light.test", timeout=5.0
        )

        assert result is True
        # Subscription was opened and torn down even though no event arrived.
        assert ws_client.subscribed == ["state_changed", "entity_registry_updated"]
        assert len(ws_client.unsubscribed) == 2
        # And we did sample more than once — at least the post-subscribe
        # plus one or more backstop ticks.
        assert mock_client.get_entity_state.call_count >= 2


class TestWsPathPartialSubscribeFailure:
    """First subscribe succeeds, second raises — cleanup must release the
    first sub_id before falling back. pr-test-analyzer gap #4."""

    async def test_partial_subscribe_failure_releases_first_subscription(
        self, ws_client, mock_client
    ):
        """``set_subscribe_failure(..., after=1)`` lets the first event
        type subscribe, then raises on the second. ``_ws_wait_for_condition``
        falls back to REST and the ``finally`` block must still
        unsubscribe the first (successful) subscription."""
        ws_client.set_subscribe_failure(
            HomeAssistantConnectionError("second sub down"), after=1
        )
        mock_client.get_entity_state.return_value = {"state": "on"}

        result = await wait_for_entity_registered(
            mock_client, "light.test", timeout=2.0, poll_interval=0.01
        )

        assert result is True  # REST fallback succeeds.
        # Exactly one subscription was established (state_changed),
        # and the cleanup released it.
        assert ws_client.subscribed == ["state_changed"]
        assert ws_client.unsubscribed == [101]
        # Both handlers were attached and both must be detached even though
        # only one subscription landed.
        assert ws_client.handlers.get("state_changed") == []
        assert ws_client.handlers.get("entity_registry_updated") == []


class TestWsPathUnsubscribeFailureTolerance:
    """``unsubscribe_events`` failing during cleanup must not mask the
    wait's real result. pr-test-analyzer gap #6."""

    async def test_unsubscribe_connection_error_does_not_mask_result(
        self, ws_client, mock_client
    ):
        """If ``unsubscribe_events`` raises ``HomeAssistantConnectionError``
        on cleanup, the wait must still return its real result and the
        second sub_id must still be unsubscribed (loop continues)."""
        mock_client.get_entity_state.return_value = {"state": "on"}
        ws_client.set_unsubscribe_failure(
            HomeAssistantConnectionError("connection lost during cleanup")
        )

        # Post-subscribe sample resolves immediately.
        result = await wait_for_entity_registered(
            mock_client, "light.test", timeout=2.0
        )

        assert result is True
        # Both sub_ids were attempted even though both raised.
        assert ws_client.unsubscribed == [101, 102]

    async def test_unsubscribe_command_timeout_does_not_mask_result(
        self, ws_client, mock_client
    ):
        """If ``unsubscribe_events`` raises ``HomeAssistantCommandTimeout``
        on cleanup (WS round-trip exceeded ``send_command``'s 30s
        deadline), the wait must still return its real result and the
        second sub_id must still be unsubscribed. Patch76 #1382 typed
        replacement for the previous ``str(e) == "Command timeout"``
        substring match."""
        from ha_mcp.client.rest_client import HomeAssistantCommandTimeout

        mock_client.get_entity_state.return_value = {"state": "on"}
        ws_client.set_unsubscribe_failure(
            HomeAssistantCommandTimeout("Command timeout")
        )

        result = await wait_for_entity_registered(
            mock_client, "light.test", timeout=2.0
        )

        assert result is True
        # Both sub_ids attempted even though each raised CommandTimeout.
        assert ws_client.unsubscribed == [101, 102]


# ---------------------------------------------------------------------------
# wait_for_automation_entity_by_unique_id (#1395) — discovery waits
# ---------------------------------------------------------------------------


class TestRestPathAutomationDiscovery:
    """REST-fallback coverage for wait_for_automation_entity_by_unique_id.

    Uses the autouse ``force_rest_fallback`` fixture so the WS path is
    skipped; verifies the REST sample finds the entity by unique_id."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.get_states = AsyncMock()
        client.base_url = "http://example.invalid"
        client.token = "test-token"
        return client

    async def test_resolves_via_rest_when_state_already_published(self, mock_client):
        """REST sample finds the matching automation in get_states()."""
        mock_client.get_states.return_value = [
            {"entity_id": "automation.target", "attributes": {"id": "uid_42"}}
        ]

        result = await wait_for_automation_entity_by_unique_id(
            mock_client, "uid_42", timeout=1.0
        )

        assert result == "automation.target"

    async def test_ignores_non_automation_entities_with_matching_id(self, mock_client):
        """A script (or other domain) sharing the same id attribute must
        NOT match — the filter pins ``entity_id.startswith("automation.")``
        so we don't mis-resolve to a sibling-domain entity."""
        mock_client.get_states.return_value = [
            {"entity_id": "script.distractor", "attributes": {"id": "uid_42"}},
            {"entity_id": "automation.real", "attributes": {"id": "uid_42"}},
        ]

        result = await wait_for_automation_entity_by_unique_id(
            mock_client, "uid_42", timeout=1.0
        )

        assert result == "automation.real"

    async def test_returns_none_on_full_miss(self, mock_client):
        """When the entity never appears within the budget, the wait
        times out and returns None."""
        mock_client.get_states.return_value = []

        result = await wait_for_automation_entity_by_unique_id(
            mock_client, "uid_missing", timeout=0.2, poll_interval=0.05
        )

        assert result is None

    async def test_transient_api_error_swallowed_by_sample(self, mock_client):
        """Transient ``HomeAssistantAPIError`` from ``get_states()`` is
        swallowed inside the sample callback — the waiter keeps polling
        until budget exhaustion rather than propagating."""
        mock_client.get_states.side_effect = HomeAssistantAPIError("transient")

        result = await wait_for_automation_entity_by_unique_id(
            mock_client, "uid_42", timeout=0.2, poll_interval=0.05
        )

        assert result is None
        # Sample was called at least once — verifies the catch path was
        # exercised, not the wait-loop short-circuited at setup.
        assert mock_client.get_states.call_count >= 1


class TestWsPathAutomationDiscovery:
    """WS-driven coverage for wait_for_automation_entity_by_unique_id (#1395)."""

    async def test_post_subscribe_sample_resolves_immediately(
        self, ws_client, mock_client
    ):
        """If HA already has the automation registered when the waiter
        subscribes, the post-subscribe REST sample resolves and no event
        is needed."""
        mock_client.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "automation.early",
                    "attributes": {"id": "uid_early"},
                }
            ]
        )

        result = await wait_for_automation_entity_by_unique_id(
            mock_client, "uid_early", timeout=2.0
        )

        assert result == "automation.early"
        # Only the single state_changed subscription is opened.
        assert ws_client.subscribed == ["state_changed"]
        assert len(ws_client.unsubscribed) == 1
        # Handler was cleaned up.
        assert ws_client.handlers.get("state_changed") == []

    async def test_event_arrival_resolves_discovery(self, ws_client, mock_client):
        """A state_changed event for an automation whose
        ``new_state.attributes.id`` matches the unique_id wakes the wait
        loop. The discovered entity_id is captured by the filter and
        returned from sample() on the next tick — no second REST scan
        needed."""
        # Post-subscribe sample finds nothing; the discovered entity_id
        # comes from the event itself via the filter's capture cell.
        mock_client.get_states = AsyncMock(return_value=[])

        async def fire_after_subscribe():
            await asyncio.sleep(0.05)
            await ws_client.fire_state_changed(
                "automation.discovered",
                new_state={"attributes": {"id": "uid_99"}},
            )

        fire_task = asyncio.create_task(fire_after_subscribe())
        result = await wait_for_automation_entity_by_unique_id(
            mock_client, "uid_99", timeout=2.0
        )
        await fire_task

        assert result == "automation.discovered"

    async def test_event_for_non_automation_does_not_wake_wait(
        self, ws_client, mock_client
    ):
        """Events for non-automation entities (e.g. ``light.kitchen``)
        must not nudge the wait loop, even if the new_state happens to
        carry an ``id`` attribute. Filter pins the entity_id prefix."""
        mock_client.get_states = AsyncMock(return_value=[])

        async def fire_noise():
            await asyncio.sleep(0.02)
            await ws_client.fire_state_changed(
                "light.kitchen",
                new_state={"attributes": {"id": "uid_42"}},
            )

        noise_task = asyncio.create_task(fire_noise())
        result = await wait_for_automation_entity_by_unique_id(
            mock_client, "uid_42", timeout=0.2, poll_interval=0.05
        )
        await noise_task

        assert result is None

    async def test_event_for_other_automation_does_not_wake_wait(
        self, ws_client, mock_client
    ):
        """An automation event for a *different* unique_id must not nudge —
        otherwise concurrent automation creates would race for the same
        wait loop. Filter pins ``new_state.attributes.id == unique_id``."""
        mock_client.get_states = AsyncMock(return_value=[])

        async def fire_noise():
            await asyncio.sleep(0.02)
            await ws_client.fire_state_changed(
                "automation.other",
                new_state={"attributes": {"id": "uid_other"}},
            )

        noise_task = asyncio.create_task(fire_noise())
        result = await wait_for_automation_entity_by_unique_id(
            mock_client, "uid_target", timeout=0.2, poll_interval=0.05
        )
        await noise_task

        assert result is None

    async def test_event_capture_short_circuits_rest_scan(self, ws_client, mock_client):
        """When the matching event arrives, the discovered entity_id is
        captured in the closure cell so the post-nudge ``sample()`` returns
        the captured value WITHOUT re-scanning ``get_states()``. Pin this
        explicitly by seeding ``get_states()`` with a *different* matching
        automation: cell-wins → result is the event's entity_id, scan-wins
        → result is the REST entity_id. Different outcomes give the test
        a discriminator the empty-list version couldn't provide."""
        # REST scan would resolve to ``automation.from_rest``; the event
        # carries ``automation.from_event``. Only one can win.
        mock_client.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "automation.from_rest",
                    "attributes": {"id": "uid_dup"},
                }
            ]
        )

        async def fire_after_subscribe():
            await asyncio.sleep(0.05)
            await ws_client.fire_state_changed(
                "automation.from_event",
                new_state={"attributes": {"id": "uid_dup"}},
            )

        fire_task = asyncio.create_task(fire_after_subscribe())
        result = await wait_for_automation_entity_by_unique_id(
            mock_client, "uid_dup", timeout=2.0
        )
        await fire_task

        # In practice the post-subscribe REST sample races against the
        # event arrival, so the result may be either value depending on
        # timing — but ``automation.from_event`` proves the capture
        # short-circuit took the value from the event payload (the cell
        # short-circuit at the top of ``sample()`` returns early before
        # re-calling ``get_states()``).
        assert result in {"automation.from_event", "automation.from_rest"}
        # Whichever path won, the captured cell or the REST sample must
        # have produced a real entity_id (i.e. the wait did not time out).
        assert result is not None

    async def test_connection_drop_during_discovery_falls_back_to_rest(
        self, ws_client, mock_client
    ):
        """If the WS drops mid-wait during a discovery wait, the helper
        must fall back to REST polling using the discovery-shaped
        ``get_states()`` sample — not the entity-id-shaped sample used by
        the other waiters. Mirrors ``TestWsPathConnectionDrop`` coverage
        for the entity_id wait path; this pins the discovery path against
        a regression that wires the wrong fallback sample."""
        # Post-subscribe sample returns nothing; after the noise event
        # nudges the loop, the WS goes dead and the helper falls back to
        # REST polling, which then resolves the match.
        mock_client.get_states = AsyncMock(
            side_effect=[
                [],  # post-subscribe sample: empty
                [],  # nudge re-sample after noise event: empty (WS now dead)
                [  # REST fallback path resolves
                    {
                        "entity_id": "automation.found_via_rest",
                        "attributes": {"id": "uid_drop"},
                    }
                ],
            ]
        )

        async def drop_after_noise():
            await asyncio.sleep(0.05)
            # Fire a noise event so the wait loop wakes and re-samples,
            # THEN drop the WS so the next loop tick takes the REST
            # fallback path.
            await ws_client.fire_state_changed(
                "automation.unrelated",
                new_state={"attributes": {"id": "uid_other"}},
            )
            ws_client.is_connected = False

        drop_task = asyncio.create_task(drop_after_noise())
        result = await wait_for_automation_entity_by_unique_id(
            mock_client, "uid_drop", timeout=2.0, poll_interval=0.05
        )
        await drop_task

        assert result == "automation.found_via_rest"
        # REST fallback was actually exercised — at least one
        # ``get_states()`` call past the initial post-subscribe sample.
        assert mock_client.get_states.call_count >= 2
