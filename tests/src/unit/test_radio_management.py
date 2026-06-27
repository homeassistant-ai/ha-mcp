"""Unit tests for radio management: the ha_manage_radio dispatcher and the
Matter diagnostics enrichment added to ha_get_device.

The per-radio handler modules (zwave/zigbee/thread) are exercised by their own
tests once implemented; this module covers the dispatcher contract (action
validation, required-param + destructive-confirm gates, entity resolution) and
the Matter handler + enricher end to end against a mocked WebSocket client.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_radio import register_radio_tools
from ha_mcp.tools.tools_registry import register_registry_tools


def _capture(register, mock_client):
    """Register tools onto a mock MCP and return {tool_name: bound_method}."""
    mock_mcp = MagicMock()
    captured: dict = {}

    def capture_add_tool(method):
        fmcp = getattr(method, "__fastmcp__", None)
        name = (fmcp.name if fmcp else None) or method.__name__
        captured[name] = method

    mock_mcp.add_tool = capture_add_tool
    register(mock_mcp, mock_client)
    return captured


def _client(routes: dict, *, record: list | None = None):
    """Mock client whose send_websocket_message routes by message ``type``.

    ``routes`` maps a ``type`` string to either a response dict or a callable
    taking the message and returning/raising. ``record`` (if given) collects
    every outgoing message for call assertions.
    """
    mock_client = MagicMock()

    async def mock_ws(msg, **kwargs):
        msg_type = msg.get("type", "") if isinstance(msg, dict) else ""
        if record is not None:
            record.append(msg)
        handler = routes.get(msg_type)
        if callable(handler):
            return handler(msg)
        if handler is not None:
            return handler
        return {"success": False, "error": f"Unknown type: {msg_type}"}

    mock_client.send_websocket_message = AsyncMock(side_effect=mock_ws)
    return mock_client


def _matter_device(device_id: str = "m1"):
    return {
        "id": device_id,
        "name": "Matter Bulb",
        "name_by_user": None,
        "manufacturer": "Acme",
        "model": "Bulb",
        "sw_version": "1.0",
        "hw_version": None,
        "serial_number": None,
        "area_id": None,
        "via_device_id": None,
        "disabled_by": None,
        "labels": [],
        "config_entries": ["e1"],
        "connections": [],
        "identifiers": [["matter", "deadbeef-1"]],
    }


_DIAG = {
    "node_id": 1,
    "network_type": "THREAD",
    "node_type": "END_DEVICE",
    "network_name": "my-thread",
    "ip_addresses": ["fe80::1"],
    "mac_address": "aa:bb",
    "available": True,
    "active_fabrics": [{"fabric_index": 2, "fabric_id": 99}],
    "active_fabric_index": 2,
}


# --------------------------------------------------------------------------- #
# Matter enrichment in ha_get_device
# --------------------------------------------------------------------------- #


class TestMatterEnrichment:
    @pytest.mark.asyncio
    async def test_matter_device_gets_node_diagnostics(self):
        device = _matter_device()
        client = _client(
            {
                "config/device_registry/list": {"success": True, "result": [device]},
                "config/entity_registry/list": {"success": True, "result": []},
                "matter/node_diagnostics": {"success": True, "result": _DIAG},
            }
        )
        captured = _capture(register_registry_tools, client)
        result = await captured["ha_get_device"](device_id="m1")

        dev = result["device"]
        assert dev["integration_type"] == "matter"
        assert dev["node_diagnostics"]["network_type"] == "THREAD"
        assert dev["node_diagnostics"]["available"] is True
        assert dev["node_diagnostics"]["active_fabric_index"] == 2

    @pytest.mark.asyncio
    async def test_matter_enrichment_oserror_still_returns_device(self):
        device = _matter_device()

        def boom(_msg):
            raise OSError("Network unreachable")

        client = _client(
            {
                "config/device_registry/list": {"success": True, "result": [device]},
                "config/entity_registry/list": {"success": True, "result": []},
                "matter/node_diagnostics": boom,
            }
        )
        captured = _capture(register_registry_tools, client)
        result = await captured["ha_get_device"](device_id="m1")

        assert result["success"] is True
        assert "node_diagnostics" not in result["device"]


# --------------------------------------------------------------------------- #
# ha_manage_radio dispatcher + Matter handler
# --------------------------------------------------------------------------- #


class TestManageRadioDispatcher:
    @pytest.mark.asyncio
    async def test_matter_diagnostics(self):
        client = _client({"matter/node_diagnostics": {"success": True, "result": _DIAG}})
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        out = await radio(radio="matter", action="diagnostics", device_id="m1")
        assert out["success"] is True
        assert out["radio"] == "matter"
        assert out["diagnostics"]["network_type"] == "THREAD"

    @pytest.mark.asyncio
    async def test_matter_ping(self):
        client = _client(
            {"matter/ping_node": {"success": True, "result": {"fe80::1": True}}}
        )
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        out = await radio(radio="matter", action="ping", device_id="m1")
        assert out["reachability"] == {"fe80::1": True}

    @pytest.mark.asyncio
    async def test_unknown_action_lists_supported(self):
        client = _client({})
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        with pytest.raises(ToolError) as exc:
            await radio(radio="matter", action="nope", device_id="m1")
        assert "diagnostics" in str(exc.value)  # supported list surfaced

    @pytest.mark.asyncio
    async def test_missing_required_param(self):
        client = _client({})
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        with pytest.raises(ToolError) as exc:
            await radio(radio="matter", action="commission")  # missing code
        assert "code" in str(exc.value)

    @pytest.mark.asyncio
    async def test_destructive_requires_confirm(self):
        client = _client({})
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        with pytest.raises(ToolError) as exc:
            await radio(radio="matter", action="remove_fabric", device_id="m1")
        assert "confirm" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_remove_fabric_autoresolves_fabric_index(self):
        record: list = []
        client = _client(
            {
                "matter/node_diagnostics": {"success": True, "result": _DIAG},
                "matter/remove_matter_fabric": {"success": True, "result": {}},
            },
            record=record,
        )
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        out = await radio(
            radio="matter", action="remove_fabric", device_id="m1", confirm=True
        )
        assert out["fabric_index"] == 2  # resolved from node diagnostics
        removed = [m for m in record if m["type"] == "matter/remove_matter_fabric"]
        assert removed and removed[0]["fabric_index"] == 2

    @pytest.mark.asyncio
    async def test_share_out_returns_codes(self):
        codes = {
            "setup_pin_code": 1234,
            "setup_manual_code": "111-22",
            "setup_qr_code": "MT:ABC",
        }
        client = _client(
            {"matter/open_commissioning_window": {"success": True, "result": codes}}
        )
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        out = await radio(radio="matter", action="share_out", device_id="m1")
        assert out["commissioning"]["setup_manual_code"] == "111-22"

    @pytest.mark.asyncio
    async def test_entity_id_resolves_to_device(self):
        record: list = []
        client = _client(
            {
                "config/entity_registry/list": {
                    "success": True,
                    "result": [{"entity_id": "light.m", "device_id": "m1"}],
                },
                "matter/node_diagnostics": {"success": True, "result": _DIAG},
            },
            record=record,
        )
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        out = await radio(radio="matter", action="diagnostics", entity_id="light.m")
        assert out["diagnostics"]["network_type"] == "THREAD"
        diag = [m for m in record if m["type"] == "matter/node_diagnostics"]
        assert diag and diag[0]["device_id"] == "m1"
