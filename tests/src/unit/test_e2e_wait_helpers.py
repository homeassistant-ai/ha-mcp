"""Unit tests for ``tests/src/e2e/utilities/wait_helpers.py``.

The test-side migration in #1382 added a WS-event-driven path to three
existing helpers (``wait_for_entity_state``, ``wait_for_entity_registration``,
``wait_for_state_change``). The ~85 existing e2e call sites exercise the
**WS happy path** in CI, but the REST fallback only fires when
``_open_ha_ws`` raises — which a healthy testcontainer never does. These
unit tests pin the fallback branches and the timeout-budget contract
without standing up a real HA.

Coverage:

- Each migrated helper takes the REST fallback when ``_open_ha_ws`` raises
  a transport error, returns the right type, and respects the remaining
  timeout budget after the WS path's elapsed time.
- ``_WsPathUnavailable.elapsed`` is honored so total wall-clock stays
  bounded by the original ``timeout`` (pr-test-analyzer finding).
- ``_open_ha_ws`` ``RuntimeError`` propagates instead of degrading silently
  (silent-failure-hunter finding).
- ``_ws_wait_for_predicate`` returns ``None`` on pure WS timeout (terminal),
  raises ``_WsPathUnavailable`` on handshake/transport failure (recoverable
  via REST).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from src.e2e.utilities import wait_helpers
from src.e2e.utilities.wait_helpers import (
    _WsPathUnavailable,
    wait_for_entity_registration,
    wait_for_entity_state,
    wait_for_state_change,
)


def _mcp_result(text: str) -> MagicMock:
    """Build an MCP ``call_tool`` result that ``parse_mcp_result`` will
    decode as ``json.loads(text)``. ``isError`` must be ``False``
    explicitly — a bare ``MagicMock()`` would return a truthy MagicMock
    from attribute access and route the parser down the error path."""
    text_part = MagicMock()
    text_part.text = text
    result = MagicMock()
    result.isError = False
    result.content = [text_part]
    return result


def _mcp_client_returning(state_value: str | None) -> MagicMock:
    """Build an MCP-client double whose ``call_tool('ha_get_state', ...)``
    returns a parsed-result wrapper with the given state value (or
    ``None`` to simulate a missing entity)."""
    client = MagicMock()
    if state_value is None:
        wrapper = _mcp_result('{"data": null}')
    else:
        wrapper = _mcp_result(
            f'{{"data": {{"entity_id": "x", "state": "{state_value}"}}}}'
        )
    client.call_tool = AsyncMock(return_value=wrapper)
    return client


@pytest.fixture
def force_ws_unavailable(monkeypatch):
    """Make ``_open_ha_ws`` raise ``OSError`` so every helper takes the
    REST fallback path. Mirrors a CI env without HA WS access."""

    async def _raise(*_args, **_kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(wait_helpers, "_open_ha_ws", _raise)


@pytest.fixture
def force_ws_runtime_error(monkeypatch):
    """Make ``_open_ha_ws`` raise ``RuntimeError`` to assert the
    config-failure path does NOT degrade to REST — it must propagate."""

    async def _raise(*_args, **_kwargs):
        raise RuntimeError("HOMEASSISTANT_URL unset")

    monkeypatch.setattr(wait_helpers, "_open_ha_ws", _raise)


class TestWsPathUnavailableContract:
    """``_WsPathUnavailable`` carries elapsed for budget deduction."""

    def test_default_elapsed_is_zero(self):
        e = _WsPathUnavailable("handshake failed")
        assert e.elapsed == 0.0

    def test_elapsed_kwarg_preserved(self):
        e = _WsPathUnavailable("mid-wait drop", elapsed=3.5)
        assert e.elapsed == 3.5


class TestRestFallbackOnHandshakeFailure:
    """When ``_open_ha_ws`` raises a transport error, each migrated
    helper must fall back to REST polling and return the right type."""

    async def test_entity_state_falls_back_to_rest_and_succeeds(
        self, force_ws_unavailable
    ):
        client = _mcp_client_returning("on")
        result = await wait_for_entity_state(
            client, "light.test", "on", timeout=2, poll_interval=0.01
        )
        assert result is True
        # Sample called at least once via REST.
        assert client.call_tool.call_count >= 1

    async def test_entity_state_rest_returns_false_on_timeout(
        self, force_ws_unavailable
    ):
        client = _mcp_client_returning("off")  # never reaches "on"
        result = await wait_for_entity_state(
            client, "light.test", "on", timeout=1, poll_interval=0.01
        )
        assert result is False

    async def test_entity_registration_falls_back_to_rest(self, force_ws_unavailable):
        client = _mcp_client_returning("on")
        result = await wait_for_entity_registration(client, "light.test", timeout=2)
        assert result is True

    async def test_entity_registration_rest_returns_false_on_timeout(
        self, force_ws_unavailable
    ):
        client = _mcp_client_returning(None)
        result = await wait_for_entity_registration(client, "light.test", timeout=1)
        assert result is False

    async def test_state_change_falls_back_to_rest(self, force_ws_unavailable):
        # Sequence: initial 'off' (capture baseline), then 'on' (change detected)
        client = MagicMock()
        client.call_tool = AsyncMock(
            side_effect=[
                _mcp_result('{"data": {"entity_id": "x", "state": "off"}}'),
                _mcp_result('{"data": {"entity_id": "x", "state": "on"}}'),
            ]
        )
        result = await wait_for_state_change(
            client, "light.test", timeout=2, poll_interval=0.01
        )
        assert result == "on"

    async def test_state_change_rest_returns_none_on_timeout(
        self, force_ws_unavailable
    ):
        # Initial state is 'off' and stays 'off' — no change observed.
        client = _mcp_client_returning("off")
        result = await wait_for_state_change(
            client, "light.test", timeout=1, poll_interval=0.01
        )
        assert result is None


class TestRuntimeErrorPropagation:
    """``RuntimeError`` from ``_open_ha_ws`` (missing URL/token, bad
    auth, malformed handshake) MUST propagate, not silently degrade —
    silent-failure-hunter finding."""

    async def test_entity_state_propagates_runtime_error(self, force_ws_runtime_error):
        client = _mcp_client_returning("on")
        with pytest.raises(RuntimeError, match="HOMEASSISTANT_URL unset"):
            await wait_for_entity_state(client, "light.test", "on", timeout=1)

    async def test_entity_registration_propagates_runtime_error(
        self, force_ws_runtime_error
    ):
        client = _mcp_client_returning("on")
        with pytest.raises(RuntimeError, match="HOMEASSISTANT_URL unset"):
            await wait_for_entity_registration(client, "light.test", timeout=1)

    async def test_state_change_propagates_runtime_error(self, force_ws_runtime_error):
        # Initial state fetch happens before the WS path, so it succeeds.
        # Then _open_ha_ws raises RuntimeError on the WS attempt.
        client = MagicMock()
        client.call_tool = AsyncMock(
            return_value=_mcp_result('{"data": {"entity_id": "x", "state": "off"}}')
        )
        with pytest.raises(RuntimeError, match="HOMEASSISTANT_URL unset"):
            await wait_for_state_change(client, "light.test", timeout=1)


class TestTimeoutBudgetDeduction:
    """A mid-wait WS drop must NOT let the REST fallback run for another
    full ``timeout`` — pr-test-analyzer finding. ``_WsPathUnavailable.elapsed``
    is the budget contract."""

    async def test_rest_budget_deducted_by_elapsed(self, monkeypatch):
        """If the WS path raises ``_WsPathUnavailable(elapsed=N)``, the
        REST fallback budget must be ``timeout - N``, capped at zero."""

        # Stub _ws_wait_for_predicate to raise with elapsed=0.8 of a 1s budget.
        async def fake_predicate(*_args, **_kwargs):
            raise _WsPathUnavailable("simulated mid-wait drop", elapsed=0.8)

        monkeypatch.setattr(wait_helpers, "_ws_wait_for_predicate", fake_predicate)

        # Sample never resolves — REST loop runs until budget exhausts.
        client = _mcp_client_returning("off")

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

    async def test_rest_budget_clamped_at_zero(self, monkeypatch):
        """If ``elapsed > timeout`` (already overspent), REST budget is
        zero and the helper returns false immediately."""

        async def fake_predicate(*_args, **_kwargs):
            raise _WsPathUnavailable("elapsed exceeded timeout", elapsed=5.0)

        monkeypatch.setattr(wait_helpers, "_ws_wait_for_predicate", fake_predicate)
        client = _mcp_client_returning("on")

        import time

        start = time.monotonic()
        result = await wait_for_entity_state(
            client, "light.test", "on", timeout=1, poll_interval=0.05
        )
        elapsed = time.monotonic() - start

        # REST budget = max(0, 1.0 - 5.0) = 0 → helper returns immediately.
        assert result is False
        assert elapsed < 0.2
