"""Shared fixtures for unit tests."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from ha_mcp.tools.tools_config_dashboards import (
    register_config_dashboard_tools,
    _compute_config_hash,
)


class MockToolRegistry:
    """Helper class to capture tool registrations."""

    def __init__(self):
        self.registered_tools: dict[str, callable] = {}

    def tool(self, *args, **kwargs):
        """Decorator that captures registered tools."""

        def wrapper(func):
            self.registered_tools[func.__name__] = func
            return func

        return wrapper


# Export _compute_config_hash for tests to use directly
compute_config_hash = _compute_config_hash


@pytest.fixture
def mock_mcp():
    """Create a mock MCP server that captures tool registrations."""
    return MockToolRegistry()


@pytest.fixture
def mock_client():
    """Create a mock Home Assistant client."""
    client = MagicMock()
    client.send_websocket_message = AsyncMock()
    return client


@pytest.fixture
def registered_tools(mock_mcp, mock_client):
    """Register tools and return the registry."""
    register_config_dashboard_tools(mock_mcp, mock_client)
    return mock_mcp.registered_tools
