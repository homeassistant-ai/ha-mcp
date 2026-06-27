"""Unit tests for the Zigbee (ZHA) handler behind ``ha_manage_radio``.

Mirrors ``test_radio_management.py``'s ``TestManageRadioDispatcher`` style: tools
are registered onto a mock MCP via ``register_radio_tools`` and a mock client
routes WebSocket commands by message ``type`` (and records service calls). Tests
assert the success envelopes plus the destructive-confirm / required-param gates
the dispatcher enforces against the handler's ``SUPPORTED`` specs.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_radio import register_radio_tools

_IEEE = "00:11:22:33:44:55:66:77"


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


def _client(
    routes: dict,
    *,
    record: list | None = None,
    service_result=None,
    service_calls: list | None = None,
):
    """Mock client: ``send_websocket_message`` routes by ``type``; ``call_service``
    records (domain, service, data) and returns ``service_result`` (default [])."""
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

    async def mock_service(domain, service, data=None, return_response=False):
        if service_calls is not None:
            service_calls.append(
                {"domain": domain, "service": service, "data": data or {}}
            )
        return service_result if service_result is not None else []

    mock_client.call_service = AsyncMock(side_effect=mock_service)
    return mock_client


def _zha_device(device_id: str = "z1", ieee: str = _IEEE):
    return {
        "id": device_id,
        "name": "Zigbee Bulb",
        "identifiers": [["zha", ieee]],
        "connections": [],
    }


def _device_registry(*devices):
    return {"success": True, "result": list(devices)}


def _radio(client):
    return _capture(register_radio_tools, client)["ha_manage_radio"]


# --------------------------------------------------------------------------- #
# Read actions
# --------------------------------------------------------------------------- #


class TestZigbeeReads:
    @pytest.mark.asyncio
    async def test_diagnostics_resolves_ieee_and_returns_metrics(self):
        record: list = []
        client = _client(
            {
                "config/device_registry/list": _device_registry(_zha_device()),
                "zha/device": {
                    "success": True,
                    "result": {
                        "ieee": _IEEE,
                        "lqi": 200,
                        "rssi": -55,
                        "available": True,
                    },
                },
            },
            record=record,
        )
        out = await _radio(client)(radio="zigbee", action="diagnostics", device_id="z1")
        assert out["success"] is True
        assert out["radio"] == "zigbee"
        assert out["ieee"] == _IEEE
        assert out["diagnostics"]["lqi"] == 200
        # zha/device keyed on the resolved IEEE, not the device_id.
        dev = [m for m in record if m["type"] == "zha/device"]
        assert dev and dev[0]["ieee"] == _IEEE

    @pytest.mark.asyncio
    async def test_network_status_returns_settings_and_kicks_topology(self):
        record: list = []
        client = _client(
            {
                "config_entries/get": {
                    "success": True,
                    "result": [{"domain": "zha", "entry_id": "e1"}],
                },
                "zha/network/settings": {
                    "success": True,
                    "result": {"network": {"channel": 15, "pan_id": 4660}},
                },
                "zha/topology/update": {"success": True, "result": None},
            },
            record=record,
        )
        out = await _radio(client)(radio="zigbee", action="network_status")
        assert out["config_entry_id"] == "e1"
        assert out["network"]["network"]["channel"] == 15
        assert any(m["type"] == "zha/topology/update" for m in record)

    @pytest.mark.asyncio
    async def test_network_status_topology_failure_is_swallowed(self):
        client = _client(
            {
                "config_entries/get": {
                    "success": True,
                    "result": [{"domain": "zha", "entry_id": "e1"}],
                },
                "zha/network/settings": {"success": True, "result": {"channel": 20}},
                "zha/topology/update": {"success": False, "error": "scan busy"},
            }
        )
        out = await _radio(client)(radio="zigbee", action="network_status")
        assert out["success"] is True
        assert out["network"]["channel"] == 20

    @pytest.mark.asyncio
    async def test_network_status_not_configured_is_degraded(self):
        client = _client({"config_entries/get": {"success": True, "result": []}})
        out = await _radio(client)(radio="zigbee", action="network_status")
        assert out["available"] is False
        assert out["warnings"]

    @pytest.mark.asyncio
    async def test_cluster_read_defaults_cluster_type_in(self):
        record: list = []
        client = _client(
            {
                "config/device_registry/list": _device_registry(_zha_device()),
                "zha/devices/clusters/attributes/value": {
                    "success": True,
                    "result": {"value": True},
                },
            },
            record=record,
        )
        out = await _radio(client)(
            radio="zigbee",
            action="cluster_read",
            device_id="z1",
            params={"endpoint_id": 1, "cluster_id": 6, "attribute": "on_off"},
        )
        assert out["value"]["value"] is True
        reads = [
            m for m in record if m["type"] == "zha/devices/clusters/attributes/value"
        ]
        assert reads and reads[0]["cluster_type"] == "in"
        assert reads[0]["ieee"] == _IEEE


# --------------------------------------------------------------------------- #
# Node management (WS + service)
# --------------------------------------------------------------------------- #


class TestZigbeeNodeManagement:
    @pytest.mark.asyncio
    async def test_permit_join_defaults_duration_no_ieee(self):
        record: list = []
        client = _client(
            {"zha/devices/permit": {"success": True, "result": None}}, record=record
        )
        out = await _radio(client)(radio="zigbee", action="permit_join")
        assert out["duration"] == 60
        assert out["ieee"] is None
        permits = [m for m in record if m["type"] == "zha/devices/permit"]
        assert permits and permits[0]["duration"] == 60
        # ws_call drops None fields — no per-device ieee on a network-wide permit.
        assert "ieee" not in permits[0]

    @pytest.mark.asyncio
    async def test_permit_join_with_device_resolves_ieee(self):
        record: list = []
        client = _client(
            {
                "config/device_registry/list": _device_registry(_zha_device()),
                "zha/devices/permit": {"success": True, "result": None},
            },
            record=record,
        )
        out = await _radio(client)(
            radio="zigbee",
            action="permit_join",
            device_id="z1",
            params={"duration": 30},
        )
        assert out["duration"] == 30
        assert out["ieee"] == _IEEE
        permits = [m for m in record if m["type"] == "zha/devices/permit"]
        assert permits and permits[0]["ieee"] == _IEEE

    @pytest.mark.asyncio
    async def test_remove_device_calls_service_with_ieee(self):
        calls: list = []
        client = _client(
            {"config/device_registry/list": _device_registry(_zha_device())},
            service_calls=calls,
        )
        out = await _radio(client)(
            radio="zigbee", action="remove_device", device_id="z1", confirm=True
        )
        assert out["ieee"] == _IEEE
        assert calls == [
            {"domain": "zha", "service": "remove", "data": {"ieee": _IEEE}}
        ]

    @pytest.mark.asyncio
    async def test_remove_device_requires_confirm(self):
        client = _client(
            {"config/device_registry/list": _device_registry(_zha_device())}
        )
        with pytest.raises(ToolError) as exc:
            await _radio(client)(radio="zigbee", action="remove_device", device_id="z1")
        assert "confirm" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_reconfigure_uses_websocket(self):
        record: list = []
        client = _client(
            {
                "config/device_registry/list": _device_registry(_zha_device()),
                "zha/devices/reconfigure": {"success": True, "result": None},
            },
            record=record,
        )
        out = await _radio(client)(radio="zigbee", action="reconfigure", device_id="z1")
        assert out["ieee"] == _IEEE
        assert any(m["type"] == "zha/devices/reconfigure" for m in record)

    @pytest.mark.asyncio
    async def test_non_zha_device_raises(self):
        client = _client(
            {
                "config/device_registry/list": _device_registry(
                    {"id": "z1", "identifiers": [["hue", "abc"]], "connections": []}
                )
            }
        )
        with pytest.raises(ToolError) as exc:
            await _radio(client)(radio="zigbee", action="diagnostics", device_id="z1")
        assert "ZHA" in str(exc.value)

    @pytest.mark.asyncio
    async def test_firmware_update_installs_update_entity(self):
        calls: list = []
        client = _client(
            {
                "config/entity_registry/list": {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "sensor.z1_lqi",
                            "device_id": "z1",
                            "platform": "zha",
                        },
                        {
                            "entity_id": "update.z1_firmware",
                            "device_id": "z1",
                            "platform": "zha",
                        },
                    ],
                }
            },
            service_calls=calls,
        )
        out = await _radio(client)(
            radio="zigbee", action="firmware_update", device_id="z1", confirm=True
        )
        assert out["entity_id"] == "update.z1_firmware"
        assert out["long_running"] is True
        assert calls == [
            {
                "domain": "update",
                "service": "install",
                "data": {"entity_id": "update.z1_firmware"},
            }
        ]


# --------------------------------------------------------------------------- #
# Groups / bindings
# --------------------------------------------------------------------------- #


class TestZigbeeGroupsAndBindings:
    @pytest.mark.asyncio
    async def test_group_add(self):
        client = _client(
            {"zha/group/add": {"success": True, "result": {"group_id": 1}}}
        )
        out = await _radio(client)(
            radio="zigbee", action="group_add", params={"group_name": "Kitchen"}
        )
        assert out["group"]["group_id"] == 1

    @pytest.mark.asyncio
    async def test_group_add_missing_name_raises(self):
        client = _client({})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(radio="zigbee", action="group_add")
        assert "group_name" in str(exc.value)

    @pytest.mark.asyncio
    async def test_group_remove_requires_confirm(self):
        client = _client({})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(
                radio="zigbee", action="group_remove", params={"group_ids": [1]}
            )
        assert "confirm" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_bind_sends_both_ieees(self):
        record: list = []
        client = _client(
            {"zha/devices/bind": {"success": True, "result": None}}, record=record
        )
        out = await _radio(client)(
            radio="zigbee",
            action="bind",
            params={"source_ieee": "aa", "target_ieee": "bb"},
        )
        assert out["success"] is True
        binds = [m for m in record if m["type"] == "zha/devices/bind"]
        assert binds and binds[0]["source_ieee"] == "aa"
        assert binds[0]["target_ieee"] == "bb"

    @pytest.mark.asyncio
    async def test_unbind_requires_confirm(self):
        client = _client({})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(
                radio="zigbee",
                action="unbind",
                params={"source_ieee": "aa", "target_ieee": "bb"},
            )
        assert "confirm" in str(exc.value).lower()


# --------------------------------------------------------------------------- #
# Cluster writes / network ops (service + destructive WS)
# --------------------------------------------------------------------------- #


class TestZigbeeClusterAndNetwork:
    @pytest.mark.asyncio
    async def test_cluster_write_calls_service(self):
        calls: list = []
        client = _client(
            {"config/device_registry/list": _device_registry(_zha_device())},
            service_calls=calls,
        )
        out = await _radio(client)(
            radio="zigbee",
            action="cluster_write",
            device_id="z1",
            params={
                "endpoint_id": 1,
                "cluster_id": 8,
                "attribute": "current_level",
                "value": 0,
            },
            confirm=True,
        )
        assert out["ieee"] == _IEEE
        data = calls[0]["data"]
        assert calls[0]["service"] == "set_zigbee_cluster_attribute"
        assert data["cluster_type"] == "in"
        assert data["value"] == 0  # falsy value is preserved
        assert data["ieee"] == _IEEE

    @pytest.mark.asyncio
    async def test_cluster_write_missing_value_raises(self):
        client = _client(
            {"config/device_registry/list": _device_registry(_zha_device())}
        )
        with pytest.raises(ToolError) as exc:
            await _radio(client)(
                radio="zigbee",
                action="cluster_write",
                device_id="z1",
                params={
                    "endpoint_id": 1,
                    "cluster_id": 8,
                    "attribute": "current_level",
                },
                confirm=True,
            )
        assert "value" in str(exc.value)

    @pytest.mark.asyncio
    async def test_cluster_command_calls_service(self):
        calls: list = []
        client = _client(
            {"config/device_registry/list": _device_registry(_zha_device())},
            service_calls=calls,
        )
        out = await _radio(client)(
            radio="zigbee",
            action="cluster_command",
            device_id="z1",
            params={
                "endpoint_id": 1,
                "cluster_id": 6,
                "command": 0,
                "command_type": "server",
            },
            confirm=True,
        )
        assert out["success"] is True
        assert calls[0]["service"] == "issue_zigbee_cluster_command"
        assert calls[0]["data"]["command_type"] == "server"

    @pytest.mark.asyncio
    async def test_network_backup(self):
        client = _client(
            {
                "zha/network/backups/create": {
                    "success": True,
                    "result": {"backup_time": "now"},
                }
            }
        )
        out = await _radio(client)(radio="zigbee", action="network_backup")
        assert out["backup"]["backup_time"] == "now"

    @pytest.mark.asyncio
    async def test_change_channel_requires_confirm(self):
        client = _client({})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(
                radio="zigbee", action="change_channel", params={"new_channel": 25}
            )
        assert "confirm" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_change_channel_with_confirm(self):
        record: list = []
        client = _client(
            {"zha/network/change_channel": {"success": True, "result": None}},
            record=record,
        )
        out = await _radio(client)(
            radio="zigbee",
            action="change_channel",
            params={"new_channel": 25},
            confirm=True,
        )
        assert out["new_channel"] == 25
        changes = [m for m in record if m["type"] == "zha/network/change_channel"]
        assert changes and changes[0]["new_channel"] == 25

    @pytest.mark.asyncio
    async def test_network_restore_requires_confirm(self):
        client = _client({})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(
                radio="zigbee",
                action="network_restore",
                params={"backup": {"some": "backup"}},
            )
        assert "confirm" in str(exc.value).lower()
