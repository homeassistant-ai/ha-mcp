"""Unit tests for tools_system module.

Regression tests for https://github.com/homeassistant-ai/ha-mcp/issues/612
ha_restart reports failure when a reverse proxy returns 504 during restart.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
)
from ha_mcp.tools.tools_system import register_system_tools


def _register_and_capture_restart(mock_client):
    """Register system tools with a mock MCP and return the ha_restart function."""
    mock_mcp = MagicMock()
    captured = {}

    def fake_tool(**kwargs):
        def decorator(fn):
            captured[fn.__name__] = fn
            return fn
        return decorator

    mock_mcp.tool = fake_tool
    register_system_tools(mock_mcp, mock_client)
    assert "ha_restart" in captured, "ha_restart was not registered"
    return captured["ha_restart"]


def _make_client_that_fails_on_restart(exception):
    """Create a mock client where check_config succeeds but call_service raises."""
    mock_client = AsyncMock()
    mock_client.check_config.return_value = {"result": "valid"}
    mock_client.call_service.side_effect = exception
    return mock_client


class TestHaRestartErrorHandling:
    """Tests for ha_restart handling of expected errors during restart."""

    @pytest.mark.asyncio
    async def test_504_gateway_timeout_treated_as_success(self):
        """A 504 from a reverse proxy after restart initiated should be success.

        Reproduces issue #612: user behind a reverse proxy gets 504 when HA
        shuts down, but HA actually restarted successfully.
        """
        error = HomeAssistantAPIError("API error: 504 - ", status_code=504)
        client = _make_client_that_fails_on_restart(error)
        ha_restart = _register_and_capture_restart(client)

        result = await ha_restart(confirm=True)

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_unrelated_error_still_fails(self):
        """Errors unrelated to restart should still report failure."""
        error = Exception("Something completely unrelated went wrong")
        client = _make_client_that_fails_on_restart(error)
        ha_restart = _register_and_capture_restart(client)

        result = await ha_restart(confirm=True)

        assert result["success"] is False
        assert "error" in result
