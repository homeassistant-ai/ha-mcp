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

from ha_mcp.tools.tools_radio import HANDLERS, register_radio_tools
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
    # Upstream NodeDiagnostics misspells this field (single 'd'); the mock must
    # match reality so the enricher's corrected lookup is actually exercised.
    "ip_adresses": ["fe80::1"],
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
        # Regression: IPs come through despite the upstream 'ip_adresses' typo.
        assert dev["node_diagnostics"]["ip_addresses"] == ["fe80::1"]

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
        client = _client(
            {"matter/node_diagnostics": {"success": True, "result": _DIAG}}
        )
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        out = await radio(radio="matter", action="diagnostics", device_id="m1")
        assert out["success"] is True
        assert out["radio"] == "matter"
        assert out["diagnostics"]["network_type"] == "THREAD"
        # Normalized from the upstream "ip_adresses" typo (matches ha_get_device).
        assert out["diagnostics"]["ip_addresses"] == ["fe80::1"]
        assert "ip_adresses" not in out["diagnostics"]

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
    async def test_remove_fabric_own_or_missing_index_errors(self):
        # _DIAG.active_fabric_index == 2; removing HA's own fabric (or omitting
        # fabric_index) must be refused with guidance toward ha_remove_device.
        client = _client(
            {"matter/node_diagnostics": {"success": True, "result": _DIAG}}
        )
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        with pytest.raises(ToolError) as exc:
            await radio(
                radio="matter", action="remove_fabric", device_id="m1", confirm=True
            )
        assert "ha_remove_device" in str(exc.value)

    @pytest.mark.asyncio
    async def test_remove_fabric_other_index_removed(self):
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
            radio="matter",
            action="remove_fabric",
            device_id="m1",
            params={"fabric_index": 1},  # a DIFFERENT controller's fabric
            confirm=True,
        )
        assert out["fabric_index"] == 1
        removed = [m for m in record if m["type"] == "matter/remove_matter_fabric"]
        assert removed and removed[0]["fabric_index"] == 1

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
                "config/entity_registry/get": {
                    "success": True,
                    "result": {"entity_id": "light.m", "device_id": "m1"},
                },
                "matter/node_diagnostics": {"success": True, "result": _DIAG},
            },
            record=record,
        )
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        out = await radio(radio="matter", action="diagnostics", entity_id="light.m")
        assert out["diagnostics"]["network_type"] == "THREAD"
        # Resolution uses the targeted single-entity get, not a full-list pull.
        reg = [m for m in record if m["type"] == "config/entity_registry/get"]
        assert reg and reg[0]["entity_id"] == "light.m"
        assert not any(m["type"] == "config/entity_registry/list" for m in record)
        diag = [m for m in record if m["type"] == "matter/node_diagnostics"]
        assert diag and diag[0]["device_id"] == "m1"

    @pytest.mark.asyncio
    async def test_firmware_update_installs_update_entity(self):
        svc: list = []
        client = _client(
            {
                "config/entity_registry/list": {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "update.bulb_fw",
                            "device_id": "m1",
                            "platform": "matter",
                        }
                    ],
                }
            }
        )

        async def fake_call_service(domain, service, data=None, return_response=False):
            svc.append((domain, service, data))
            return {"success": True}

        client.call_service = AsyncMock(side_effect=fake_call_service)
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        out = await radio(radio="matter", action="firmware_update", device_id="m1")

        assert out["entity_id"] == "update.bulb_fw"
        assert svc == [("update", "install", {"entity_id": "update.bulb_fw"})]

    # ---- commission --------------------------------------------------------- #
    @pytest.mark.asyncio
    async def test_commission_forwards_code_and_network_only(self):
        record: list = []
        client = _client(
            {"matter/commission": {"success": True, "result": {"node_id": 5}}},
            record=record,
        )
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        out = await radio(
            radio="matter", action="commission", params={"code": "MT:CODE"}
        )
        assert out["action"] == "commission"
        assert out["long_running"] is True
        assert out["result"] == {"node_id": 5}
        sent = next(m for m in record if m["type"] == "matter/commission")
        assert sent["code"] == "MT:CODE"
        # network_only defaults to False — and a literal False is forwarded
        # (ws_call only drops None fields).
        assert sent["network_only"] is False

    @pytest.mark.asyncio
    async def test_commission_on_network_forwards_pin(self):
        record: list = []
        client = _client(
            {"matter/commission_on_network": {"success": True, "result": {}}},
            record=record,
        )
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        out = await radio(
            radio="matter",
            action="commission_on_network",
            params={"pin": "12345678"},
        )
        assert out["action"] == "commission_on_network"
        assert out["long_running"] is True
        sent = next(m for m in record if m["type"] == "matter/commission_on_network")
        assert sent["pin"] == "12345678"
        assert "ip_addr" not in sent  # None field dropped by ws_call

    @pytest.mark.asyncio
    async def test_commission_on_network_requires_pin(self):
        radio = _capture(register_radio_tools, _client({}))["ha_manage_radio"]
        with pytest.raises(ToolError) as exc:
            await radio(radio="matter", action="commission_on_network")
        assert "pin" in str(exc.value)

    # ---- interview ---------------------------------------------------------- #
    @pytest.mark.asyncio
    async def test_interview_forwards_device_id(self):
        record: list = []
        client = _client(
            {"matter/interview_node": {"success": True, "result": {}}}, record=record
        )
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        out = await radio(radio="matter", action="interview", device_id="m1")
        assert out["action"] == "interview"
        assert out["long_running"] is True
        sent = next(m for m in record if m["type"] == "matter/interview_node")
        assert sent["device_id"] == "m1"

    # ---- set_thread --------------------------------------------------------- #
    @pytest.mark.asyncio
    async def test_set_thread_forwards_dataset(self):
        record: list = []
        client = _client(
            {"matter/set_thread": {"success": True, "result": {}}}, record=record
        )
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        out = await radio(
            radio="matter",
            action="set_thread",
            params={"thread_operation_dataset": "0e080000"},
        )
        assert out["action"] == "set_thread"
        sent = next(m for m in record if m["type"] == "matter/set_thread")
        assert sent["thread_operation_dataset"] == "0e080000"

    @pytest.mark.asyncio
    async def test_set_thread_requires_dataset(self):
        radio = _capture(register_radio_tools, _client({}))["ha_manage_radio"]
        with pytest.raises(ToolError) as exc:
            await radio(radio="matter", action="set_thread")
        assert "thread_operation_dataset" in str(exc.value)

    # ---- set_wifi_credentials ----------------------------------------------- #
    @pytest.mark.asyncio
    async def test_set_wifi_credentials_forwards_both(self):
        record: list = []
        client = _client(
            {"matter/set_wifi_credentials": {"success": True, "result": {}}},
            record=record,
        )
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        out = await radio(
            radio="matter",
            action="set_wifi_credentials",
            params={"network_name": "Home", "password": "secret"},
        )
        assert out["action"] == "set_wifi_credentials"
        sent = next(m for m in record if m["type"] == "matter/set_wifi_credentials")
        assert sent["network_name"] == "Home"
        assert sent["password"] == "secret"

    @pytest.mark.asyncio
    async def test_set_wifi_credentials_requires_name_and_password(self):
        radio = _capture(register_radio_tools, _client({}))["ha_manage_radio"]
        with pytest.raises(ToolError) as exc:
            await radio(radio="matter", action="set_wifi_credentials")
        msg = str(exc.value)
        assert "network_name" in msg
        assert "password" in msg

    # ---- network_status ----------------------------------------------------- #
    @pytest.mark.asyncio
    async def test_network_status_loaded_returns_note(self):
        client = _client(
            {
                "config_entries/get": {
                    "success": True,
                    "result": [{"domain": "matter", "entry_id": "e1"}],
                }
            }
        )
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        out = await radio(radio="matter", action="network_status")
        assert out["success"] is True
        assert out["config_entry_id"] == "e1"
        assert "note" in out

    @pytest.mark.asyncio
    async def test_network_status_integration_not_found(self):
        client = _client({"config_entries/get": {"success": True, "result": []}})
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        out = await radio(radio="matter", action="network_status")
        assert out["available"] is False
        assert out["warnings"]

    # ---- firmware_update no entity ------------------------------------------ #
    @pytest.mark.asyncio
    async def test_firmware_update_no_update_entity(self):
        client = _client(
            {
                "config/entity_registry/list": {
                    "success": True,
                    "result": [{"entity_id": "light.m1", "device_id": "m1"}],
                }
            }
        )
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        with pytest.raises(ToolError) as exc:
            await radio(radio="matter", action="firmware_update", device_id="m1")
        assert "update entity" in str(exc.value).lower()


# --------------------------------------------------------------------------- #
# Dispatcher contract: ws_call failure surfacing, SUPPORTED<->handle drift,
# and entity resolution.
# --------------------------------------------------------------------------- #


# Superset of every required-param name across all radio handlers, so require()
# passes for every action and handle() is actually exercised (the drift check
# relies on reaching the handler body, not the require gate). Extra keys are
# ignored by each handler's args.get() lookups.
_CONTRACT_PARAMS: dict = {
    # matter
    "code": "MT:CODE",
    "pin": "12345678",
    "thread_operation_dataset": "0e080000",
    "network_name": "Net",
    "password": "secret",
    # zigbee
    "group_name": "G",
    "group_ids": [1],
    "group_id": 1,
    "members": [{"ieee": "aa:bb", "endpoint_id": 1}],
    "source_ieee": "aa:bb",
    "target_ieee": "cc:dd",
    "endpoint_id": 1,
    "cluster_id": 6,
    "attribute": "on_off",
    "value": 1,
    "command": 0,
    "command_type": "server",
    "backup": {"backup": "payload"},
    "new_channel": 20,
    # thread
    "dataset_id": "d1",
    "channel": 20,
    "source": "Google",
    "tlv": "0e080000",
    # zwave
    "property": 5,
}


def _permissive_client():
    """Client that succeeds for any WS type and any service call.

    ``otbr/info`` returns a non-empty border-router map so the Thread per-OTBR
    actions resolve an extended_address and reach their dedicated branches
    instead of short-circuiting on integration_not_found.
    """
    mock_client = MagicMock()

    async def mock_ws(msg, **kwargs):
        msg_type = msg.get("type", "") if isinstance(msg, dict) else ""
        if msg_type == "otbr/info":
            return {"success": True, "result": {"otbr-1": {"channel": 15}}}
        return {"success": True, "result": {}}

    mock_client.send_websocket_message = AsyncMock(side_effect=mock_ws)
    mock_client.call_service = AsyncMock(return_value={})
    return mock_client


class TestRadioDispatcherContract:
    @pytest.mark.asyncio
    async def test_ws_call_failure_surfaces_tool_error(self):
        # A primary WS command reporting success=False must surface as a
        # ToolError (SERVICE_CALL_FAILED), carrying HA's error text.
        client = _client(
            {"matter/node_diagnostics": {"success": False, "error": "boom"}}
        )
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        with pytest.raises(ToolError) as exc:
            await radio(radio="matter", action="diagnostics", device_id="m1")
        assert "boom" in str(exc.value)

    @pytest.mark.asyncio
    async def test_every_supported_action_is_handled(self):
        # SUPPORTED<->handle drift guard: every action in every handler's
        # SUPPORTED must reach a real branch in handle(), never the terminal
        # "unhandled <radio> action" AssertionError (the dispatcher re-wraps
        # that AssertionError as an INTERNAL_ERROR ToolError carrying the text).
        for radio_name, handler in HANDLERS.items():
            for action in handler.SUPPORTED:
                client = _permissive_client()
                tool = _capture(register_radio_tools, client)["ha_manage_radio"]
                try:
                    await tool(
                        radio=radio_name,
                        action=action,
                        device_id="d1",
                        params=dict(_CONTRACT_PARAMS),
                        confirm=True,
                    )
                except ToolError as exc:
                    msg = str(exc)
                    assert "unhandled" not in msg, (
                        f"{radio_name}/{action} fell through handle(): {msg}"
                    )
                    # A "requires:" error means _CONTRACT_PARAMS lacks a
                    # newly-added required arg, so handle() was never reached
                    # and the drift check above is vacuous for this action.
                    assert "requires:" not in msg, (
                        f"{radio_name}/{action} needs a required arg absent from "
                        f"_CONTRACT_PARAMS; add it: {msg}"
                    )

    @pytest.mark.asyncio
    async def test_resolve_entity_device_not_found(self):
        # An entity_id the registry doesn't contain -> ENTITY_NOT_FOUND. The
        # targeted get returns success=False for an unknown entity_id.
        record: list = []
        client = _client(
            {"config/entity_registry/get": {"success": False, "error": "not_found"}},
            record=record,
        )
        radio = _capture(register_radio_tools, client)["ha_manage_radio"]
        with pytest.raises(ToolError) as exc:
            await radio(radio="matter", action="diagnostics", entity_id="light.ghost")
        msg = str(exc.value)
        assert "light.ghost" in msg
        assert "not found" in msg.lower()
        # Uses the targeted get keyed by the requested entity_id.
        reg = [m for m in record if m["type"] == "config/entity_registry/get"]
        assert reg and reg[0]["entity_id"] == "light.ghost"


@pytest.mark.asyncio
async def test_resolve_entity_device_connection_failure_not_entity_not_found():
    """A transport-shaped registry-get failure must surface as a connection
    error, never ENTITY_NOT_FOUND (issue #1832 review)."""
    client = _client(
        {
            "config/entity_registry/get": {
                "success": False,
                "error": "Failed to connect to Home Assistant WebSocket",
            }
        },
    )
    radio = _capture(register_radio_tools, client)["ha_manage_radio"]
    with pytest.raises(ToolError) as exc:
        await radio(radio="matter", action="diagnostics", entity_id="light.real")
    msg = str(exc.value)
    assert "CONNECTION_FAILED" in msg
    assert "ENTITY_NOT_FOUND" not in msg
