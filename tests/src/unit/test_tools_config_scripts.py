"""
Unit tests for Script configuration tools.

These tests verify the input validation and error handling of the script tools,
especially for blueprint-based scripts (issue #466).
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Valid guide_response for tools requiring ha_get_tool_guide()
_GR = {"success": True}


class TestScriptToolsValidation:
    """Test input validation for script configuration tools."""

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.upsert_script_config = AsyncMock(
            return_value={"success": True, "script_id": "test_script"}
        )
        client.get_script_config = AsyncMock(
            return_value={
                "alias": "Test Script",
                "sequence": [{"delay": {"seconds": 1}}],
            }
        )
        client.delete_script_config = AsyncMock(
            return_value={"success": True, "script_id": "test_script"}
        )
        client.get_entity_state = AsyncMock(
            return_value={"state": "off", "entity_id": "script.test_script"}
        )
        return client

    @pytest.fixture
    def register_tools(self, mock_client):
        """Register script tools with mocks."""
        from ha_mcp.tools.tools_config_scripts import register_config_script_tools

        # Create a container to capture the registered functions
        registered_tools: dict[str, Any] = {}

        def capture_tool(**kwargs):
            def decorator(fn):
                registered_tools[fn.__name__] = fn
                return fn

            return decorator

        mock_mcp = MagicMock()
        mock_mcp.tool = capture_tool

        register_config_script_tools(mock_mcp, mock_client)
        return registered_tools

    async def test_set_script_missing_both_sequence_and_blueprint(
        self, register_tools, mock_client
    ):
        """Test that config without sequence or use_blueprint is rejected."""
        result = await register_tools["ha_config_set_script"](
            script_id="test_script",
            guide_response=_GR,
            config={"alias": "Test Script"},  # Missing both sequence and use_blueprint
        )

        assert result["success"] is False
        assert "sequence" in result["error"] and "use_blueprint" in result["error"]
        assert "required_fields" in result

    async def test_set_script_with_sequence_success(self, register_tools, mock_client):
        """Test that regular script with sequence is accepted."""
        result = await register_tools["ha_config_set_script"](
            script_id="test_script",
            guide_response=_GR,
            config={
                "alias": "Test Script",
                "sequence": [{"delay": {"seconds": 5}}],
            },
        )

        assert result["success"] is True
        mock_client.upsert_script_config.assert_called_once()

    async def test_set_script_with_blueprint_success(self, register_tools, mock_client):
        """Test that blueprint-based script is accepted."""
        result = await register_tools["ha_config_set_script"](
            script_id="test_script",
            guide_response=_GR,
            config={
                "alias": "My Blueprint Script",
                "use_blueprint": {
                    "path": "notification_script.yaml",
                    "input": {"message": "Hello"},
                },
            },
        )

        assert result["success"] is True
        mock_client.upsert_script_config.assert_called_once()

        # Verify the config passed to client doesn't have empty sequence
        call_args = mock_client.upsert_script_config.call_args
        config_passed = call_args[0][0]
        assert "use_blueprint" in config_passed
        assert "sequence" not in config_passed or config_passed["sequence"] != []

    async def test_set_script_blueprint_with_empty_sequence_strips_it(
        self, register_tools, mock_client
    ):
        """Test that empty sequence is stripped from blueprint scripts."""
        result = await register_tools["ha_config_set_script"](
            script_id="test_script",
            guide_response=_GR,
            config={
                "alias": "My Blueprint Script",
                "use_blueprint": {
                    "path": "notification_script.yaml",
                    "input": {"message": "Hello"},
                },
                "sequence": [],  # Empty sequence should be stripped
            },
        )

        assert result["success"] is True

        # Verify empty sequence was stripped
        call_args = mock_client.upsert_script_config.call_args
        config_passed = call_args[0][0]
        assert "sequence" not in config_passed, "Empty sequence should be stripped"

    async def test_set_script_blueprint_with_non_empty_sequence_keeps_it(
        self, register_tools, mock_client
    ):
        """Test that non-empty sequence is kept even with blueprint."""
        result = await register_tools["ha_config_set_script"](
            script_id="test_script",
            guide_response=_GR,
            config={
                "alias": "My Blueprint Script",
                "use_blueprint": {
                    "path": "notification_script.yaml",
                    "input": {"message": "Hello"},
                },
                "sequence": [{"delay": {"seconds": 1}}],  # Non-empty should be kept
            },
        )

        assert result["success"] is True

        # Verify non-empty sequence was kept
        call_args = mock_client.upsert_script_config.call_args
        config_passed = call_args[0][0]
        assert "sequence" in config_passed
        assert config_passed["sequence"] == [{"delay": {"seconds": 1}}]

    async def test_set_script_invalid_json_config(self, register_tools, mock_client):
        """Test that invalid JSON config is rejected."""
        result = await register_tools["ha_config_set_script"](
            script_id="test_script",
            guide_response=_GR,
            config='{"invalid": json}',  # Invalid JSON string
        )

        assert result["success"] is False
        assert "Invalid config parameter" in result["error"]

    async def test_set_script_config_not_dict(self, register_tools, mock_client):
        """Test that non-dict config is rejected."""
        result = await register_tools["ha_config_set_script"](
            script_id="test_script",
            guide_response=_GR,
            config="not a dict",
        )

        assert result["success"] is False
        # The error message comes from parse_json_param which tries to parse as JSON first
        assert "Invalid" in result["error"]


class TestStripEmptyScriptFields:
    """Test the _strip_empty_script_fields helper function."""

    def test_strip_empty_sequence(self):
        """Test that empty sequence array is removed."""
        from ha_mcp.tools.tools_config_scripts import _strip_empty_script_fields

        config = {
            "alias": "Test",
            "use_blueprint": {"path": "test.yaml", "input": {}},
            "sequence": [],
        }

        result = _strip_empty_script_fields(config)

        assert "sequence" not in result
        assert "use_blueprint" in result
        assert "alias" in result

    def test_keep_non_empty_sequence(self):
        """Test that non-empty sequence is kept."""
        from ha_mcp.tools.tools_config_scripts import _strip_empty_script_fields

        config = {
            "alias": "Test",
            "use_blueprint": {"path": "test.yaml", "input": {}},
            "sequence": [{"delay": {"seconds": 1}}],
        }

        result = _strip_empty_script_fields(config)

        assert "sequence" in result
        assert result["sequence"] == [{"delay": {"seconds": 1}}]

    def test_no_sequence_field(self):
        """Test that config without sequence is unchanged."""
        from ha_mcp.tools.tools_config_scripts import _strip_empty_script_fields

        config = {
            "alias": "Test",
            "use_blueprint": {"path": "test.yaml", "input": {}},
        }

        result = _strip_empty_script_fields(config)

        assert "sequence" not in result
        assert result == config
