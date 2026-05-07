"""Unit tests for `ha_get_logs(source="supervisor"|"system_service")`.

Covers two REST-client paths and their tools_utility wrappers:

- `HomeAssistantClient.get_addon_logs()` — branches on `is_running_in_addon()`:
  inside the addon container hits Supervisor directly at
  `http://supervisor/addons/{slug}/logs` (the HA-Core proxy rejects the
  Supervisor token there — see #1116); otherwise falls back to
  `/api/hassio/addons/{slug}/logs` (returned as text/plain — see #950).
- `HomeAssistantClient._get_system_service_logs()` — fetches HA-Supervisor
  system-service logs at `http://supervisor/{service}/logs` for
  service ∈ {supervisor, host, core, dns, audio, multicast, observer} (#1116
  scope-add).
- The wrappers (`_get_supervisor_log`, `_get_system_service_log`) — response
  shape, tail slicing, search filter, slug-enum validation, and structured-
  error translation.
"""

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_utility import register_utility_tools


@pytest.fixture
def mock_client():
    """HomeAssistantClient with stubbed internals — no real network."""
    with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
        client = HomeAssistantClient()
        client.base_url = "http://test.local:8123"
        client.token = "test-token"
        client.timeout = 30
        client.verify_ssl = True
        client.httpx_client = MagicMock()
        return client


@pytest.fixture
def non_addon_install():
    """Force `is_running_in_addon()` False (HA-Core-proxy path)."""
    with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
        yield


@pytest.fixture
def addon_install():
    """Force `is_running_in_addon()` True with a stubbed SUPERVISOR_TOKEN."""
    with (
        patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=True),
        patch.dict("os.environ", {"SUPERVISOR_TOKEN": "supervisor-token-test"}),
    ):
        yield


@pytest.fixture
def addon_install_no_token():
    """`is_running_in_addon()` True but SUPERVISOR_TOKEN deliberately empty.

    Models the detection/config mismatch that triggers the fail-fast path in
    `_supervisor_logs_get` (gate fired but env var not actually set).
    """
    with (
        patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=True),
        patch.dict("os.environ", {"SUPERVISOR_TOKEN": ""}, clear=False),
    ):
        yield


def _register_and_collect(client: Any) -> dict[str, Any]:
    """Register utility tools on a collector mcp and return the registered tools.

    The production decorator chain is ``@mcp.tool(...)`` outside ``@log_tool_usage``,
    so the collected entry is the ``log_tool_usage``-wrapped async function.
    """
    collected: dict[str, Any] = {}

    def _tool(**_kwargs: Any) -> Any:
        def _wrap(fn: Any) -> Any:
            collected[fn.__name__] = fn
            return fn

        return _wrap

    mcp = SimpleNamespace(tool=_tool)
    register_utility_tools(mcp, client)
    return collected


def _parse_tool_error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    """Parse the JSON payload from a ToolError raised by a tool."""
    payload: dict[str, Any] = json.loads(str(exc_info.value))
    return payload


class TestGetAddonLogs:
    """Tests for the REST-client `get_addon_logs` method on non-addon installs.

    These exercise the HA-Core-proxy fallback branch (`/hassio/addons/{slug}/logs`)
    via `httpx_client.request`. The `non_addon_install` fixture forces
    `is_running_in_addon()` False so `get_addon_logs` doesn't take the
    Supervisor-direct branch — that path opens a fresh `httpx.AsyncClient`
    and would bypass the `mock_client.httpx_client` mock entirely.
    """

    @pytest.fixture(autouse=True)
    def _force_non_addon(self, non_addon_install):
        """Apply `non_addon_install` to every test in this class."""
        yield

    @pytest.mark.asyncio
    async def test_returns_text_on_200(self, mock_client):
        """Successful 200 response returns the raw text body."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "2026-04-11 10:00:00 addon starting\nready\n"
        mock_client.httpx_client.request = AsyncMock(return_value=mock_response)

        result = await mock_client.get_addon_logs("core_mosquitto")

        assert "addon starting" in result
        assert "ready" in result

    @pytest.mark.asyncio
    async def test_calls_correct_endpoint_with_text_accept(self, mock_client):
        """Endpoint path and Accept: text/plain header must match the HA proxy contract."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = ""
        mock_client.httpx_client.request = AsyncMock(return_value=mock_response)

        await mock_client.get_addon_logs("81f33d0f_ha_mcp_dev")

        mock_client.httpx_client.request.assert_called_once()
        args, kwargs = mock_client.httpx_client.request.call_args
        assert args[0] == "GET"
        assert args[1] == "/hassio/addons/81f33d0f_ha_mcp_dev/logs"
        assert kwargs["headers"]["Accept"] == "text/plain"

    @pytest.mark.asyncio
    async def test_raises_auth_error_on_401(self, mock_client):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "unauthorized"
        mock_client.httpx_client.request = AsyncMock(return_value=mock_response)

        with pytest.raises(HomeAssistantAuthError):
            await mock_client.get_addon_logs("core_mosquitto")

    @pytest.mark.asyncio
    async def test_raises_api_error_on_404_with_slug_context(self, mock_client):
        """404 (unknown slug) raises HomeAssistantAPIError with status 404 and body.

        The Supervisor-proxied endpoint returns `text/plain` error bodies, not
        JSON, so `response.json()` raises and the error message falls back to
        `response.text`. Mirror that here.
        """
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Addon is not installed"
        mock_response.json = MagicMock(side_effect=ValueError("not json"))
        mock_client.httpx_client.request = AsyncMock(return_value=mock_response)

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.get_addon_logs("nonexistent_slug")

        assert exc_info.value.status_code == 404
        assert "Addon is not installed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_connection_error_on_network_failure(self, mock_client):
        mock_client.httpx_client.request = AsyncMock(
            side_effect=httpx.ConnectError("no route")
        )

        with pytest.raises(HomeAssistantConnectionError):
            await mock_client.get_addon_logs("core_mosquitto")

    @pytest.mark.asyncio
    async def test_raises_connection_error_on_timeout(self, mock_client):
        mock_client.httpx_client.request = AsyncMock(
            side_effect=httpx.TimeoutException("timeout")
        )

        with pytest.raises(HomeAssistantConnectionError):
            await mock_client.get_addon_logs("core_mosquitto")

    @pytest.mark.asyncio
    async def test_does_not_parse_json(self, mock_client):
        """Regression guard for #950: the fetch must not try to JSON-decode the
        text/plain log body (that's what broke the old websocket path)."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "plain log line 1\nplain log line 2\n"
        # Make .json() raise so any stray call would fail the test.
        mock_response.json = MagicMock(
            side_effect=ValueError("json parse should not be called")
        )
        mock_client.httpx_client.request = AsyncMock(return_value=mock_response)

        result = await mock_client.get_addon_logs("core_mosquitto")

        assert "plain log line 1" in result
        mock_response.json.assert_not_called()


class TestGetAddonLogsViaSupervisor:
    """Supervisor-direct branch of `get_addon_logs` (#1116 regression scope)."""

    @pytest.fixture
    def mock_async_client_class(self):
        inner_client = MagicMock()
        inner_client.get = AsyncMock()

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=inner_client)
        cm.__aexit__ = AsyncMock(return_value=None)

        client_class = MagicMock(return_value=cm)
        # Patching `httpx.AsyncClient` directly (not through the `rest_client`
        # module attribute) is robust to either `import httpx` or a future
        # `from httpx import AsyncClient` form (#1126 review item 15).
        with patch("httpx.AsyncClient", client_class):
            yield inner_client, client_class

    @pytest.mark.asyncio
    async def test_uses_direct_supervisor_url_and_supervisor_token(
        self, mock_client, addon_install, mock_async_client_class
    ):
        inner_client, client_class = mock_async_client_class
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "addon log line 1\nready\n"
        inner_client.get.return_value = mock_response

        result = await mock_client.get_addon_logs("81f33d0f_ha_mcp")

        assert "addon log line 1" in result
        inner_client.get.assert_awaited_once()
        args, kwargs = inner_client.get.call_args
        assert args[0] == "http://supervisor/addons/81f33d0f_ha_mcp/logs"
        assert kwargs["headers"]["Authorization"] == "Bearer supervisor-token-test"
        assert kwargs["headers"]["Accept"] == "text/plain"
        # Constructor kwargs (verify_ssl + timeout) propagated from the client
        # instance — guards against a regression that hard-codes either
        # (#1126 review item 13).
        ctor_kwargs = client_class.call_args.kwargs
        assert ctor_kwargs["verify"] is True  # mirrors mock_client.verify_ssl
        assert isinstance(ctor_kwargs["timeout"], httpx.Timeout)
        # The HA-Core-proxy path must NOT have been touched.
        mock_client.httpx_client.request.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_auth_error_on_401(
        self, mock_client, addon_install, mock_async_client_class
    ):
        inner_client, _ = mock_async_client_class
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "unauthorized"
        inner_client.get.return_value = mock_response

        with pytest.raises(HomeAssistantAuthError):
            await mock_client.get_addon_logs("core_mosquitto")

    @pytest.mark.asyncio
    async def test_raises_auth_error_on_empty_supervisor_token(
        self, mock_client, addon_install_no_token, mock_async_client_class
    ):
        """Gate fires but SUPERVISOR_TOKEN is empty → fail-fast with a distinct
        message so it doesn't read as "token rejected" (#1126 review item 1)."""
        inner_client, _ = mock_async_client_class

        with pytest.raises(HomeAssistantAuthError) as exc_info:
            await mock_client.get_addon_logs("core_mosquitto")

        assert "absent at call time" in str(exc_info.value)
        assert "SUPERVISOR_TOKEN" in str(exc_info.value)
        # No HTTP request must have been issued — fail-fast happens before.
        inner_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_api_error_on_403_with_role_hint(
        self, mock_client, addon_install, mock_async_client_class, caplog
    ):
        """403 distinct from 401: addon's hassio_role too low. Surfaces with a
        role-hint suggestion + warning log so operators don't read this as a
        token-validity problem (#1126 review items 2 + 9)."""
        inner_client, _ = mock_async_client_class
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = ""
        mock_response.reason_phrase = "Forbidden"
        inner_client.get.return_value = mock_response

        import logging

        with (
            caplog.at_level(logging.WARNING, logger="ha_mcp.client.rest_client"),
            pytest.raises(HomeAssistantAPIError) as exc_info,
        ):
            await mock_client.get_addon_logs("core_mosquitto")

        assert exc_info.value.status_code == 403
        msg = str(exc_info.value)
        assert "hassio_role" in msg and "manager" in msg
        # Warning log fired before the raise (#1126 review item 9).
        assert any("403" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_raises_api_error_on_404(
        self, mock_client, addon_install, mock_async_client_class
    ):
        inner_client, _ = mock_async_client_class
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Addon is not installed"
        mock_response.reason_phrase = "Not Found"
        inner_client.get.return_value = mock_response

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.get_addon_logs("nonexistent_slug")

        assert exc_info.value.status_code == 404
        assert "Addon is not installed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_parses_supervisor_json_envelope(
        self, mock_client, addon_install, mock_async_client_class
    ):
        """Supervisor's `{"result":"error","message":"..."}` envelope is parsed
        first — user-facing message gets the human prose, not the JSON blob
        (#1126 review item 6)."""
        inner_client, _ = mock_async_client_class
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = '{"result":"error","message":"Add-on is not running"}'
        mock_response.reason_phrase = "Bad Request"
        inner_client.get.return_value = mock_response

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.get_addon_logs("core_mosquitto")

        assert exc_info.value.status_code == 400
        msg = str(exc_info.value)
        assert "Add-on is not running" in msg
        # The full JSON blob must NOT be in the user-facing message.
        assert '{"result"' not in msg

    @pytest.mark.asyncio
    async def test_empty_body_falls_back_to_reason_phrase(
        self, mock_client, addon_install, mock_async_client_class
    ):
        inner_client, _ = mock_async_client_class
        mock_response = MagicMock()
        mock_response.status_code = 502
        mock_response.text = ""
        mock_response.reason_phrase = "Bad Gateway"
        inner_client.get.return_value = mock_response

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.get_addon_logs("core_mosquitto")

        assert exc_info.value.status_code == 502
        assert "Bad Gateway" in str(exc_info.value)
        assert not str(exc_info.value).endswith(" - ")

    @pytest.mark.asyncio
    async def test_empty_body_no_reason_phrase_uses_placeholder(
        self, mock_client, addon_install, mock_async_client_class
    ):
        """Tier-3 fallback parity for the supervisor branch: empty body AND
        empty reason_phrase → `<empty body>` placeholder (#1126 review item 12).
        `TestRawRequestEmptyBodyFallback` covers this for the proxy branch."""
        inner_client, _ = mock_async_client_class
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.text = ""
        mock_response.reason_phrase = ""
        inner_client.get.return_value = mock_response

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.get_addon_logs("core_mosquitto")

        assert exc_info.value.status_code == 503
        assert "<empty body>" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_connection_error_on_timeout_with_distinct_message(
        self, mock_client, addon_install, mock_async_client_class
    ):
        """Timeout vs transport error get distinct messages so callers (and
        log-watchers) can tell them apart (#1126 review item 7)."""
        inner_client, _ = mock_async_client_class
        inner_client.get.side_effect = httpx.TimeoutException("supervisor timeout")

        with pytest.raises(HomeAssistantConnectionError) as exc_info:
            await mock_client.get_addon_logs("core_mosquitto")

        assert "Timeout" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_connection_error_on_network_failure_with_distinct_message(
        self, mock_client, addon_install, mock_async_client_class
    ):
        inner_client, _ = mock_async_client_class
        inner_client.get.side_effect = httpx.ConnectError("supervisor unreachable")

        with pytest.raises(HomeAssistantConnectionError) as exc_info:
            await mock_client.get_addon_logs("core_mosquitto")

        assert "Transport" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_connection_error_on_remote_protocol_error(
        self, mock_client, addon_install, mock_async_client_class
    ):
        """A non-Connect/non-Timeout subclass of `httpx.HTTPError` (e.g.
        RemoteProtocolError on partial responses) hits the broad-except clause
        — pinned so a future refactor can't silently narrow it (#1126 review
        item 11)."""
        inner_client, _ = mock_async_client_class
        inner_client.get.side_effect = httpx.RemoteProtocolError(
            "server closed connection without response"
        )

        with pytest.raises(HomeAssistantConnectionError) as exc_info:
            await mock_client.get_addon_logs("core_mosquitto")

        assert "Transport" in str(exc_info.value)


class TestGetAddonLogsBranchSelection:
    """The branch decision is made via `is_running_in_addon()`. Pin both
    directions so a future refactor of the gate (e.g. inlining the env-var
    check) doesn't silently regress one branch.
    """

    @pytest.mark.asyncio
    async def test_non_addon_install_uses_ha_core_proxy(self, mock_client):
        """`is_running_in_addon()` False → HA-Core-proxy path, no Supervisor URL."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "via proxy\n"
        mock_client.httpx_client.request = AsyncMock(return_value=mock_response)

        with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
            result = await mock_client.get_addon_logs("core_mosquitto")

        assert "via proxy" in result
        mock_client.httpx_client.request.assert_called_once()
        args, _ = mock_client.httpx_client.request.call_args
        assert args[1] == "/hassio/addons/core_mosquitto/logs"

    @pytest.mark.asyncio
    async def test_addon_install_does_not_call_ha_core_proxy(self, mock_client):
        """`is_running_in_addon()` True → HA-Core-proxy path must be skipped.

        Shrunk per #1126 review item 14: the URL/auth contract for the
        Supervisor-direct branch lives in `TestGetAddonLogsViaSupervisor`;
        this test only verifies the gate consultation. Asserting URL/auth
        here too would mock-the-mock without re-asserting anything.
        """
        inner_client = MagicMock()
        inner_client.get = AsyncMock()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = ""
        inner_client.get.return_value = mock_response

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=inner_client)
        cm.__aexit__ = AsyncMock(return_value=None)

        with (
            patch(
                "ha_mcp.client.rest_client.is_running_in_addon",
                return_value=True,
            ),
            patch.dict("os.environ", {"SUPERVISOR_TOKEN": "supervisor-token-branch"}),
            patch("httpx.AsyncClient", return_value=cm),
        ):
            await mock_client.get_addon_logs("core_mosquitto")

        # Sole assertion: HA-Core-proxy path NOT taken. URL/auth contract is
        # pinned by TestGetAddonLogsViaSupervisor.
        mock_client.httpx_client.request.assert_not_called()


class TestGetErrorLogBranchSelection:
    """`get_error_log()` mirrors `get_addon_logs()`'s `is_running_in_addon()`
    branch. On addon installs, HA Core's ``bootstrap.py`` sets
    ``err_log_path = None`` when the ``SUPERVISOR`` env var is present, so
    ``hass.data[DATA_LOGGING]`` is never populated and the ``APIErrorLog``
    view is not registered — ``/api/error_log`` returns 404 by-design.
    Same root cause and fix shape as #1116 add-on logs.
    """

    @pytest.mark.asyncio
    async def test_non_addon_install_uses_ha_core_proxy(self, mock_client):
        """`is_running_in_addon()` False → ``/error_log`` proxy path."""
        mock_client._request = AsyncMock(return_value="error log via proxy\n")

        with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=False):
            result = await mock_client.get_error_log()

        assert "error log via proxy" in result
        mock_client._request.assert_called_once_with("GET", "/error_log")

    @pytest.mark.asyncio
    async def test_addon_install_routes_to_supervisor_core(self, mock_client):
        """`is_running_in_addon()` True → ``_supervisor_logs_get("core")``.

        The HA-Core-proxy ``/error_log`` path must NOT be called: HA Core
        doesn't register ``APIErrorLog`` when running under Supervisor.
        """
        mock_client._request = AsyncMock()
        mock_client._supervisor_logs_get = AsyncMock(
            return_value="error log via supervisor\n"
        )

        with patch("ha_mcp.client.rest_client.is_running_in_addon", return_value=True):
            result = await mock_client.get_error_log()

        assert "error log via supervisor" in result
        mock_client._supervisor_logs_get.assert_called_once_with("core")
        mock_client._request.assert_not_called()


class TestGetSystemServiceLogs:
    """REST-client `_get_system_service_logs` — system-service variant of the
    Supervisor-direct path covering ``/{service}/logs`` (#1116 scope-add)."""

    @pytest.fixture
    def mock_async_client_class(self):
        inner_client = MagicMock()
        inner_client.get = AsyncMock()

        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=inner_client)
        cm.__aexit__ = AsyncMock(return_value=None)

        client_class = MagicMock(return_value=cm)
        with patch("httpx.AsyncClient", client_class):
            yield inner_client, client_class

    @pytest.mark.asyncio
    async def test_uses_service_url_with_supervisor_token(
        self, mock_client, addon_install, mock_async_client_class
    ):
        inner_client, client_class = mock_async_client_class
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "supervisor service log line\n"
        inner_client.get.return_value = mock_response

        result = await mock_client._get_system_service_logs("supervisor")

        assert "supervisor service log line" in result
        args, kwargs = inner_client.get.call_args
        assert args[0] == "http://supervisor/supervisor/logs"
        assert kwargs["headers"]["Authorization"] == "Bearer supervisor-token-test"
        # Constructor kwargs propagated (parity with addon-logs branch).
        ctor_kwargs = client_class.call_args.kwargs
        assert ctor_kwargs["verify"] is True
        assert isinstance(ctor_kwargs["timeout"], httpx.Timeout)

    @pytest.mark.asyncio
    async def test_raises_auth_error_on_empty_supervisor_token(
        self, mock_client, addon_install_no_token, mock_async_client_class
    ):
        """Same fail-fast as the addon-logs branch — shared helper means
        coverage extends to system_service automatically."""
        inner_client, _ = mock_async_client_class

        with pytest.raises(HomeAssistantAuthError) as exc_info:
            await mock_client._get_system_service_logs("host")

        assert "absent at call time" in str(exc_info.value)
        inner_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_api_error_on_403_with_role_hint(
        self, mock_client, addon_install, mock_async_client_class
    ):
        inner_client, _ = mock_async_client_class
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.text = ""
        mock_response.reason_phrase = "Forbidden"
        inner_client.get.return_value = mock_response

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client._get_system_service_logs("core")

        assert exc_info.value.status_code == 403
        assert "hassio_role" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_raises_api_error_on_404(
        self, mock_client, addon_install, mock_async_client_class
    ):
        """Supervisor returns 404 for unknown service paths — caller-layer
        validation already rejects unknown service names, so this is the
        fail-safe for upstream Supervisor changes."""
        inner_client, _ = mock_async_client_class
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "service unknown"
        mock_response.reason_phrase = "Not Found"
        inner_client.get.return_value = mock_response

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client._get_system_service_logs("nonexistent")

        assert exc_info.value.status_code == 404


class TestRawRequestEmptyBodyFallback:
    """Error message must stay actionable even when the 4xx body is empty.

    If `_raw_request` just used `error_data.get("message", "Unknown error")`
    when the proxy returned a blank body, the raised error read
    `"API error: 4xx - "` — same silent-failure signature #950 describes,
    one layer down.
    """

    @pytest.mark.asyncio
    async def test_empty_body_falls_back_to_reason_phrase(self, mock_client):
        mock_response = MagicMock()
        mock_response.status_code = 502
        mock_response.reason_phrase = "Bad Gateway"
        mock_response.text = ""
        mock_response.json = MagicMock(side_effect=ValueError("empty"))
        mock_client.httpx_client.request = AsyncMock(return_value=mock_response)

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client._raw_request("GET", "/anything")

        # Message must not be the bare "API error: 502 - " with an empty tail.
        assert "Bad Gateway" in str(exc_info.value)
        assert not str(exc_info.value).endswith(" - ")

    @pytest.mark.asyncio
    async def test_whitespace_only_body_falls_back(self, mock_client):
        """A whitespace-only JSON body like `{"message": "   "}` still yields an
        actionable tail, not `"API error: 4xx -    "`."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.reason_phrase = "Internal Server Error"
        mock_response.text = '{"message": "   "}'
        mock_response.json = MagicMock(return_value={"message": "   "})
        mock_client.httpx_client.request = AsyncMock(return_value=mock_response)

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client._raw_request("GET", "/anything")

        assert "Internal Server Error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_empty_body_and_no_reason_phrase_uses_placeholder(self, mock_client):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.reason_phrase = ""
        mock_response.text = ""
        mock_response.json = MagicMock(side_effect=ValueError("empty"))
        mock_client.httpx_client.request = AsyncMock(return_value=mock_response)

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client._raw_request("GET", "/anything")

        assert "<empty body>" in str(exc_info.value)


class TestGetSupervisorLogWrapper:
    """Tests for the `_get_supervisor_log` wrapper exercised via `ha_get_logs`.

    Locks down the response shape, the `[-limit:]` tail slicing, the `search`
    filter, and the `HomeAssistantAPIError → exception_to_structured_error`
    translation the REST-client tests don't cover.
    """

    @pytest.fixture
    def client_with_logs(self):
        """Client whose `get_addon_logs` is a configurable AsyncMock."""
        client = MagicMock()
        client.get_addon_logs = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_happy_path_response_shape(self, client_with_logs):
        client_with_logs.get_addon_logs.return_value = "line 1\nline 2\nline 3\n"
        tools = _register_and_collect(client_with_logs)

        result = await tools["ha_get_logs"](source="supervisor", slug="core_mosquitto")

        assert result["success"] is True
        assert result["source"] == "supervisor"
        assert result["slug"] == "core_mosquitto"
        assert result["log"] == "line 1\nline 2\nline 3"
        assert result["total_lines"] == 3
        assert result["returned_lines"] == 3
        assert "limit" in result
        # No filters applied → key is omitted
        assert "filters_applied" not in result
        client_with_logs.get_addon_logs.assert_awaited_once_with("core_mosquitto")

    @pytest.mark.asyncio
    async def test_tail_slicing_returns_last_n_lines(self, client_with_logs):
        """`lines[-effective_limit:]` — users want recent activity, not the head."""
        client_with_logs.get_addon_logs.return_value = (
            "\n".join(f"line {i}" for i in range(1, 21)) + "\n"
        )
        tools = _register_and_collect(client_with_logs)

        result = await tools["ha_get_logs"](
            source="supervisor", slug="core_mosquitto", limit=5
        )

        returned = result["log"].splitlines()
        assert returned == ["line 16", "line 17", "line 18", "line 19", "line 20"]
        assert result["total_lines"] == 20
        assert result["returned_lines"] == 5
        assert result["limit"] == 5

    @pytest.mark.asyncio
    async def test_search_filter_is_case_insensitive_and_recorded(
        self, client_with_logs
    ):
        client_with_logs.get_addon_logs.return_value = (
            "INFO startup complete\n"
            "ERROR something broke\n"
            "DEBUG trivial\n"
            "ERROR another failure\n"
        )
        tools = _register_and_collect(client_with_logs)

        result = await tools["ha_get_logs"](
            source="supervisor", slug="core_mosquitto", search="error"
        )

        lines = result["log"].splitlines()
        assert len(lines) == 2
        assert all("ERROR" in ln for ln in lines)
        assert result["total_lines"] == 2  # total after filter
        assert result["filters_applied"] == {"search": "error"}

    @pytest.mark.asyncio
    async def test_404_raises_tool_error_with_not_found_suggestion(
        self, client_with_logs
    ):
        client_with_logs.get_addon_logs.side_effect = HomeAssistantAPIError(
            "API error: 404 - Addon is not installed",
            status_code=404,
            response_data={"message": "Addon is not installed"},
        )
        tools = _register_and_collect(client_with_logs)

        with pytest.raises(ToolError) as exc_info:
            await tools["ha_get_logs"](source="supervisor", slug="nonexistent")

        payload = _parse_tool_error(exc_info)
        suggestions = payload["error"]["suggestions"]
        assert any("not found or not installed" in s for s in suggestions)
        assert any("ha_get_addon" in s for s in suggestions)
        # context kwargs get spread onto the response root by create_error_response
        assert payload.get("slug") == "nonexistent"
        assert payload.get("source") == "supervisor"

    @pytest.mark.asyncio
    async def test_400_uses_distinct_suggestion_and_service_error_code(
        self, client_with_logs
    ):
        """400 means Supervisor rejected the request, not caller input validation.

        The default `exception_to_structured_error` path would map 400 →
        VALIDATION_INVALID_PARAMETER; for a downstream proxy rejection,
        SERVICE_CALL_FAILED is more accurate.
        """
        client_with_logs.get_addon_logs.side_effect = HomeAssistantAPIError(
            "API error: 400 - bad request",
            status_code=400,
            response_data={"message": "bad request"},
        )
        tools = _register_and_collect(client_with_logs)

        with pytest.raises(ToolError) as exc_info:
            await tools["ha_get_logs"](source="supervisor", slug="weird_slug")

        payload = _parse_tool_error(exc_info)
        suggestions = payload["error"]["suggestions"]
        # 400 must NOT say "not found or not installed" — different root cause.
        assert not any("not found or not installed" in s for s in suggestions)
        assert any("Supervisor rejected" in s for s in suggestions)
        assert payload["error"]["code"] == "SERVICE_CALL_FAILED"

    @pytest.mark.asyncio
    async def test_connection_error_keeps_slug_hint(self, client_with_logs):
        """A transient network failure on a wrong slug should still hint at slug
        verification — the connection-error path must not drop that suggestion."""
        client_with_logs.get_addon_logs.side_effect = HomeAssistantConnectionError(
            "no route"
        )
        tools = _register_and_collect(client_with_logs)

        with pytest.raises(ToolError) as exc_info:
            await tools["ha_get_logs"](source="supervisor", slug="core_mosquitto")

        payload = _parse_tool_error(exc_info)
        suggestions = payload["error"]["suggestions"]
        assert any("Check Home Assistant connection" in s for s in suggestions)
        assert any(
            "Verify add-on slug 'core_mosquitto' is correct" in s for s in suggestions
        )
        assert any("ha_get_addon" in s for s in suggestions)

    @pytest.mark.asyncio
    async def test_level_param_emits_warning_for_supervisor_source(
        self, client_with_logs
    ):
        """`level` doesn't apply to supervisor logs (raw container text); the
        validation layer should warn rather than silently drop the parameter.
        """
        client_with_logs.get_addon_logs.return_value = "line 1\n"
        tools = _register_and_collect(client_with_logs)

        result = await tools["ha_get_logs"](
            source="supervisor", slug="core_mosquitto", level="ERROR"
        )

        assert result["success"] is True
        assert "warnings" in result, (
            "Expected a warning when level is set on supervisor"
        )
        assert any("level" in w and "supervisor" in w for w in result["warnings"]), (
            f"Expected level/supervisor warning, got: {result['warnings']}"
        )


class TestSlugParameterIncompatibilityWarning:
    """`slug` only applies to `source='supervisor'` and `source='system_service'`.
    For any other source it should be flagged in `result["warnings"]` rather
    than silently dropped — same shape as the existing `level`/`entity_id`
    incompatibility warnings.
    """

    @pytest.fixture
    def client_with_logbook(self):
        client = MagicMock()
        client.get_logbook = AsyncMock(return_value=[])
        return client

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["logbook", "system", "error_log", "logger"])
    async def test_slug_on_non_supervisor_source_warns(
        self, client_with_logbook, source
    ):
        """`slug='x'` paired with any non-supervisor/system_service source
        produces a warning naming the parameter and the source."""
        client_with_logbook.get_error_log = AsyncMock(return_value="")
        # `system` and `logger` sources route via WebSocket → system_log/list
        # and logger/log_info respectively; stub send_websocket_message so the
        # tool reaches the warnings-emit path without a real WS client.
        client_with_logbook.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        tools = _register_and_collect(client_with_logbook)

        result = await tools["ha_get_logs"](source=source, slug="core_mosquitto")

        assert "warnings" in result, (
            f"Expected a warning when slug is set on source={source!r}, "
            f"got: {result.keys()}"
        )
        assert any("slug" in w and source in w for w in result["warnings"]), (
            f"Expected slug/{source} warning, got: {result['warnings']}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("source", ["supervisor", "system_service"])
    async def test_slug_on_supervisor_sources_does_not_warn(
        self, client_with_logbook, source
    ):
        """`slug` is meaningful for these sources — no warning should fire.

        Pins that the warning trigger doesn't accidentally cover the supported
        cases, which would silently break the legitimate slug usage flow.
        """
        client_with_logbook.get_addon_logs = AsyncMock(return_value="line\n")
        client_with_logbook._get_system_service_logs = AsyncMock(return_value="line\n")
        tools = _register_and_collect(client_with_logbook)

        slug = "core_mosquitto" if source == "supervisor" else "host"
        result = await tools["ha_get_logs"](source=source, slug=slug)

        if "warnings" in result:
            assert not any("slug" in w for w in result["warnings"]), (
                f"Did not expect a slug warning on {source}, got: {result['warnings']}"
            )


class TestGetSystemServiceLogWrapper:
    """`ha_get_logs(source='system_service')` — slug enum validation, response
    shape, and routing to ``client._get_system_service_logs`` (#1116 scope-add)."""

    @pytest.fixture
    def client_with_system_logs(self):
        client = MagicMock()
        client._get_system_service_logs = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_happy_path_response_shape(self, client_with_system_logs):
        client_with_system_logs._get_system_service_logs.return_value = (
            "host log line 1\nhost log line 2\n"
        )
        tools = _register_and_collect(client_with_system_logs)

        result = await tools["ha_get_logs"](source="system_service", slug="host")

        assert result["success"] is True
        assert result["source"] == "system_service"
        assert result["slug"] == "host"
        assert "host log line 1" in result["log"]
        assert result["total_lines"] == 2
        client_with_system_logs._get_system_service_logs.assert_awaited_once_with(
            "host"
        )

    @pytest.mark.asyncio
    async def test_missing_slug_raises_validation_error(self, client_with_system_logs):
        tools = _register_and_collect(client_with_system_logs)

        with pytest.raises(ToolError) as exc_info:
            await tools["ha_get_logs"](source="system_service")

        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "slug" in body["error"]["message"]
        client_with_system_logs._get_system_service_logs.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_slug_raises_validation_error_with_enum_hint(
        self, client_with_system_logs
    ):
        """Unknown service name fails fast with the allowed-set listed —
        better than a 404 from Supervisor several layers down."""
        tools = _register_and_collect(client_with_system_logs)

        with pytest.raises(ToolError) as exc_info:
            await tools["ha_get_logs"](
                source="system_service", slug="not_a_real_service"
            )

        body = _parse_tool_error(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        # Allowed values appear in either the message or the suggestions
        # so callers can see what's acceptable.
        searchable = body["error"]["message"] + " ".join(
            body["error"].get("suggestions", [])
        )
        for service in ("supervisor", "host", "core", "dns"):
            assert service in searchable
        client_with_system_logs._get_system_service_logs.assert_not_called()

    @pytest.mark.parametrize(
        "service",
        ["supervisor", "host", "core", "dns", "audio", "multicast", "observer"],
    )
    @pytest.mark.asyncio
    async def test_all_seven_allowed_services_dispatch(
        self, client_with_system_logs, service
    ):
        """Every allowed service routes through to the client helper. Catches
        a regression where the slug enum is narrowed in one place but not the
        other."""
        client_with_system_logs._get_system_service_logs.return_value = "ok\n"
        tools = _register_and_collect(client_with_system_logs)

        result = await tools["ha_get_logs"](source="system_service", slug=service)

        assert result["success"] is True
        assert result["slug"] == service
        client_with_system_logs._get_system_service_logs.assert_awaited_once_with(
            service
        )

    @pytest.mark.asyncio
    async def test_403_role_hint_suggestion(self, client_with_system_logs):
        client_with_system_logs._get_system_service_logs.side_effect = (
            HomeAssistantAPIError(
                "Supervisor forbids /core/logs (403) — addon's hassio_role "
                "may be 'default'; need 'manager' or higher",
                status_code=403,
                response_data={"path": "core"},
            )
        )
        tools = _register_and_collect(client_with_system_logs)

        with pytest.raises(ToolError) as exc_info:
            await tools["ha_get_logs"](source="system_service", slug="core")

        body = _parse_tool_error(exc_info)
        suggestions = body["error"]["suggestions"]
        assert any("hassio_role" in s and "manager" in s for s in suggestions)

    @pytest.mark.asyncio
    async def test_level_param_emits_warning_for_system_service_source(
        self, client_with_system_logs
    ):
        """Parity with the supervisor-source level-warning behavior — raw
        container stdout, level-filtering doesn't apply."""
        client_with_system_logs._get_system_service_logs.return_value = "x\n"
        tools = _register_and_collect(client_with_system_logs)

        result = await tools["ha_get_logs"](
            source="system_service", slug="supervisor", level="ERROR"
        )

        assert result["success"] is True
        assert "warnings" in result
        assert any("level" in w and "system_service" in w for w in result["warnings"])


class TestStaleToolNameReferences:
    """Regression guard for #950 bug 2: stale `ha_list_addons()` suggestions."""

    def test_no_tool_module_references_removed_ha_list_addons(self):
        """`ha_list_addons` was consolidated into `ha_get_addon` — no stale refs.

        Scans every `src/ha_mcp/tools/**/*.py` with a word-boundary regex so
        the guard catches regressions in any module, not just tools_utility.py,
        and ignores substrings inside longer identifiers.
        """
        tools_dir = Path(__file__).resolve().parents[3] / "src" / "ha_mcp" / "tools"
        pattern = re.compile(r"\bha_list_addons\b")
        offenders = [
            f.relative_to(tools_dir)
            for f in tools_dir.rglob("*.py")
            if pattern.search(f.read_text(encoding="utf-8"))
        ]
        assert not offenders, (
            f"Stale `ha_list_addons` reference in: {offenders}. "
            "Replace suggestions/docs with `ha_get_addon()` — see #950."
        )
