"""
Unit tests for Script configuration tools.

These tests verify the input validation and error handling of the script tools,
especially for blueprint-based scripts (issue #466).
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_config_scripts import ConfigScriptTools


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
        # Reference validator (#940) calls these during set_script;
        # provide empty-but-valid payloads so the walker runs.
        client.get_services = AsyncMock(return_value=[])
        client.get_states = AsyncMock(return_value=[])
        return client

    @pytest.fixture
    def tools(self, mock_client):
        """Create ConfigScriptTools instance."""
        return ConfigScriptTools(mock_client)

    async def test_set_script_missing_both_sequence_and_blueprint(
        self, tools
    ):
        """Test that config without sequence or use_blueprint is rejected."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_script(
                script_id="test_script",
                config={"alias": "Test Script"},  # Missing both sequence and use_blueprint
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        error_msg = error_data["error"]["message"]
        assert "sequence" in error_msg and "use_blueprint" in error_msg

    async def test_set_script_with_sequence_success(self, tools, mock_client):
        """Test that regular script with sequence is accepted."""
        result = await tools.ha_config_set_script(
            script_id="test_script",
            config={
                "alias": "Test Script",
                "sequence": [{"delay": {"seconds": 5}}],
            },
        )

        assert result["success"] is True
        mock_client.upsert_script_config.assert_called_once()

    async def test_set_script_with_blueprint_success(self, tools, mock_client):
        """Test that blueprint-based script is accepted."""
        result = await tools.ha_config_set_script(
            script_id="test_script",
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
        self, tools, mock_client
    ):
        """Test that empty sequence is stripped from blueprint scripts."""
        result = await tools.ha_config_set_script(
            script_id="test_script",
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
        self, tools, mock_client
    ):
        """Test that non-empty sequence is kept even with blueprint."""
        result = await tools.ha_config_set_script(
            script_id="test_script",
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

    async def test_set_script_invalid_json_config(self, tools):
        """Test that invalid JSON config is rejected."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_script(
                script_id="test_script",
                config='{"invalid": json}',  # Invalid JSON string
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "Invalid config parameter" in error_data["error"]["message"]

    async def test_set_script_config_not_dict(self, tools):
        """Test that non-dict config is rejected."""
        with pytest.raises(ToolError) as exc_info:
            await tools.ha_config_set_script(
                script_id="test_script",
                config="not a dict",
            )

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        # The error message comes from parse_json_param which tries to parse as JSON first
        assert "Invalid" in error_data["error"]["message"]


class TestGetScriptCanonicalId:
    """Issue #1334: ha_config_get_script returns the canonical storage key.

    Previously the tool echoed the caller-supplied ``script_id`` unchanged,
    forcing clients to do their own resolution before chaining into
    ``ha_config_set_script`` / ``ha_config_remove_script``. The rest_client
    envelope already carries the resolved storage key; this test pins the
    tool to surface that value (with fallback to the input when absent),
    matching scenes / dashboards / automations.
    """

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        # send_websocket_message is invoked by fetch_entity_category; return
        # a no-category response so the get path doesn't error.
        client.send_websocket_message = AsyncMock(
            return_value={"success": False}
        )
        return client

    @pytest.fixture
    def tools(self, mock_client):
        return ConfigScriptTools(mock_client)

    async def test_returns_canonical_script_id_from_envelope(
        self, tools, mock_client
    ):
        """When the rest_client resolves an alias to a storage key, the
        tool's returned ``script_id`` reflects the canonical key."""
        mock_client.get_script_config = AsyncMock(
            return_value={
                "success": True,
                "script_id": "1234567890",
                "config": {
                    "alias": "Morning Routine",
                    "sequence": [{"delay": {"seconds": 1}}],
                },
            }
        )

        result = await tools.ha_config_get_script(script_id="morning_routine")

        assert result["success"] is True
        assert result["script_id"] == "1234567890"

    async def test_falls_back_to_input_when_envelope_missing_key(
        self, tools, mock_client
    ):
        """If the rest_client envelope omits ``script_id`` (defensive
        fallback), the tool surfaces the caller-supplied identifier."""
        mock_client.get_script_config = AsyncMock(
            return_value={
                "success": True,
                "config": {
                    "alias": "Test",
                    "sequence": [{"delay": {"seconds": 1}}],
                },
            }
        )

        result = await tools.ha_config_get_script(script_id="caller_input")

        assert result["success"] is True
        assert result["script_id"] == "caller_input"


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
