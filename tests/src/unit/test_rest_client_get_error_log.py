"""Unit tests for ``HomeAssistantClient.get_error_log`` three-way branch.

The three branches:

- **Addon context** (``is_running_in_addon()`` True) — Supervisor REST.
  Covered by existing tests for ``_supervisor_logs_get``; we just
  regression-check that the branch is still entered.
- **External → Supervised/HAOS** (probe of ``/api/config`` returns
  ``"hassio" in components``) — HA Core hassio proxy at
  ``/api/hassio/core/logs``. New branch added in #1349 item 4 fix.
- **External → Container/pip** (no hassio in components) — historical
  ``/api/error_log`` path.

Also covers the ``_is_supervised_install`` cache invariant: positive
result cached, negative results re-probed (so a transient probe failure
doesn't permanently disable the supervised branch).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ha_mcp.client.rest_client import HomeAssistantClient


@pytest.fixture
def client():
    """``HomeAssistantClient`` with stubbed internals — no real network.

    Mirrors the fixture pattern in ``test_tools_utility_supervisor_logs.py``.
    """
    with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
        c = HomeAssistantClient()
        c.base_url = "http://test.local:8123"
        c.token = "test-token"
        c.timeout = 30
        c.verify_ssl = True
        c.httpx_client = MagicMock()
        c._supervised_detected = None
        return c


# ----- _is_supervised_install probe semantics -----


@pytest.mark.asyncio
async def test_is_supervised_returns_true_when_hassio_loaded(client):
    """`hassio` in /api/config["components"] → True, cached."""
    client._request = AsyncMock(return_value={"components": ["sun", "hassio", "demo"]})
    assert await client._is_supervised_install() is True
    assert client._supervised_detected is True


@pytest.mark.asyncio
async def test_is_supervised_returns_false_when_hassio_absent(client):
    """`hassio` absent → False, NOT cached (re-probe on next call)."""
    client._request = AsyncMock(return_value={"components": ["sun", "demo"]})
    assert await client._is_supervised_install() is False
    # Negative result MUST NOT be cached — a future call should still
    # probe (e.g. user switches install types between sessions, or this
    # call hit a transient mid-boot state where hassio hadn't loaded yet).
    assert client._supervised_detected is None


@pytest.mark.asyncio
async def test_is_supervised_caches_positive_result(client):
    """One probe per session once True — repeated calls don't re-GET /config."""
    client._request = AsyncMock(return_value={"components": ["hassio"]})
    assert await client._is_supervised_install() is True
    assert await client._is_supervised_install() is True
    assert await client._is_supervised_install() is True
    assert client._request.call_count == 1


@pytest.mark.asyncio
async def test_is_supervised_fails_open_on_probe_error(client):
    """Probe exception → False, NOT cached (so a flake doesn't poison the cache)."""
    client._request = AsyncMock(side_effect=httpx.ConnectError("boom"))
    assert await client._is_supervised_install() is False
    assert client._supervised_detected is None


@pytest.mark.asyncio
async def test_is_supervised_handles_unexpected_shape(client):
    """Probe returns non-dict or missing `components` → False without raising."""
    client._request = AsyncMock(return_value=None)  # _request can return {} on empty
    assert await client._is_supervised_install() is False

    client._request = AsyncMock(return_value={"components": "not a list"})
    assert await client._is_supervised_install() is False


# ----- get_error_log three-way branch -----


@pytest.mark.asyncio
async def test_get_error_log_addon_branch(client):
    """Addon (`is_running_in_addon()` True) → `_supervisor_logs_get('core')`."""
    client._supervisor_logs_get = AsyncMock(return_value="addon-log-content")
    client._request = AsyncMock()
    client._raw_request = AsyncMock()

    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=True):
        result = await client.get_error_log()

    assert result == "addon-log-content"
    client._supervisor_logs_get.assert_awaited_once_with("core")
    # Must NOT have touched the external-branch paths.
    client._request.assert_not_called()
    client._raw_request.assert_not_called()


@pytest.mark.asyncio
async def test_get_error_log_supervised_external_branch(client):
    """External + hassio loaded → `/hassio/core/logs` via _raw_request."""
    response = SimpleNamespace(text="supervised-log-content")
    client._raw_request = AsyncMock(return_value=response)
    client._request = AsyncMock(return_value={"components": ["hassio", "sun"]})
    client._supervisor_logs_get = AsyncMock()

    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
        result = await client.get_error_log()

    assert result == "supervised-log-content"
    # /api/config probe + /hassio/core/logs fetch.
    assert client._request.call_count == 1
    client._request.assert_awaited_with("GET", "/config")
    client._raw_request.assert_awaited_once_with(
        "GET",
        "/hassio/core/logs?lines=20000",
        headers={"Accept": "text/plain"},
    )
    # Must NOT have entered the addon branch.
    client._supervisor_logs_get.assert_not_called()


@pytest.mark.asyncio
async def test_get_error_log_container_external_branch(client):
    """External + hassio NOT loaded → historical `/error_log` path."""
    client._request = AsyncMock(
        side_effect=[
            # First call: /api/config probe — no hassio.
            {"components": ["sun", "demo"]},
            # Second call: /api/error_log — returns parsed-json fallback.
            {},
        ]
    )
    client._raw_request = AsyncMock()
    client._supervisor_logs_get = AsyncMock()

    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
        result = await client.get_error_log()

    # The historical code path on Container HA: _request returns {}, then
    # the surrounding `str(...)` coerces to "{}". This test pins the
    # existing behavior verbatim so the fix doesn't accidentally regress
    # the Container branch.
    assert result == "{}"
    # Probe then /error_log fetch.
    assert client._request.await_args_list[0].args == ("GET", "/config")
    assert client._request.await_args_list[1].args == ("GET", "/error_log")
    client._raw_request.assert_not_called()
    client._supervisor_logs_get.assert_not_called()


@pytest.mark.asyncio
async def test_get_error_log_uses_cached_supervised_flag(client):
    """Second `get_error_log` call on a supervised install reuses the cache."""
    response = SimpleNamespace(text="cached-call-log")
    client._raw_request = AsyncMock(return_value=response)
    # First call's probe returns hassio loaded; second call MUST NOT re-probe.
    client._request = AsyncMock(return_value={"components": ["hassio"]})

    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
        await client.get_error_log()
        await client.get_error_log()
        await client.get_error_log()

    # /api/config probe fired exactly once across 3 get_error_log calls.
    assert client._request.call_count == 1
    # /hassio/core/logs fetched on every call (no caching of log content).
    assert client._raw_request.call_count == 3


@pytest.mark.asyncio
async def test_get_error_log_supervised_probe_failure_falls_to_container(client):
    """Probe failure on the first call drops to the Container branch.

    Fail-open contract: a transient /api/config failure must not break
    get_error_log entirely. It falls through to the historical
    /error_log path. (On HAOS that path then 404s — same status quo
    as before the fix.) But the negative is NOT cached, so the next
    call re-probes.
    """
    client._request = AsyncMock(
        side_effect=[
            # First call's probe fails.
            httpx.ConnectError("boom"),
            # First call then hits /error_log — pretend it works.
            {},
        ]
    )
    client._raw_request = AsyncMock()

    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
        result = await client.get_error_log()

    assert result == "{}"
    # Probe was attempted, then /error_log fetched.
    calls = client._request.await_args_list
    assert calls[0].args == ("GET", "/config")
    assert calls[1].args == ("GET", "/error_log")
    # Negative-probe result must NOT have poisoned the cache.
    assert client._supervised_detected is None
