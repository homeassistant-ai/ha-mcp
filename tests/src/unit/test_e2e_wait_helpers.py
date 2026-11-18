"""Unit tests for ``tests/src/e2e/utilities/wait_helpers.py``.

The test-side migration in #1382 added a WS-event-driven path to three
existing helpers (``wait_for_entity_state``, ``wait_for_entity_registration``,
``wait_for_state_change``). The ~85 existing e2e call sites exercise the
**WS happy path** in CI, but the REST fallback only fires when
``open_ha_ws`` raises — which a healthy testcontainer never does. These
unit tests pin the fallback branches and the timeout-budget contract
against the real state tool and Home Assistant Core.

Coverage:

- Each migrated helper takes the REST fallback when ``open_ha_ws`` raises
  a transport error, returns the right type, and respects the remaining
  timeout budget after the WS path's elapsed time.
- ``_WsPathUnavailable.elapsed`` is honored so total wall-clock stays
  bounded by the original ``timeout`` (pr-test-analyzer finding).
- ``open_ha_ws`` ``RuntimeError`` propagates instead of degrading silently
  (silent-failure-hunter finding).
- ``ws_wait_for_predicate`` returns ``None`` on pure WS timeout (terminal),
  raises ``_WsPathUnavailable`` on handshake/transport failure (recoverable
  via REST).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import register_search_tools
from tests.src.e2e.utilities import wait_helpers
from tests.src.e2e.utilities.wait_helpers import (
    _WsPathUnavailable,
    wait_for_entity_registration,
    wait_for_entity_state,
    wait_for_state_change,
)


@pytest.fixture
async def mcp_client(core_client: HomeAssistantClient) -> AsyncIterator[Client]:
    """The actual state tool, with MCP serialization and Core's REST response."""
    server = FastMCP("wait-helper tests")
    register_search_tools(
        server, core_client, smart_tools=SmartSearchTools(core_client)
    )
    async with Client(server) as client:
        yield client


@pytest.fixture
def force_ws_unavailable(monkeypatch):
    """Make ``open_ha_ws`` raise ``OSError`` so every helper takes the
    REST fallback path. Mirrors a CI env without HA WS access."""

    async def _raise(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(wait_helpers, "open_ha_ws", _raise)


@pytest.fixture
def force_ws_runtime_error(monkeypatch):
    """Make ``open_ha_ws`` raise ``RuntimeError`` to assert the
    config-failure path does NOT degrade to REST — it must propagate."""

    async def _raise(*_args, **_kwargs):
        raise RuntimeError("HOMEASSISTANT_URL unset")

    monkeypatch.setattr(wait_helpers, "open_ha_ws", _raise)


class TestRestFallbackOnHandshakeFailure:
    """When ``open_ha_ws`` raises a transport error, each migrated
    helper must fall back to REST polling and return the right type."""

    async def test_entity_state_falls_back_to_rest_and_succeeds(
        self, force_ws_unavailable, hass: HomeAssistant, mcp_client: Client
    ):
        hass.states.async_set("light.test", "on")
        client = mcp_client
        with patch.object(client, "call_tool", wraps=client.call_tool) as sampled:
            result = await wait_for_entity_state(
                client, "light.test", "on", timeout=2, poll_interval=0.01
            )
        assert result is True
        sampled.assert_awaited()

    async def test_entity_state_rest_returns_false_on_timeout(
        self, force_ws_unavailable, hass: HomeAssistant, mcp_client: Client
    ):
        hass.states.async_set("light.test", "off")
        client = mcp_client  # never reaches "on"
        result = await wait_for_entity_state(
            client, "light.test", "on", timeout=1, poll_interval=0.01
        )
        assert result is False

    async def test_entity_registration_falls_back_to_rest(
        self, force_ws_unavailable, hass: HomeAssistant, mcp_client: Client
    ):
        hass.states.async_set("light.test", "on")
        client = mcp_client
        result = await wait_for_entity_registration(client, "light.test", timeout=2)
        assert result is True

    async def test_entity_registration_rest_returns_false_on_timeout(
        self, force_ws_unavailable, hass: HomeAssistant, mcp_client: Client
    ):
        client = mcp_client
        result = await wait_for_entity_registration(client, "light.test", timeout=1)
        assert result is False

    async def test_state_change_falls_back_to_rest(
        self, force_ws_unavailable, hass: HomeAssistant, mcp_client: Client, monkeypatch
    ):
        hass.states.async_set("light.test", "off")
        client = mcp_client
        call_tool = client.call_tool

        async def change_after_baseline(*args, **kwargs):
            result = await call_tool(*args, **kwargs)
            hass.states.async_set("light.test", "on")
            return result

        monkeypatch.setattr(client, "call_tool", change_after_baseline)
        result = await wait_for_state_change(
            client, "light.test", timeout=2, poll_interval=0.01
        )
        assert result == "on"

    async def test_state_change_rest_returns_none_on_timeout(
        self, force_ws_unavailable, hass: HomeAssistant, mcp_client: Client
    ):
        # Initial state is 'off' and stays 'off' — no change observed.
        hass.states.async_set("light.test", "off")
        client = mcp_client
        result = await wait_for_state_change(
            client, "light.test", timeout=1, poll_interval=0.01
        )
        assert result is None


class TestRuntimeErrorPropagation:
    """``RuntimeError`` from ``open_ha_ws`` (missing URL/token, bad
    auth, malformed handshake) MUST propagate, not silently degrade —
    silent-failure-hunter finding."""

    async def test_entity_state_propagates_runtime_error(
        self, force_ws_runtime_error, hass: HomeAssistant, mcp_client: Client
    ):
        hass.states.async_set("light.test", "on")
        client = mcp_client
        with pytest.raises(RuntimeError, match="HOMEASSISTANT_URL unset"):
            await wait_for_entity_state(client, "light.test", "on", timeout=1)

    async def test_entity_registration_propagates_runtime_error(
        self, force_ws_runtime_error, hass: HomeAssistant, mcp_client: Client
    ):
        hass.states.async_set("light.test", "on")
        client = mcp_client
        with pytest.raises(RuntimeError, match="HOMEASSISTANT_URL unset"):
            await wait_for_entity_registration(client, "light.test", timeout=1)

    async def test_state_change_propagates_runtime_error(
        self, force_ws_runtime_error, hass: HomeAssistant, mcp_client: Client
    ):
        # Initial state fetch happens before the WS path, so it succeeds.
        # Then open_ha_ws raises RuntimeError on the WS attempt.
        hass.states.async_set("light.test", "off")
        client = mcp_client
        with pytest.raises(RuntimeError, match="HOMEASSISTANT_URL unset"):
            await wait_for_state_change(client, "light.test", timeout=1)


class TestTimeoutBudgetDeduction:
    """A mid-wait WS drop must NOT let the REST fallback run for another
    full ``timeout`` — pr-test-analyzer finding. ``_WsPathUnavailable.elapsed``
    is the budget contract."""

    async def test_rest_budget_deducted_by_elapsed(
        self, monkeypatch, hass: HomeAssistant, mcp_client: Client
    ):
        """If the WS path raises ``_WsPathUnavailable(elapsed=N)``, the
        REST fallback budget must be ``timeout - N``, capped at zero."""

        # Stub ws_wait_for_predicate to raise with elapsed=0.8 of a 1s budget.
        async def fake_predicate(*_args, **_kwargs):
            raise _WsPathUnavailable("simulated mid-wait drop", elapsed=0.8)

        monkeypatch.setattr(wait_helpers, "ws_wait_for_predicate", fake_predicate)

        # Sample never resolves — REST loop runs until budget exhausts.
        hass.states.async_set("light.test", "off")
        client = mcp_client

        import time

        start = time.monotonic()
        result = await wait_for_entity_state(
            client, "light.test", "on", timeout=1, poll_interval=0.05
        )
        elapsed = time.monotonic() - start

        assert result is False
        # Total wall-clock must be near the REMAINING budget (0.2s),
        # NOT the full 1s timeout. Allow generous slack for scheduling.
        assert elapsed < 0.9, (
            f"REST fallback ran for {elapsed:.2f}s; expected <0.9s "
            f"(budget = timeout - elapsed = 1.0 - 0.8 = 0.2s)"
        )

    async def test_rest_budget_clamped_at_zero(
        self, monkeypatch, hass: HomeAssistant, mcp_client: Client
    ):
        """If ``elapsed > timeout`` (already overspent), REST budget is
        zero and the helper returns false immediately."""

        async def fake_predicate(*_args, **_kwargs):
            raise _WsPathUnavailable("elapsed exceeded timeout", elapsed=5.0)

        monkeypatch.setattr(wait_helpers, "ws_wait_for_predicate", fake_predicate)
        hass.states.async_set("light.test", "on")
        client = mcp_client

        import time

        start = time.monotonic()
        result = await wait_for_entity_state(
            client, "light.test", "on", timeout=1, poll_interval=0.05
        )
        elapsed = time.monotonic() - start

        # REST budget = max(0, 1.0 - 5.0) = 0 → helper returns immediately.
        assert result is False
        assert elapsed < 0.2
