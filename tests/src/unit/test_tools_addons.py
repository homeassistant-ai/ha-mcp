"""Unit tests for add-on tools (_call_addon_api error paths)."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ha_mcp.tools.tools_addons import _call_addon_api


def _make_mock_client() -> MagicMock:
    """Create a mock HomeAssistantClient."""
    client = MagicMock()
    client.base_url = "http://localhost:8123"
    client.token = "test-token"
    return client


class TestCallAddonApiErrors:
    """Tests for _call_addon_api error paths."""

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self):
        """Paths containing '..' components should be rejected."""
        client = _make_mock_client()
        result = await _call_addon_api(client, "test_addon", "../../etc/passwd")

        assert result["success"] is False
        assert "error" in result
        assert "traversal" in result["error"]["message"].lower() or ".." in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_path_traversal_middle_segment(self):
        """Paths with '..' in the middle should also be rejected."""
        client = _make_mock_client()
        result = await _call_addon_api(client, "test_addon", "api/../secret/data")

        assert result["success"] is False
        assert ".." in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_path_with_dotdot_in_name_allowed(self):
        """Paths where '..' is part of a filename (not a segment) should pass traversal check."""
        client = _make_mock_client()

        # "..foo" is not a ".." path segment, so it should pass the traversal check
        # but it will fail on the addon info lookup (next step)
        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value={"success": False, "error": {"code": "RESOURCE_NOT_FOUND", "message": "Not found"}},
        ):
            result = await _call_addon_api(client, "test_addon", "..foo/bar")

        # Should have passed traversal check and failed on addon lookup instead
        assert result["success"] is False
        assert "Not found" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_addon_not_found(self):
        """Should return error when add-on slug doesn't exist."""
        client = _make_mock_client()
        error_response = {
            "success": False,
            "error": {"code": "RESOURCE_NOT_FOUND", "message": "Add-on 'fake_addon' not found"},
        }

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value=error_response,
        ):
            result = await _call_addon_api(client, "fake_addon", "/api/test")

        assert result["success"] is False
        assert "not found" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_addon_no_ingress_support(self):
        """Should return error when add-on doesn't support Ingress."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "addon": {
                    "name": "Test Addon",
                    "slug": "test_addon",
                    "ingress": False,
                    "state": "started",
                },
            },
        ):
            result = await _call_addon_api(client, "test_addon", "/api/test")

        assert result["success"] is False
        assert "ingress" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_addon_not_running(self):
        """Should return error when add-on is not running."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "addon": {
                    "name": "Test Addon",
                    "slug": "test_addon",
                    "ingress": True,
                    "state": "stopped",
                    "ingress_entry": "/api/hassio_ingress/abc123",
                },
            },
        ):
            result = await _call_addon_api(client, "test_addon", "/api/test")

        assert result["success"] is False
        assert "not running" in result["error"]["message"].lower()
        assert "stopped" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_addon_no_ingress_entry(self):
        """Should return error when add-on has Ingress but no entry path."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "addon": {
                    "name": "Test Addon",
                    "slug": "test_addon",
                    "ingress": True,
                    "state": "started",
                    "ingress_entry": "",
                },
            },
        ):
            result = await _call_addon_api(client, "test_addon", "/api/test")

        assert result["success"] is False
        assert "ingress_entry" in result["error"]["message"].lower() or "ingress" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_http_timeout(self):
        """Should return timeout error when add-on API doesn't respond."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "addon": {
                    "name": "Test Addon",
                    "slug": "test_addon",
                    "ingress": True,
                    "state": "started",
                    "ingress_entry": "/api/hassio_ingress/abc123",
                },
            },
        ), patch(
            "ha_mcp.tools.tools_addons.httpx.AsyncClient",
        ) as mock_httpx:
            mock_http_client = AsyncMock()
            mock_http_client.request.side_effect = httpx.TimeoutException("timed out")
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_http_client)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _call_addon_api(client, "test_addon", "/api/test", timeout=5)

        assert result["success"] is False
        assert "timeout" in result["error"]["message"].lower() or "timed out" in str(result).lower()

    @pytest.mark.asyncio
    async def test_http_connection_error(self):
        """Should return connection error when can't reach add-on."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "addon": {
                    "name": "Test Addon",
                    "slug": "test_addon",
                    "ingress": True,
                    "state": "started",
                    "ingress_entry": "/api/hassio_ingress/abc123",
                },
            },
        ), patch(
            "ha_mcp.tools.tools_addons.httpx.AsyncClient",
        ) as mock_httpx:
            mock_http_client = AsyncMock()
            mock_http_client.request.side_effect = httpx.ConnectError("Connection refused")
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_http_client)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _call_addon_api(client, "test_addon", "/api/test")

        assert result["success"] is False
        assert "connect" in result["error"]["message"].lower() or "connection" in str(result).lower()
