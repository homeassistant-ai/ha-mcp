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
        self._subscribe_should_raise: Exception | None = None

    def add_event_handler(self, event_type: str, handler) -> None:
        self.handlers.setdefault(event_type, []).append(handler)

    def remove_event_handler(self, event_type: str, handler) -> None:
        if event_type in self.handlers and handler in self.handlers[event_type]:
            self.handlers[event_type].remove(handler)

    async def subscribe_events(self, event_type: str) -> int:
        if self._subscribe_should_raise is not None:
            raise self._subscribe_should_raise
        self.subscribed.append(event_type)
        self._next_sub += 1
        return self._next_sub

    async def unsubscribe_events(self, sub_id: int) -> None:
        self.unsubscribed.append(sub_id)

    async def fire_state_changed(self, entity_id: str) -> None:
        """Dispatch a state_changed event to every registered handler."""
        for handler in list(self.handlers.get("state_changed", [])):
            await handler(
                {"event_type": "state_changed", "data": {"entity_id": entity_id}}
            )


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
        """If subscribe_events raises, we degrade to the legacy REST poll."""
        ws_client._subscribe_should_raise = RuntimeError("subscribe down")
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
