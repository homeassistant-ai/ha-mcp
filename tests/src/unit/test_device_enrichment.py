"""Unit tests for device enrichment logic in tools_registry.

Tests identifier parsing (ZHA ieee_address, Z-Wave node_id extraction)
and graceful degradation when WebSocket enrichment calls fail.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_registry import register_registry_tools


def _register_and_capture(mock_client):
    """Register registry tools with a mock MCP and return captured functions."""
    mock_mcp = MagicMock()
    captured = {}

    def fake_tool(**kwargs):
        def decorator(fn):
            captured[fn.__name__] = fn
            return fn
        return decorator

    mock_mcp.tool = fake_tool
    register_registry_tools(mock_mcp, mock_client)
    return captured


def _make_device(
    device_id: str = "test_device_123",
    name: str = "Test Device",
    identifiers: list | None = None,
    connections: list | None = None,
    config_entries: list | None = None,
    **kwargs,
):
    """Build a mock device registry entry."""
    return {
        "id": device_id,
        "name": name,
        "name_by_user": None,
        "manufacturer": "Test Mfg",
        "model": "Test Model",
        "sw_version": "1.0",
        "hw_version": None,
        "serial_number": None,
        "area_id": None,
        "via_device_id": None,
        "disabled_by": None,
        "labels": [],
        "config_entries": config_entries or ["entry_1"],
        "connections": connections or [],
        "identifiers": identifiers or [],
        **kwargs,
    }


def _mock_client_with_device(device: dict, ws_responses: dict | None = None):
    """Create a mock client that returns the given device from registry list."""
    mock_client = MagicMock()

    async def mock_ws(msg, **kwargs):
        msg_type = msg.get("type", "") if isinstance(msg, dict) else ""

        # Device registry list
        if msg_type == "config/device_registry/list":
            return {"success": True, "result": [device]}

        # Entity registry list
        if msg_type == "config/entity_registry/list":
            return {"success": True, "result": []}

        # Custom responses for enrichment calls
        if ws_responses and msg_type in ws_responses:
            return ws_responses[msg_type]

        return {"success": False, "error": f"Unknown type: {msg_type}"}

    mock_client.send_websocket_message = AsyncMock(side_effect=mock_ws)
    return mock_client


class TestZWaveNodeIdParsing:
    """Tests for Z-Wave JS node_id extraction from device identifiers."""

    @pytest.mark.asyncio
    async def test_zwave_node_id_extracted_from_identifier(self):
        """Z-Wave identifier ["zwave_js", "3232323232-5"] should extract node_id="5"."""
        device = _make_device(
            identifiers=[["zwave_js", "3232323232-5"]],
        )
        mock_client = _mock_client_with_device(device, {
            "zwave_js/node_status": {
                "success": True,
                "result": {
                    "node_id": 5,
                    "status": "alive",
                    "is_routing": True,
                    "is_secure": True,
                    "highest_security_class": "S2_Authenticated",
                    "zwave_plus_version": 2,
                    "is_controller_node": False,
                },
            },
        })
        captured = _register_and_capture(mock_client)
        result = await captured["ha_get_device"](device_id="test_device_123")

        assert result["success"] is True
        dev = result["device"]
        assert dev["integration_type"] == "zwave_js"
        assert dev["node_id"] == "5"
        assert "node_status" in dev
        assert dev["node_status"]["status"] == "alive"

    @pytest.mark.asyncio
    async def test_zwave_identifier_no_dash(self):
        """Z-Wave identifier without dash should not extract node_id."""
        device = _make_device(
            identifiers=[["zwave_js", "3232323232"]],
        )
        mock_client = _mock_client_with_device(device)
        captured = _register_and_capture(mock_client)
        result = await captured["ha_get_device"](device_id="test_device_123")

        assert result["success"] is True
        dev = result["device"]
        assert dev["integration_type"] == "zwave_js"
        assert "node_id" not in dev
        assert "node_status" not in dev

    @pytest.mark.asyncio
    async def test_zwave_identifier_multiple_dashes(self):
        """Z-Wave identifier with multiple dashes should extract first segment after first dash."""
        device = _make_device(
            identifiers=[["zwave_js", "3232323232-5-extra"]],
        )
        mock_client = _mock_client_with_device(device, {
            "zwave_js/node_status": {
                "success": True,
                "result": {
                    "node_id": 5,
                    "status": "alive",
                    "is_routing": False,
                    "is_secure": False,
                },
            },
        })
        captured = _register_and_capture(mock_client)
        result = await captured["ha_get_device"](device_id="test_device_123")

        dev = result["device"]
        # split("-")[1] gives "5" even with extra dashes
        assert dev["node_id"] == "5"


class TestZHAIeeeAddressParsing:
    """Tests for ZHA IEEE address extraction."""

    @pytest.mark.asyncio
    async def test_zha_ieee_extracted(self):
        """ZHA identifier should extract ieee_address."""
        device = _make_device(
            identifiers=[["zha", "00:11:22:33:44:55:66:77"]],
        )
        mock_client = _mock_client_with_device(device, {
            "zha/devices": {
                "success": True,
                "result": [
                    {
                        "ieee": "00:11:22:33:44:55:66:77",
                        "lqi": 200,
                        "rssi": -45,
                    }
                ],
            },
        })
        captured = _register_and_capture(mock_client)
        result = await captured["ha_get_device"](device_id="test_device_123")

        dev = result["device"]
        assert dev["integration_type"] == "zha"
        assert dev["ieee_address"] == "00:11:22:33:44:55:66:77"
        assert "radio_metrics" in dev
        assert dev["radio_metrics"]["lqi"] == 200
        assert dev["radio_metrics"]["rssi"] == -45


class TestEnrichmentGracefulDegradation:
    """Tests that device response still succeeds when enrichment calls fail."""

    @pytest.mark.asyncio
    async def test_zha_enrichment_timeout_still_returns_device(self):
        """ZHA radio metrics timeout should not fail the device lookup."""
        device = _make_device(
            identifiers=[["zha", "00:11:22:33:44:55:66:77"]],
        )

        call_count = 0

        async def mock_ws(msg, **kwargs):
            nonlocal call_count
            msg_type = msg.get("type", "") if isinstance(msg, dict) else ""
            if msg_type == "config/device_registry/list":
                return {"success": True, "result": [device]}
            if msg_type == "config/entity_registry/list":
                return {"success": True, "result": []}
            if msg_type == "zha/devices":
                call_count += 1
                raise TimeoutError("Connection timed out")
            return {"success": False}

        mock_client = MagicMock()
        mock_client.send_websocket_message = AsyncMock(side_effect=mock_ws)

        captured = _register_and_capture(mock_client)
        result = await captured["ha_get_device"](device_id="test_device_123")

        assert result["success"] is True
        assert "radio_metrics" not in result["device"]
        assert call_count == 1  # Enrichment was attempted

    @pytest.mark.asyncio
    async def test_zwave_enrichment_oserror_still_returns_device(self):
        """Z-Wave node status OSError should not fail the device lookup."""
        device = _make_device(
            identifiers=[["zwave_js", "3232323232-5"]],
        )

        async def mock_ws(msg, **kwargs):
            msg_type = msg.get("type", "") if isinstance(msg, dict) else ""
            if msg_type == "config/device_registry/list":
                return {"success": True, "result": [device]}
            if msg_type == "config/entity_registry/list":
                return {"success": True, "result": []}
            if msg_type == "zwave_js/node_status":
                raise OSError("Network unreachable")
            return {"success": False}

        mock_client = MagicMock()
        mock_client.send_websocket_message = AsyncMock(side_effect=mock_ws)

        captured = _register_and_capture(mock_client)
        result = await captured["ha_get_device"](device_id="test_device_123")

        assert result["success"] is True
        assert "node_status" not in result["device"]
        assert result["device"]["node_id"] == "5"  # Parsing still works
