"""Unit tests for REST client Supervisor log retrieval and _request_text.

These tests cover the text/plain fetch path for Supervisor add-on logs:
the ``/addons/{slug}/logs`` endpoint returns raw text, so it cannot go
through ``_request`` (which forces JSON parsing).
"""

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
    """Create a mock HomeAssistantClient for testing."""
    with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
        client = HomeAssistantClient()
        client.base_url = "http://test.local:8123"
        client.token = "test-token"
        client.timeout = 30
        client.httpx_client = MagicMock()
        return client


class TestRequestText:
    """Tests for the _request_text method."""

    @pytest.mark.asyncio
    async def test_returns_raw_text_on_200(self, mock_client):
        """200 response body should be returned verbatim, no JSON parsing."""
        response = MagicMock()
        response.status_code = 200
        response.text = "2026-04-16 12:00:00 INFO some log line\nsecond line\n"
        mock_client.httpx_client.request = AsyncMock(return_value=response)

        result = await mock_client._request_text("GET", "/hassio/addons/foo/logs")

        assert result == "2026-04-16 12:00:00 INFO some log line\nsecond line\n"
        mock_client.httpx_client.request.assert_called_once_with(
            "GET", "/hassio/addons/foo/logs"
        )

    @pytest.mark.asyncio
    async def test_401_raises_auth_error(self, mock_client):
        """401 should raise HomeAssistantAuthError with the standard message."""
        response = MagicMock()
        response.status_code = 401
        mock_client.httpx_client.request = AsyncMock(return_value=response)

        with pytest.raises(HomeAssistantAuthError) as exc_info:
            await mock_client._request_text("GET", "/hassio/addons/foo/logs")

        assert "Invalid authentication token" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_404_raises_api_error_with_status(self, mock_client):
        """404 should raise HomeAssistantAPIError with status_code=404."""
        response = MagicMock()
        response.status_code = 404
        response.json = MagicMock(side_effect=ValueError("not json"))
        response.text = "404: Not Found"
        mock_client.httpx_client.request = AsyncMock(return_value=response)

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client._request_text("GET", "/hassio/addons/bogus/logs")

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_500_raises_api_error(self, mock_client):
        """5xx should raise HomeAssistantAPIError; JSON error body is used if present."""
        response = MagicMock()
        response.status_code = 500
        response.json = MagicMock(return_value={"message": "internal boom"})
        mock_client.httpx_client.request = AsyncMock(return_value=response)

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client._request_text("GET", "/hassio/addons/foo/logs")

        assert exc_info.value.status_code == 500
        assert "internal boom" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_connect_error_raises_connection_error(self, mock_client):
        """httpx.ConnectError should be wrapped in HomeAssistantConnectionError."""
        mock_client.httpx_client.request = AsyncMock(
            side_effect=httpx.ConnectError("connection refused")
        )

        with pytest.raises(HomeAssistantConnectionError):
            await mock_client._request_text("GET", "/hassio/addons/foo/logs")

    @pytest.mark.asyncio
    async def test_timeout_raises_connection_error(self, mock_client):
        """httpx.TimeoutException should be wrapped in HomeAssistantConnectionError."""
        mock_client.httpx_client.request = AsyncMock(
            side_effect=httpx.TimeoutException("timed out")
        )

        with pytest.raises(HomeAssistantConnectionError):
            await mock_client._request_text("GET", "/hassio/addons/foo/logs")


class TestGetAddonLog:
    """Tests for get_addon_log."""

    @pytest.mark.asyncio
    async def test_success_returns_log_text(self, mock_client):
        """Happy path: get_addon_log returns the raw text from _request_text."""
        mock_client._request_text = AsyncMock(
            return_value="line 1\nline 2\nline 3\n"
        )

        result = await mock_client.get_addon_log("core_mosquitto")

        assert result == "line 1\nline 2\nline 3\n"
        mock_client._request_text.assert_called_once_with(
            "GET", "/hassio/addons/core_mosquitto/logs"
        )

    @pytest.mark.asyncio
    async def test_addon_not_found_propagates_404(self, mock_client):
        """404 from the Supervisor proxy should propagate as HomeAssistantAPIError."""
        mock_client._request_text = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 404 - not found", status_code=404
            )
        )

        with pytest.raises(HomeAssistantAPIError) as exc_info:
            await mock_client.get_addon_log("does_not_exist")

        assert exc_info.value.status_code == 404
        assert "404" in str(exc_info.value)
        assert "not found" in str(exc_info.value).lower()
