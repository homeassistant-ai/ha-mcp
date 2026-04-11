"""Unit tests for the supervisor add-on log fix (#950).

Covers:
- `HomeAssistantClient.get_addon_logs()` — new REST-client method that fetches
  add-on container logs via HA Core's `/api/hassio/addons/{slug}/logs` proxy,
  which HA Core returns as text/plain (no JSON decode in the hot path).
- `ha_get_logs(source="supervisor")` no longer routes through the broken
  `supervisor/api` websocket proxy.
- Stale `ha_list_addons()` suggestion strings are replaced with
  `ha_get_addon()`.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)


@pytest.fixture
def mock_client():
    """HomeAssistantClient with stubbed internals — no real network."""
    with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
        client = HomeAssistantClient()
        client.base_url = "http://test.local:8123"
        client.token = "test-token"
        client.timeout = 30
        client.httpx_client = MagicMock()
        return client


class TestGetAddonLogs:
    """Tests for the REST-client `get_addon_logs` method (the core fix)."""

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
        """404 (unknown slug) raises HomeAssistantAPIError with status 404 and body."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Addon is not installed"
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


class TestStaleToolNameReferences:
    """Regression guard for #950 bug 2: stale `ha_list_addons()` suggestions."""

    def test_tools_utility_does_not_reference_removed_ha_list_addons(self):
        """`ha_list_addons` was consolidated into `ha_get_addon` — no stale refs."""
        source = (
            Path(__file__).resolve().parents[3]
            / "src"
            / "ha_mcp"
            / "tools"
            / "tools_utility.py"
        ).read_text()
        assert "ha_list_addons" not in source, (
            "tools_utility.py still references the removed `ha_list_addons` tool. "
            "Replace suggestions with `ha_get_addon()` — see #950."
        )
