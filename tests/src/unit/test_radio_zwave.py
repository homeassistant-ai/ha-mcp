"""Unit tests for the Z-Wave JS handler behind ``ha_manage_radio``.

Mirrors ``test_radio_management.py``'s ``TestManageRadioDispatcher`` style: tools
are registered onto a mock MCP via ``register_radio_tools`` and the mock client
routes ``send_websocket_message`` by message ``type`` (and ``call_service`` for
the service-backed actions: ``ping`` and ``firmware_update``). Tests assert the
success envelopes and that the destructive/required/conditional gates raise
``ToolError``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_radio import register_radio_tools


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


def _client(routes: dict, *, record: list | None = None, service_result=None):
    """Mock client routing ``send_websocket_message`` by ``type``.

    ``routes`` maps a ``type`` string to a response dict or a callable. ``record``
    (if given) collects every outgoing WS message. ``call_service`` is an
    AsyncMock returning ``service_result`` (default ``[]``).
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
    mock_client.call_service = AsyncMock(
        return_value=[] if service_result is None else service_result
    )
    return mock_client


def _radio(client):
    return _capture(register_radio_tools, client)["ha_manage_radio"]


def _entries(present: bool = True):
    """A ``config_entries/get`` response (resolve_entry_id source)."""
    result = [{"domain": "zwave_js", "entry_id": "e1"}] if present else []
    return {"success": True, "result": result}


_NODE = {
    "node_id": 7,
    "status": "alive",
    "is_routing": True,
    "is_secure": True,
    "highest_security_class": 1,
    "zwave_plus_version": 2,
    "is_controller_node": False,
}


class TestZwaveRadio:
    # ---- diagnostics -------------------------------------------------------- #
    @pytest.mark.asyncio
    async def test_diagnostics_projects_node_status(self):
        client = _client({"zwave_js/node_status": {"success": True, "result": _NODE}})
        out = await _radio(client)(radio="zwave", action="diagnostics", device_id="z1")
        assert out["success"] is True
        assert out["radio"] == "zwave"
        assert out["node_status"]["node_id"] == 7
        assert out["node_status"]["highest_security_class"] == 1
        assert out["node_status"]["is_controller_node"] is False

    @pytest.mark.asyncio
    async def test_diagnostics_requires_device_id(self):
        with pytest.raises(ToolError) as exc:
            await _radio(_client({}))(radio="zwave", action="diagnostics")
        assert "device_id" in str(exc.value)

    # ---- network_status ----------------------------------------------------- #
    @pytest.mark.asyncio
    async def test_network_status_summary(self):
        net = {
            "success": True,
            "result": {
                "controller": {
                    "home_id": 1234,
                    "is_primary": True,
                    "nodes": [_NODE, {**_NODE, "node_id": 8}],
                }
            },
        }
        client = _client(
            {"config_entries/get": _entries(), "zwave_js/network_status": net}
        )
        out = await _radio(client)(radio="zwave", action="network_status")
        assert out["config_entry_id"] == "e1"
        assert out["count"] == 2
        assert out["total_count"] == 2
        assert out["nodes"][1]["node_id"] == 8
        # The heavy embedded node list is dropped from the controller view.
        assert "nodes" not in out["controller"]
        assert out["controller"]["home_id"] == 1234
        assert "truncated" not in out

    @pytest.mark.asyncio
    async def test_network_status_caps_and_truncates(self):
        nodes = [{**_NODE, "node_id": i} for i in range(60)]
        net = {"success": True, "result": {"controller": {"nodes": nodes}}}
        client = _client(
            {"config_entries/get": _entries(), "zwave_js/network_status": net}
        )
        out = await _radio(client)(radio="zwave", action="network_status")
        assert out["count"] == 50
        assert out["total_count"] == 60
        assert out["truncated"] is True

    @pytest.mark.asyncio
    async def test_network_status_integration_absent(self):
        client = _client({"config_entries/get": _entries(present=False)})
        out = await _radio(client)(radio="zwave", action="network_status")
        assert out["available"] is False
        assert out["warnings"]

    # ---- ping --------------------------------------------------------------- #
    @pytest.mark.asyncio
    async def test_ping_calls_service(self):
        client = _client({})
        out = await _radio(client)(radio="zwave", action="ping", device_id="z1")
        assert out["success"] is True
        client.call_service.assert_awaited_once_with(
            "zwave_js", "ping", {"device_id": "z1"}
        )

    # ---- add ---------------------------------------------------------------- #
    @pytest.mark.asyncio
    async def test_add_node_requires_a_credential(self):
        client = _client({"config_entries/get": _entries()})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(radio="zwave", action="add")
        assert "qr_provisioning_information" in str(exc.value)

    @pytest.mark.asyncio
    async def test_add_node_with_qr_code_string(self):
        record: list = []
        client = _client(
            {
                "config_entries/get": _entries(),
                "zwave_js/add_node": {"success": True, "result": {}},
            },
            record=record,
        )
        out = await _radio(client)(
            radio="zwave",
            action="add",
            params={"qr_code_string": "90010...", "inclusion_strategy": 2},
        )
        assert out["mode"] == "add_node"
        sent = next(m for m in record if m["type"] == "zwave_js/add_node")
        assert sent["entry_id"] == "e1"
        assert sent["qr_code_string"] == "90010..."
        assert sent["inclusion_strategy"] == 2
        assert "dsk" not in sent  # None fields filtered out

    @pytest.mark.asyncio
    async def test_add_smart_start_requires_qr(self):
        client = _client({"config_entries/get": _entries()})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(
                radio="zwave", action="add", params={"smart_start": True}
            )
        assert "qr_provisioning_information" in str(exc.value)

    @pytest.mark.asyncio
    async def test_add_smart_start_success(self):
        record: list = []
        client = _client(
            {
                "config_entries/get": _entries(),
                "zwave_js/provision_smart_start_node": {"success": True, "result": {}},
            },
            record=record,
        )
        out = await _radio(client)(
            radio="zwave",
            action="add",
            params={"smart_start": True, "qr_provisioning_information": {"dsk": "x"}},
        )
        assert out["mode"] == "smart_start"
        assert any(m["type"] == "zwave_js/provision_smart_start_node" for m in record)

    @pytest.mark.asyncio
    async def test_add_rejects_multiple_credentials(self):
        # HA's add_node schema marks the inclusion credentials as vol.Exclusive;
        # passing more than one must raise (the single-credential path is fine).
        record: list = []
        client = _client(
            {
                "config_entries/get": _entries(),
                "zwave_js/add_node": {"success": True, "result": {}},
            },
            record=record,
        )
        with pytest.raises(ToolError) as exc:
            await _radio(client)(
                radio="zwave",
                action="add",
                params={"qr_code_string": "90010...", "dsk": "11111-22222"},
            )
        msg = str(exc.value)
        assert "qr_code_string" in msg
        assert "dsk" in msg
        assert "mutually exclusive" in msg.lower()
        # The conflicting call must never reach add_node.
        assert not any(m["type"] == "zwave_js/add_node" for m in record)

    @pytest.mark.asyncio
    async def test_add_integration_absent_raises(self):
        # add is a write action: an absent zwave_js config entry must raise
        # (integration_required) before the credential check is reached.
        client = _client({"config_entries/get": _entries(present=False)})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(radio="zwave", action="add")
        assert "zwave_js" in str(exc.value)

    # ---- remove_device ------------------------------------------------------ #
    @pytest.mark.asyncio
    async def test_remove_device_requires_confirm(self):
        with pytest.raises(ToolError) as exc:
            await _radio(_client({}))(
                radio="zwave", action="remove_device", device_id="z1"
            )
        assert "confirm" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_remove_failed_node(self):
        record: list = []
        client = _client(
            {"zwave_js/remove_failed_node": {"success": True, "result": {}}},
            record=record,
        )
        out = await _radio(client)(
            radio="zwave",
            action="remove_device",
            device_id="z1",
            params={"failed": True},
            confirm=True,
        )
        assert out["mode"] == "failed"
        sent = next(m for m in record if m["type"] == "zwave_js/remove_failed_node")
        assert sent["device_id"] == "z1"

    @pytest.mark.asyncio
    async def test_remove_failed_requires_device_id(self):
        client = _client({"config_entries/get": _entries()})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(
                radio="zwave",
                action="remove_device",
                params={"failed": True},
                confirm=True,
            )
        assert "device_id" in str(exc.value)

    @pytest.mark.asyncio
    async def test_remove_device_exclusion(self):
        record: list = []
        client = _client(
            {
                "config_entries/get": _entries(),
                "zwave_js/remove_node": {"success": True, "result": {}},
            },
            record=record,
        )
        out = await _radio(client)(radio="zwave", action="remove_device", confirm=True)
        assert out["mode"] == "exclusion"
        sent = next(m for m in record if m["type"] == "zwave_js/remove_node")
        assert sent["entry_id"] == "e1"

    @pytest.mark.asyncio
    async def test_remove_device_exclusion_integration_absent_raises(self):
        # The exclusion path (no params.failed) opens a controller window, so an
        # absent zwave_js config entry must raise rather than no-op.
        client = _client({"config_entries/get": _entries(present=False)})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(radio="zwave", action="remove_device", confirm=True)
        assert "zwave_js" in str(exc.value)

    # ---- reinterview -------------------------------------------------------- #
    @pytest.mark.asyncio
    async def test_reinterview(self):
        record: list = []
        client = _client(
            {"zwave_js/refresh_node_info": {"success": True, "result": {}}},
            record=record,
        )
        out = await _radio(client)(radio="zwave", action="reinterview", device_id="z1")
        assert out["success"] is True
        assert any(m["type"] == "zwave_js/refresh_node_info" for m in record)

    # ---- rebuild_routes ----------------------------------------------------- #
    @pytest.mark.asyncio
    async def test_rebuild_routes_node(self):
        record: list = []
        client = _client(
            {"zwave_js/rebuild_node_routes": {"success": True, "result": {}}},
            record=record,
        )
        out = await _radio(client)(
            radio="zwave", action="rebuild_routes", device_id="z1"
        )
        assert out["scope"] == "node"
        assert any(m["type"] == "zwave_js/rebuild_node_routes" for m in record)

    @pytest.mark.asyncio
    async def test_rebuild_routes_network(self):
        record: list = []
        client = _client(
            {
                "config_entries/get": _entries(),
                "zwave_js/begin_rebuilding_routes": {"success": True, "result": {}},
            },
            record=record,
        )
        out = await _radio(client)(
            radio="zwave", action="rebuild_routes", params={"scope": "network"}
        )
        assert out["scope"] == "network"
        sent = next(
            m for m in record if m["type"] == "zwave_js/begin_rebuilding_routes"
        )
        assert sent["entry_id"] == "e1"

    @pytest.mark.asyncio
    async def test_rebuild_routes_node_requires_device_id(self):
        with pytest.raises(ToolError) as exc:
            await _radio(_client({}))(radio="zwave", action="rebuild_routes")
        assert "device_id" in str(exc.value)

    @pytest.mark.asyncio
    async def test_rebuild_routes_network_integration_absent_raises(self):
        # The network scope rebuilds via the controller, so an absent zwave_js
        # config entry must raise (integration_required), not silently no-op.
        client = _client({"config_entries/get": _entries(present=False)})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(
                radio="zwave",
                action="rebuild_routes",
                params={"scope": "network"},
            )
        assert "zwave_js" in str(exc.value)

    # ---- set_config_param --------------------------------------------------- #
    @pytest.mark.asyncio
    async def test_set_config_param_requires_value(self):
        with pytest.raises(ToolError) as exc:
            await _radio(_client({}))(
                radio="zwave",
                action="set_config_param",
                device_id="z1",
                params={"property": 5},
            )
        assert "value" in str(exc.value)

    @pytest.mark.asyncio
    async def test_set_config_param_accepts_zero_value(self):
        record: list = []
        client = _client(
            {"zwave_js/set_config_parameter": {"success": True, "result": {}}},
            record=record,
        )
        out = await _radio(client)(
            radio="zwave",
            action="set_config_param",
            device_id="z1",
            params={"property": 5, "value": 0},
        )
        assert out["success"] is True
        sent = next(m for m in record if m["type"] == "zwave_js/set_config_parameter")
        assert sent["property"] == 5
        assert sent["value"] == 0
        assert sent["endpoint"] == 0  # default applied

    # ---- firmware_update ---------------------------------------------------- #
    @pytest.mark.asyncio
    async def test_firmware_update_installs_via_update_entity(self):
        registry = {
            "success": True,
            "result": [
                {"entity_id": "sensor.node_temp", "device_id": "z1"},
                {"entity_id": "update.node_fw", "device_id": "z1"},
            ],
        }
        client = _client({"config/entity_registry/list": registry})
        out = await _radio(client)(
            radio="zwave", action="firmware_update", device_id="z1"
        )
        assert out["entity_id"] == "update.node_fw"
        client.call_service.assert_awaited_once_with(
            "update", "install", {"entity_id": "update.node_fw"}
        )

    @pytest.mark.asyncio
    async def test_firmware_update_no_update_entity(self):
        registry = {
            "success": True,
            "result": [{"entity_id": "sensor.node_temp", "device_id": "z1"}],
        }
        client = _client({"config/entity_registry/list": registry})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(
                radio="zwave", action="firmware_update", device_id="z1"
            )
        assert "update entity" in str(exc.value).lower()

    # ---- hard_reset --------------------------------------------------------- #
    @pytest.mark.asyncio
    async def test_hard_reset_requires_confirm(self):
        with pytest.raises(ToolError) as exc:
            await _radio(_client({}))(radio="zwave", action="hard_reset")
        assert "confirm" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_hard_reset_success(self):
        record: list = []
        client = _client(
            {
                "config_entries/get": _entries(),
                "zwave_js/hard_reset_controller": {"success": True, "result": {}},
            },
            record=record,
        )
        out = await _radio(client)(radio="zwave", action="hard_reset", confirm=True)
        assert out["success"] is True
        sent = next(m for m in record if m["type"] == "zwave_js/hard_reset_controller")
        assert sent["entry_id"] == "e1"

    @pytest.mark.asyncio
    async def test_hard_reset_integration_absent_raises(self):
        # hard_reset wipes the controller, so an absent zwave_js config entry
        # must raise (integration_required) rather than no-op.
        client = _client({"config_entries/get": _entries(present=False)})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(radio="zwave", action="hard_reset", confirm=True)
        assert "zwave_js" in str(exc.value)

    # ---- unknown action ----------------------------------------------------- #
    @pytest.mark.asyncio
    async def test_unknown_action_lists_supported(self):
        with pytest.raises(ToolError) as exc:
            await _radio(_client({}))(radio="zwave", action="nope")
        assert "diagnostics" in str(exc.value)
