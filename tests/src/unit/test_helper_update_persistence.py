"""
Unit tests for input helper update persistence (issue #880).

Verifies that ha_config_set_helper uses the {type}/update WebSocket API
(not just entity registry) when updating input helpers, so that config
changes like options, min/max, etc. persist across HA restarts.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_client():
    """Create a mock client with WebSocket support for helper updates."""
    client = MagicMock()

    def make_ws_responses(helper_type: str, unique_id: str = "abc123"):
        """Build side_effect for send_websocket_message based on message type."""

        async def ws_handler(msg: dict) -> dict:
            msg_type = msg.get("type", "")

            if msg_type == "config/entity_registry/get":
                return {
                    "success": True,
                    "result": {
                        "entity_id": msg["entity_id"],
                        "unique_id": unique_id,
                        "platform": helper_type,
                    },
                }

            if msg_type.endswith("/update"):
                return {
                    "success": True,
                    "result": {
                        "id": unique_id,
                        **{k: v for k, v in msg.items() if k != "type"},
                    },
                }

            if msg_type == "config/entity_registry/update":
                return {
                    "success": True,
                    "result": {"entity_entry": {"entity_id": msg["entity_id"]}},
                }

            return {"success": True, "result": {}}

        return ws_handler

    client._make_ws_responses = make_ws_responses
    return client


@pytest.fixture
def register_tools(mock_client):
    """Register helper config tools and return the captured tool functions."""
    from ha_mcp.tools.tools_config_helpers import register_config_helper_tools

    registered_tools: dict[str, Any] = {}

    def capture_tool(**kwargs):
        def decorator(fn):
            registered_tools[fn.__name__] = fn
            return fn

        return decorator

    mock_mcp = MagicMock()
    mock_mcp.tool = capture_tool
    register_config_helper_tools(mock_mcp, mock_client)
    return registered_tools


class TestInputSelectUpdatePersistence:
    """Test that input_select updates use the storage API, not just entity registry."""

    async def test_update_options_calls_storage_api(self, register_tools, mock_client):
        """Updating options should call input_select/update, not entity registry."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_select")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_select",
                helper_id="my_dropdown",
                options=["Option A", "Option B", "Option C"],
            )

        assert result["success"] is True

        # Find the storage update call
        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            (c for c in ws_calls if c[0][0].get("type") == "input_select/update"),
            None,
        )
        assert update_call is not None, (
            "Expected input_select/update WebSocket call, got: "
            + str([c[0][0].get("type") for c in ws_calls])
        )
        msg = update_call[0][0]
        assert msg["options"] == ["Option A", "Option B", "Option C"]
        assert "input_select_id" in msg

    async def test_update_initial_value(self, register_tools, mock_client):
        """Updating initial value should be included in storage API call."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_select")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_select",
                helper_id="my_dropdown",
                options=["A", "B"],
                initial="B",
            )

        assert result["success"] is True
        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            c for c in ws_calls if c[0][0].get("type") == "input_select/update"
        )
        assert update_call[0][0]["initial"] == "B"


class TestInputNumberUpdatePersistence:
    """Test that input_number updates use the storage API."""

    async def test_update_min_max_calls_storage_api(self, register_tools, mock_client):
        """Updating min/max should call input_number/update."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_number")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_number",
                helper_id="my_number",
                min_value=0,
                max_value=200,
                step=5,
                unit_of_measurement="W",
                mode="slider",
            )

        assert result["success"] is True

        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            (c for c in ws_calls if c[0][0].get("type") == "input_number/update"),
            None,
        )
        assert update_call is not None
        msg = update_call[0][0]
        assert msg["min"] == 0
        assert msg["max"] == 200
        assert msg["step"] == 5
        assert msg["unit_of_measurement"] == "W"
        assert msg["mode"] == "slider"


class TestInputTextUpdatePersistence:
    """Test that input_text updates use the storage API."""

    async def test_update_mode_calls_storage_api(self, register_tools, mock_client):
        """Updating mode should call input_text/update."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_text")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_text",
                helper_id="my_text",
                min_value=1,
                max_value=50,
                mode="password",
            )

        assert result["success"] is True
        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            (c for c in ws_calls if c[0][0].get("type") == "input_text/update"),
            None,
        )
        assert update_call is not None
        msg = update_call[0][0]
        assert msg["min"] == 1
        assert msg["max"] == 50
        assert msg["mode"] == "password"


class TestInputBooleanUpdatePersistence:
    """Test that input_boolean updates use the storage API."""

    async def test_update_name_calls_storage_api(self, register_tools, mock_client):
        """Updating name should call input_boolean/update."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_boolean")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_boolean",
                helper_id="my_toggle",
                name="Updated Toggle",
            )

        assert result["success"] is True
        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            (c for c in ws_calls if c[0][0].get("type") == "input_boolean/update"),
            None,
        )
        assert update_call is not None
        assert update_call[0][0]["name"] == "Updated Toggle"


class TestInputDatetimeUpdatePersistence:
    """Test that input_datetime updates use the storage API."""

    async def test_update_has_date_time_calls_storage_api(
        self, register_tools, mock_client
    ):
        """Updating has_date/has_time should call input_datetime/update."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_datetime")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_datetime",
                helper_id="my_datetime",
                has_date=True,
                has_time=False,
            )

        assert result["success"] is True
        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            (c for c in ws_calls if c[0][0].get("type") == "input_datetime/update"),
            None,
        )
        assert update_call is not None
        msg = update_call[0][0]
        assert msg["has_date"] is True
        assert msg["has_time"] is False


class TestCounterUpdatePersistence:
    """Test that counter updates use the storage API."""

    async def test_update_step_calls_storage_api(self, register_tools, mock_client):
        """Updating step/min/max should call counter/update."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("counter")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="counter",
                helper_id="my_counter",
                initial="10",
                min_value=0,
                max_value=100,
                step=2,
                restore=True,
            )

        assert result["success"] is True
        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            (c for c in ws_calls if c[0][0].get("type") == "counter/update"),
            None,
        )
        assert update_call is not None
        msg = update_call[0][0]
        assert msg["initial"] == 10
        assert msg["minimum"] == 0
        assert msg["maximum"] == 100
        assert msg["step"] == 2
        assert msg["restore"] is True


class TestTimerUpdatePersistence:
    """Test that timer updates use the storage API."""

    async def test_update_duration_calls_storage_api(self, register_tools, mock_client):
        """Updating duration should call timer/update."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("timer")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="timer",
                helper_id="my_timer",
                duration="00:30:00",
                restore=False,
            )

        assert result["success"] is True
        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            (c for c in ws_calls if c[0][0].get("type") == "timer/update"),
            None,
        )
        assert update_call is not None
        msg = update_call[0][0]
        assert msg["duration"] == "00:30:00"
        assert msg["restore"] is False


class TestInputButtonUpdatePersistence:
    """Test that input_button updates use the storage API."""

    async def test_update_name_calls_storage_api(self, register_tools, mock_client):
        """Updating name should call input_button/update."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=mock_client._make_ws_responses("input_button")
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="input_button",
                helper_id="my_button",
                name="Updated Button",
                icon="mdi:gesture-tap",
            )

        assert result["success"] is True
        ws_calls = mock_client.send_websocket_message.call_args_list
        update_call = next(
            (c for c in ws_calls if c[0][0].get("type") == "input_button/update"),
            None,
        )
        assert update_call is not None
        msg = update_call[0][0]
        assert msg["name"] == "Updated Button"
        assert msg["icon"] == "mdi:gesture-tap"


class TestEntityRegistryFallback:
    """Verify the entity-registry-only fallback still works for unknown types."""

    async def test_unknown_type_uses_entity_registry(self, register_tools, mock_client):
        """Unknown helper types should fall back to entity registry update."""
        mock_client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {"entity_entry": {"entity_id": "unknown_type.test"}},
            }
        )

        with patch(
            "ha_mcp.tools.tools_config_helpers.wait_for_entity_registered",
            new_callable=AsyncMock,
            return_value=True,
        ):
            result = await register_tools["ha_config_set_helper"](
                helper_type="unknown_type",
                helper_id="test",
                name="Test",
            )

        assert result["success"] is True
        ws_calls = mock_client.send_websocket_message.call_args_list
        # Should use entity registry, not {type}/update
        update_call = ws_calls[0][0][0]
        assert update_call["type"] == "config/entity_registry/update"
