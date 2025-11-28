"""Unit tests for ZHA device detection tools."""

import pytest
from unittest.mock import AsyncMock

from ha_mcp.tools.tools_zha import register_zha_tools


class MockMCP:
    """Mock MCP server for testing tool registration."""

    def __init__(self):
        self.tools = {}

    def tool(self, annotations=None):
        """Decorator to capture registered tools."""
        def decorator(func):
            self.tools[func.__name__] = func
            return func
        return decorator


@pytest.fixture
def mock_client():
    """Create a mock Home Assistant client."""
    client = AsyncMock()
    return client


@pytest.fixture
def mock_mcp():
    """Create a mock MCP server."""
    return MockMCP()


@pytest.fixture
def registered_tools(mock_mcp, mock_client):
    """Register ZHA tools and return the MCP with tools."""
    register_zha_tools(mock_mcp, mock_client)
    return mock_mcp, mock_client


class TestHaGetZhaDevices:
    """Test ha_get_zha_devices tool."""

    @pytest.mark.asyncio
    async def test_list_zha_devices_success(self, registered_tools):
        """Test successfully listing ZHA devices."""
        mock_mcp, mock_client = registered_tools

        # Mock device registry response with ZHA devices
        mock_client.send_websocket_message.return_value = {
            "success": True,
            "result": [
                {
                    "id": "device_1",
                    "name": "ZHA Button",
                    "name_by_user": None,
                    "manufacturer": "IKEA",
                    "model": "TRADFRI Remote",
                    "sw_version": "1.0",
                    "area_id": "living_room",
                    "identifiers": [["zha", "00:11:22:33:44:55:66:77"]],
                    "connections": [],
                    "via_device_id": "coordinator_1",
                },
                {
                    "id": "device_2",
                    "name": "ZHA Light",
                    "name_by_user": "Kitchen Light",
                    "manufacturer": "Philips",
                    "model": "Hue Bulb",
                    "sw_version": "2.0",
                    "area_id": "kitchen",
                    "identifiers": [["zha", "AA:BB:CC:DD:EE:FF:00:11"]],
                    "connections": [["ieee", "AA:BB:CC:DD:EE:FF:00:11"]],
                    "via_device_id": "coordinator_1",
                },
                {
                    "id": "device_3",
                    "name": "Non-ZHA Device",
                    "manufacturer": "Other",
                    "model": "Something",
                    "identifiers": [["hue", "12345"]],
                    "connections": [],
                },
            ],
        }

        tool_func = mock_mcp.tools["ha_get_zha_devices"]
        result = await tool_func(include_entities=False)

        assert result["success"] is True
        assert result["count"] == 2
        assert len(result["devices"]) == 2

        # Verify first device
        device_1 = result["devices"][0]
        assert device_1["device_id"] == "device_1"
        assert device_1["ieee_address"] == "00:11:22:33:44:55:66:77"
        assert device_1["manufacturer"] == "IKEA"

        # Verify second device (with user name)
        device_2 = result["devices"][1]
        assert device_2["name"] == "Kitchen Light"
        assert device_2["ieee_address"] == "AA:BB:CC:DD:EE:FF:00:11"

    @pytest.mark.asyncio
    async def test_list_zha_devices_with_area_filter(self, registered_tools):
        """Test filtering ZHA devices by area."""
        mock_mcp, mock_client = registered_tools

        mock_client.send_websocket_message.return_value = {
            "success": True,
            "result": [
                {
                    "id": "device_1",
                    "name": "Living Room Button",
                    "area_id": "living_room",
                    "identifiers": [["zha", "00:11:22:33:44:55:66:77"]],
                    "connections": [],
                },
                {
                    "id": "device_2",
                    "name": "Kitchen Light",
                    "area_id": "kitchen",
                    "identifiers": [["zha", "AA:BB:CC:DD:EE:FF:00:11"]],
                    "connections": [],
                },
            ],
        }

        tool_func = mock_mcp.tools["ha_get_zha_devices"]
        result = await tool_func(area_id="living_room", include_entities=False)

        assert result["success"] is True
        assert result["count"] == 1
        assert result["devices"][0]["device_id"] == "device_1"
        assert result["area_filter"] == "living_room"

    @pytest.mark.asyncio
    async def test_list_zha_devices_empty(self, registered_tools):
        """Test when no ZHA devices are found."""
        mock_mcp, mock_client = registered_tools

        mock_client.send_websocket_message.return_value = {
            "success": True,
            "result": [
                {
                    "id": "device_1",
                    "name": "Non-ZHA Device",
                    "identifiers": [["hue", "12345"]],
                    "connections": [],
                },
            ],
        }

        tool_func = mock_mcp.tools["ha_get_zha_devices"]
        result = await tool_func(include_entities=False)

        assert result["success"] is True
        assert result["count"] == 0
        assert result["devices"] == []

    @pytest.mark.asyncio
    async def test_list_zha_devices_with_entities(self, registered_tools):
        """Test including entities in ZHA device listing."""
        mock_mcp, mock_client = registered_tools

        # Mock responses for both device and entity registry
        def mock_websocket(message):
            if message["type"] == "config/device_registry/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "id": "device_1",
                            "name": "ZHA Button",
                            "identifiers": [["zha", "00:11:22:33:44:55:66:77"]],
                            "connections": [],
                        },
                    ],
                }
            elif message["type"] == "config/entity_registry/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "sensor.zha_button_battery",
                            "device_id": "device_1",
                            "name": "Battery",
                            "platform": "zha",
                        },
                        {
                            "entity_id": "binary_sensor.zha_button_action",
                            "device_id": "device_1",
                            "name": "Action",
                            "platform": "zha",
                        },
                    ],
                }
            return {"success": False}

        mock_client.send_websocket_message.side_effect = mock_websocket

        tool_func = mock_mcp.tools["ha_get_zha_devices"]
        result = await tool_func(include_entities=True)

        assert result["success"] is True
        assert result["count"] == 1
        device = result["devices"][0]
        assert len(device["entities"]) == 2
        assert device["entities"][0]["entity_id"] == "sensor.zha_button_battery"

    @pytest.mark.asyncio
    async def test_list_zha_devices_registry_error(self, registered_tools):
        """Test handling device registry access error."""
        mock_mcp, mock_client = registered_tools

        mock_client.send_websocket_message.return_value = {
            "success": False,
            "error": "Connection failed",
        }

        tool_func = mock_mcp.tools["ha_get_zha_devices"]
        result = await tool_func(include_entities=False)

        assert result["success"] is False
        assert "error" in result


class TestHaGetDeviceIntegration:
    """Test ha_get_device_integration tool."""

    @pytest.mark.asyncio
    async def test_get_integration_by_device_id(self, registered_tools):
        """Test getting integration info by device_id."""
        mock_mcp, mock_client = registered_tools

        mock_client.send_websocket_message.return_value = {
            "success": True,
            "result": [
                {
                    "id": "device_1",
                    "name": "ZHA Button",
                    "manufacturer": "IKEA",
                    "model": "TRADFRI Remote",
                    "identifiers": [["zha", "00:11:22:33:44:55:66:77"]],
                    "connections": [["ieee", "00:11:22:33:44:55:66:77"]],
                    "config_entries": ["zha_entry_1"],
                },
            ],
        }

        tool_func = mock_mcp.tools["ha_get_device_integration"]
        result = await tool_func(device_id="device_1")

        assert result["success"] is True
        assert result["device_id"] == "device_1"
        assert result["integration_type"] == "zha"
        assert "zha" in result["integration_sources"]
        assert result["ieee_address"] == "00:11:22:33:44:55:66:77"
        assert "zha_trigger_hint" in result

    @pytest.mark.asyncio
    async def test_get_integration_by_entity_id(self, registered_tools):
        """Test getting integration info by entity_id."""
        mock_mcp, mock_client = registered_tools

        def mock_websocket(message):
            if message["type"] == "config/entity_registry/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "light.living_room",
                            "device_id": "device_1",
                            "platform": "zha",
                        },
                    ],
                }
            elif message["type"] == "config/device_registry/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "id": "device_1",
                            "name": "ZHA Light",
                            "identifiers": [["zha", "AA:BB:CC:DD:EE:FF:00:11"]],
                            "connections": [],
                            "config_entries": ["zha_entry_1"],
                        },
                    ],
                }
            return {"success": False}

        mock_client.send_websocket_message.side_effect = mock_websocket

        tool_func = mock_mcp.tools["ha_get_device_integration"]
        result = await tool_func(entity_id="light.living_room")

        assert result["success"] is True
        assert result["queried_entity_id"] == "light.living_room"
        assert result["integration_type"] == "zha"
        assert result["ieee_address"] == "AA:BB:CC:DD:EE:FF:00:11"

    @pytest.mark.asyncio
    async def test_get_integration_mqtt_device(self, registered_tools):
        """Test identifying MQTT device."""
        mock_mcp, mock_client = registered_tools

        mock_client.send_websocket_message.return_value = {
            "success": True,
            "result": [
                {
                    "id": "device_1",
                    "name": "MQTT Sensor",
                    "identifiers": [["mqtt", "sensor_123"]],
                    "connections": [],
                    "config_entries": ["mqtt_entry_1"],
                },
            ],
        }

        tool_func = mock_mcp.tools["ha_get_device_integration"]
        result = await tool_func(device_id="device_1")

        assert result["success"] is True
        assert result["integration_type"] == "mqtt"
        assert "ieee_address" not in result

    @pytest.mark.asyncio
    async def test_get_integration_missing_params(self, registered_tools):
        """Test error when neither device_id nor entity_id provided."""
        mock_mcp, mock_client = registered_tools

        tool_func = mock_mcp.tools["ha_get_device_integration"]
        result = await tool_func()

        assert result["success"] is False
        assert "Either device_id or entity_id must be provided" in result["error"]

    @pytest.mark.asyncio
    async def test_get_integration_entity_not_found(self, registered_tools):
        """Test error when entity not found."""
        mock_mcp, mock_client = registered_tools

        mock_client.send_websocket_message.return_value = {
            "success": True,
            "result": [],
        }

        tool_func = mock_mcp.tools["ha_get_device_integration"]
        result = await tool_func(entity_id="light.nonexistent")

        assert result["success"] is False
        assert "Entity not found" in result["error"]

    @pytest.mark.asyncio
    async def test_get_integration_entity_without_device(self, registered_tools):
        """Test error when entity has no associated device."""
        mock_mcp, mock_client = registered_tools

        mock_client.send_websocket_message.return_value = {
            "success": True,
            "result": [
                {
                    "entity_id": "input_boolean.test",
                    "device_id": None,
                    "platform": "input_boolean",
                },
            ],
        }

        tool_func = mock_mcp.tools["ha_get_device_integration"]
        result = await tool_func(entity_id="input_boolean.test")

        assert result["success"] is False
        assert "not associated with a device" in result["error"]

    @pytest.mark.asyncio
    async def test_get_integration_device_not_found(self, registered_tools):
        """Test error when device not found."""
        mock_mcp, mock_client = registered_tools

        mock_client.send_websocket_message.return_value = {
            "success": True,
            "result": [],
        }

        tool_func = mock_mcp.tools["ha_get_device_integration"]
        result = await tool_func(device_id="nonexistent")

        assert result["success"] is False
        assert "Device not found" in result["error"]

    @pytest.mark.asyncio
    async def test_get_integration_zigbee2mqtt_detection(self, registered_tools):
        """Test detecting Zigbee2MQTT devices."""
        mock_mcp, mock_client = registered_tools

        mock_client.send_websocket_message.return_value = {
            "success": True,
            "result": [
                {
                    "id": "device_1",
                    "name": "Z2M Light",
                    "identifiers": [["mqtt", "zigbee2mqtt_0x1234"]],
                    "connections": [["ieee", "00:11:22:33:44:55:66:77"]],
                    "config_entries": ["mqtt_entry_1"],
                },
            ],
        }

        tool_func = mock_mcp.tools["ha_get_device_integration"]
        result = await tool_func(device_id="device_1")

        assert result["success"] is True
        assert result["integration_type"] == "zigbee2mqtt"
        assert result["ieee_address"] == "00:11:22:33:44:55:66:77"
