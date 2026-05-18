"""Unit tests for ``HomeAssistantClient.get_error_log`` three-way branch.

The three branches:

- **Addon context** (``is_running_in_addon()`` True) — Supervisor REST.
  Covered by existing tests for ``_supervisor_logs_get``; we just
  regression-check that the branch is still entered.
- **External → Supervised/HAOS** (probe of ``/api/config`` returns
  ``"hassio" in components``) — HA Core hassio proxy at
  ``/api/hassio/core/logs``. New branch added in #1349 item 4 fix.
- **External → Container/pip** (no hassio in components) — historical
  ``/api/error_log`` path, now using ``_raw_request`` so plain-text
  responses aren't lossily JSON-parsed.

Also covers the ``_is_supervised_install`` cache invariant: both
positive and negative outcomes of a successful probe are cached
(definitive signals), only probe FAILURES leave the cache unset so the
next call can re-probe.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)


@pytest.fixture
def client():
    """``HomeAssistantClient`` with stubbed internals — no real network.

    Mirrors the fixture pattern in ``test_tools_utility_supervisor_logs.py``;
    sets every attribute the real ``__init__`` sets so production code can
    use direct attribute access (no defensive ``getattr`` needed to paper
    over a test-fixture omission).
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
async def test_is_supervised_returns_false_when_hassio_absent_and_caches(client):
    """`hassio` absent → False, CACHED (definitive non-supervised signal).

    A successful /api/config response that doesn't list hassio is a
    definitive Container/pip signal — caching it avoids re-probing on
    every subsequent get_error_log call. Only probe FAILURES leave the
    cache unset (see test_is_supervised_fails_open_on_probe_error).
    """
    client._request = AsyncMock(return_value={"components": ["sun", "demo"]})
    assert await client._is_supervised_install() is False
    assert client._supervised_detected is False
    # Second call must NOT re-probe — cached negative is reused.
    assert await client._is_supervised_install() is False
    assert client._request.await_count == 1


@pytest.mark.asyncio
async def test_is_supervised_caches_positive_result(client):
    """One probe per session once True — repeated calls don't re-GET /config."""
    client._request = AsyncMock(return_value={"components": ["hassio"]})
    assert await client._is_supervised_install() is True
    assert await client._is_supervised_install() is True
    assert await client._is_supervised_install() is True
    assert client._request.await_count == 1


@pytest.mark.asyncio
async def test_is_supervised_fails_open_on_probe_error(client):
    """Probe transport-layer error → False, NOT cached (next call re-probes)."""
    client._request = AsyncMock(side_effect=httpx.ConnectError("boom"))
    assert await client._is_supervised_install() is False
    assert client._supervised_detected is None


@pytest.mark.asyncio
async def test_is_supervised_fails_open_on_ha_api_error(client):
    """Probe HomeAssistantAPIError (non-2xx) → False, not cached."""
    client._request = AsyncMock(
        side_effect=HomeAssistantAPIError("503 Service Unavailable")
    )
    assert await client._is_supervised_install() is False
    assert client._supervised_detected is None


@pytest.mark.asyncio
async def test_is_supervised_propagates_runtime_bugs(client):
    """Programming errors (TypeError, etc.) must NOT be swallowed by fail-open.

    Narrow-except contract: only catch HTTP/transport/auth layer errors;
    runtime bugs like ``TypeError`` from a misshaped mock or
    ``AttributeError`` from a misuse signal real issues and must surface
    loudly.
    """
    client._request = AsyncMock(side_effect=TypeError("oops"))
    with pytest.raises(TypeError):
        await client._is_supervised_install()


@pytest.mark.asyncio
async def test_is_supervised_handles_unexpected_response_shape(client):
    """Probe returns dict without `components` key → False (cached).

    ``_request`` returns ``{}`` on a JSON-empty response (rest_client.py
    line 240); that's the realistic edge case. The branch defensively
    handles missing or wrong-typed ``components`` without raising.
    """
    client._request = AsyncMock(return_value={})
    assert await client._is_supervised_install() is False
    # Empty-dict response IS a successful probe — counts as definitive non-supervised.
    assert client._supervised_detected is False

    # Wrong-typed components — also fail-closed without raising.
    client2_response = {"components": "not a list"}
    client._supervised_detected = None
    client._request = AsyncMock(return_value=client2_response)
    assert await client._is_supervised_install() is False
    assert client._supervised_detected is False


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
    # /api/config probe (1) + /hassio/core/logs fetch (1).
    assert client._request.await_count == 1
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
    """External + hassio NOT loaded → ``/error_log`` via _raw_request.

    The Container/pip branch must use ``_raw_request`` so the plain-text
    response from ``/api/error_log`` reaches the caller verbatim. The
    older implementation used ``_request`` which JSON-parses the body and
    silently returned ``"{}"`` on the JSONDecodeError fallback — fixed
    by the Gemini-flagged HIGH-priority bug in PR #1360.
    """
    container_response = SimpleNamespace(text="real container log line\n")
    client._request = AsyncMock(return_value={"components": ["sun", "demo"]})
    client._raw_request = AsyncMock(return_value=container_response)
    client._supervisor_logs_get = AsyncMock()

    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
        result = await client.get_error_log()

    assert result == "real container log line\n"
    # Probe via _request, log fetch via _raw_request — symmetry with supervised branch.
    client._request.assert_awaited_once_with("GET", "/config")
    client._raw_request.assert_awaited_once_with(
        "GET", "/error_log", headers={"Accept": "text/plain"}
    )
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
    assert client._request.await_count == 1
    # /hassio/core/logs fetched on every call (no caching of log content).
    assert client._raw_request.await_count == 3


@pytest.mark.asyncio
async def test_get_error_log_caches_negative_supervised_flag(client):
    """Container HA: probe runs once across N get_error_log calls.

    Definitive non-supervised signal is cached, so subsequent calls go
    straight to /error_log without re-probing /api/config. Avoids the
    extra round-trip Gemini flagged as a MEDIUM efficiency issue.
    """
    container_response = SimpleNamespace(text="log content\n")
    client._request = AsyncMock(return_value={"components": ["sun", "demo"]})
    client._raw_request = AsyncMock(return_value=container_response)

    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
        await client.get_error_log()
        await client.get_error_log()
        await client.get_error_log()

    # /api/config probe fired exactly once.
    assert client._request.await_count == 1
    # /error_log fetched on every call.
    assert client._raw_request.await_count == 3


@pytest.mark.asyncio
async def test_get_error_log_supervised_probe_failure_falls_to_container(client):
    """Probe transport failure on first call drops to the Container branch.

    Fail-open contract: a transient /api/config failure must not break
    get_error_log entirely. It falls through to the historical
    /error_log path. (On HAOS that path then 404s — same status quo
    as before the fix, but with a clearer error than swallowing the
    probe exception would surface.) The probe failure is NOT cached, so
    the next call re-probes.
    """
    container_response = SimpleNamespace(text="container fallback log\n")
    client._request = AsyncMock(side_effect=HomeAssistantConnectionError("boom"))
    client._raw_request = AsyncMock(return_value=container_response)

    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
        result = await client.get_error_log()

    assert result == "container fallback log\n"
    # Probe was attempted, then /error_log fetched via _raw_request.
    client._request.assert_awaited_once_with("GET", "/config")
    client._raw_request.assert_awaited_once_with(
        "GET", "/error_log", headers={"Accept": "text/plain"}
    )
    # Probe failure must NOT have poisoned the cache.
    assert client._supervised_detected is None
