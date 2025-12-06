"""
Unit tests for Group management tools.

These tests verify the input validation and error handling of the group tools
without requiring a live Home Assistant instance.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestGroupToolsValidation:
    """Test input validation for group tools."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.get_states = AsyncMock(return_value=[])
        client.call_service = AsyncMock(return_value=None)
        return client

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server."""
        mcp = MagicMock()
        mcp.tool = lambda **kwargs: lambda fn: fn
        return mcp

    @pytest.fixture
    def register_tools(self, mock_mcp, mock_client):
        """Register group tools with mocks."""
        from ha_mcp.tools.tools_groups import register_group_tools

        register_group_tools(mock_mcp, mock_client)
        return mock_mcp

    async def test_set_group_invalid_object_id_with_dot(self, mock_client):
        """Test that object_id with dots is rejected."""
        from ha_mcp.tools.tools_groups import register_group_tools

        # Create a container to capture the registered functions
        registered_tools: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered_tools[fn.__name__] = fn
                return fn
            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool

        register_group_tools(mock_mcp, mock_client)

        # Call the set_group function
        result = await registered_tools["ha_config_set_group"](
            object_id="group.invalid",
            entities=["light.test"],
        )

        assert result["success"] is False
        assert "Invalid object_id" in result["error"]

    async def test_set_group_empty_entities_list(self, mock_client):
        """Test that empty entities list is rejected."""
        from ha_mcp.tools.tools_groups import register_group_tools

        registered_tools: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered_tools[fn.__name__] = fn
                return fn
            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool

        register_group_tools(mock_mcp, mock_client)

        result = await registered_tools["ha_config_set_group"](
            object_id="test_group",
            entities=[],
        )

        assert result["success"] is False
        assert "empty" in result["error"].lower()

    async def test_set_group_empty_add_entities_list(self, mock_client):
        """Test that empty add_entities list is rejected."""
        from ha_mcp.tools.tools_groups import register_group_tools

        registered_tools: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered_tools[fn.__name__] = fn
                return fn
            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool

        register_group_tools(mock_mcp, mock_client)

        result = await registered_tools["ha_config_set_group"](
            object_id="test_group",
            add_entities=[],
        )

        assert result["success"] is False
        assert "empty" in result["error"].lower()

    async def test_set_group_mutually_exclusive_operations(self, mock_client):
        """Test that mutually exclusive entity operations are rejected."""
        from ha_mcp.tools.tools_groups import register_group_tools

        registered_tools: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered_tools[fn.__name__] = fn
                return fn
            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool

        register_group_tools(mock_mcp, mock_client)

        # Test entities + add_entities
        result = await registered_tools["ha_config_set_group"](
            object_id="test_group",
            entities=["light.test"],
            add_entities=["light.another"],
        )

        assert result["success"] is False
        assert "Only one of" in result["error"]

        # Test entities + remove_entities
        result = await registered_tools["ha_config_set_group"](
            object_id="test_group",
            entities=["light.test"],
            remove_entities=["light.old"],
        )

        assert result["success"] is False
        assert "Only one of" in result["error"]

        # Test add_entities + remove_entities
        result = await registered_tools["ha_config_set_group"](
            object_id="test_group",
            add_entities=["light.new"],
            remove_entities=["light.old"],
        )

        assert result["success"] is False
        assert "Only one of" in result["error"]

    async def test_remove_group_invalid_object_id(self, mock_client):
        """Test that remove_group rejects invalid object_id."""
        from ha_mcp.tools.tools_groups import register_group_tools

        registered_tools: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered_tools[fn.__name__] = fn
                return fn
            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool

        register_group_tools(mock_mcp, mock_client)

        result = await registered_tools["ha_config_remove_group"](
            object_id="group.invalid",
        )

        assert result["success"] is False
        assert "Invalid object_id" in result["error"]

    async def test_list_groups_success(self, mock_client):
        """Test successful group listing."""
        from ha_mcp.tools.tools_groups import register_group_tools

        # Mock states with groups
        mock_client.get_states = AsyncMock(return_value=[
            {
                "entity_id": "group.living_room",
                "state": "on",
                "attributes": {
                    "friendly_name": "Living Room",
                    "entity_id": ["light.lamp1", "light.lamp2"],
                    "icon": "mdi:sofa",
                    "all": False,
                },
            },
            {
                "entity_id": "light.bed_light",  # Not a group
                "state": "off",
                "attributes": {"friendly_name": "Bed Light"},
            },
        ])

        registered_tools: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered_tools[fn.__name__] = fn
                return fn
            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool

        register_group_tools(mock_mcp, mock_client)

        result = await registered_tools["ha_config_list_groups"]()

        assert result["success"] is True
        assert result["count"] == 1
        assert len(result["groups"]) == 1
        assert result["groups"][0]["entity_id"] == "group.living_room"
        assert result["groups"][0]["object_id"] == "living_room"
        assert result["groups"][0]["friendly_name"] == "Living Room"
        assert "light.lamp1" in result["groups"][0]["entity_ids"]

    async def test_set_group_success(self, mock_client):
        """Test successful group creation."""
        from ha_mcp.tools.tools_groups import register_group_tools

        registered_tools: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered_tools[fn.__name__] = fn
                return fn
            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool

        register_group_tools(mock_mcp, mock_client)

        result = await registered_tools["ha_config_set_group"](
            object_id="test_group",
            name="Test Group",
            entities=["light.lamp1", "light.lamp2"],
            icon="mdi:lightbulb-group",
        )

        assert result["success"] is True
        assert result["entity_id"] == "group.test_group"
        assert result["object_id"] == "test_group"
        assert "name" in result["updated_fields"]
        assert "entities" in result["updated_fields"]
        assert "icon" in result["updated_fields"]

        # Verify service was called
        mock_client.call_service.assert_called_once_with(
            "group", "set",
            {
                "object_id": "test_group",
                "name": "Test Group",
                "icon": "mdi:lightbulb-group",
                "entities": ["light.lamp1", "light.lamp2"],
            }
        )

    async def test_remove_group_success(self, mock_client):
        """Test successful group removal."""
        from ha_mcp.tools.tools_groups import register_group_tools

        registered_tools: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered_tools[fn.__name__] = fn
                return fn
            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool

        register_group_tools(mock_mcp, mock_client)

        result = await registered_tools["ha_config_remove_group"](
            object_id="test_group",
        )

        assert result["success"] is True
        assert result["entity_id"] == "group.test_group"
        assert result["object_id"] == "test_group"

        # Verify service was called
        mock_client.call_service.assert_called_once_with(
            "group", "remove",
            {"object_id": "test_group"}
        )

    async def test_set_group_all_on_parameter(self, mock_client):
        """Test that all_on parameter is correctly mapped to 'all'."""
        from ha_mcp.tools.tools_groups import register_group_tools

        registered_tools: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered_tools[fn.__name__] = fn
                return fn
            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool

        register_group_tools(mock_mcp, mock_client)

        result = await registered_tools["ha_config_set_group"](
            object_id="test_group",
            entities=["light.lamp1"],
            all_on=True,
        )

        assert result["success"] is True

        # Verify 'all' was passed to service
        call_args = mock_client.call_service.call_args
        assert call_args[0][2]["all"] is True

    async def test_list_groups_error_handling(self, mock_client):
        """Test error handling in list_groups."""
        from ha_mcp.tools.tools_groups import register_group_tools

        # Make get_states raise an exception
        mock_client.get_states = AsyncMock(side_effect=Exception("Connection failed"))

        registered_tools: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered_tools[fn.__name__] = fn
                return fn
            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool

        register_group_tools(mock_mcp, mock_client)

        result = await registered_tools["ha_config_list_groups"]()

        assert result["success"] is False
        assert "Failed to list groups" in result["error"]
        assert "suggestions" in result

    async def test_set_group_error_handling(self, mock_client):
        """Test error handling in set_group."""
        from ha_mcp.tools.tools_groups import register_group_tools

        # Make call_service raise an exception
        mock_client.call_service = AsyncMock(side_effect=Exception("Service failed"))

        registered_tools: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered_tools[fn.__name__] = fn
                return fn
            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool

        register_group_tools(mock_mcp, mock_client)

        result = await registered_tools["ha_config_set_group"](
            object_id="test_group",
            entities=["light.lamp1"],
        )

        assert result["success"] is False
        assert "Failed to set group" in result["error"]
        assert "suggestions" in result

    async def test_remove_group_error_handling(self, mock_client):
        """Test error handling in remove_group."""
        from ha_mcp.tools.tools_groups import register_group_tools

        # Make call_service raise an exception
        mock_client.call_service = AsyncMock(side_effect=Exception("Service failed"))

        registered_tools: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered_tools[fn.__name__] = fn
                return fn
            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool

        register_group_tools(mock_mcp, mock_client)

        result = await registered_tools["ha_config_remove_group"](
            object_id="test_group",
        )

        assert result["success"] is False
        assert "Failed to remove group" in result["error"]
        assert "suggestions" in result
