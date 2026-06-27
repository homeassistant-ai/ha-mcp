"""Unit tests for the Thread/OTBR handler behind ``ha_manage_radio``.

Mirrors test_radio_management.py's dispatcher-level style: register the tool via
``register_radio_tools`` against a mock client that routes
``send_websocket_message`` by message ``type``, then assert success envelopes
and that the destructive / required-param gates raise ``ToolError``.
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


def _client(routes: dict, *, record: list | None = None):
    """Mock client whose send_websocket_message routes by message ``type``."""
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


def _radio(client):
    return _capture(register_radio_tools, client)["ha_manage_radio"]


# config_entries/get reply with a single OTBR entry (resolve_entry_id input).
_OTBR_ENTRY = {"success": True, "result": [{"domain": "otbr", "entry_id": "otbr1"}]}
_INFO = {
    "success": True,
    "result": {
        "f00dcafe": {
            "border_agent_id": "ba1",
            "channel": 15,
            "extended_pan_id": "1111222233334444",
            "active_dataset_tlvs": "0e08abcd",
        }
    },
}


class TestThreadHandler:
    @pytest.mark.asyncio
    async def test_network_status(self):
        client = _client({"config_entries/get": _OTBR_ENTRY, "otbr/info": _INFO})
        out = await _radio(client)(radio="thread", action="network_status")
        assert out["success"] is True
        assert out["radio"] == "thread"
        assert out["config_entry_id"] == "otbr1"
        assert out["border_routers"]["f00dcafe"]["channel"] == 15

    @pytest.mark.asyncio
    async def test_network_status_no_otbr_degrades(self):
        client = _client({"config_entries/get": {"success": True, "result": []}})
        out = await _radio(client)(radio="thread", action="network_status")
        assert out["success"] is True
        assert out["available"] is False
        assert out["warnings"]

    @pytest.mark.asyncio
    async def test_list_datasets(self):
        datasets = {"datasets": [{"dataset_id": "d1", "preferred": True}]}
        client = _client(
            {"thread/list_datasets": {"success": True, "result": datasets}}
        )
        out = await _radio(client)(radio="thread", action="list_datasets")
        assert out["datasets"] == datasets

    @pytest.mark.asyncio
    async def test_discover_routers_started(self):
        record: list = []
        client = _client(
            {"thread/discover_routers": {"success": True, "result": None}},
            record=record,
        )
        out = await _radio(client)(radio="thread", action="discover_routers")
        assert out["success"] is True
        assert out["long_running"] is True
        assert any(m["type"] == "thread/discover_routers" for m in record)

    @pytest.mark.asyncio
    async def test_add_dataset(self):
        record: list = []
        client = _client(
            {"thread/add_dataset_tlv": {"success": True, "result": None}},
            record=record,
        )
        out = await _radio(client)(
            radio="thread",
            action="add_dataset",
            params={"source": "Google", "tlv": "0e08abcd"},
        )
        assert out["success"] is True
        sent = [m for m in record if m["type"] == "thread/add_dataset_tlv"]
        assert sent and sent[0]["source"] == "Google"
        assert sent[0]["tlv"] == "0e08abcd"

    @pytest.mark.asyncio
    async def test_add_dataset_missing_params_raises(self):
        client = _client({})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(
                radio="thread", action="add_dataset", params={"source": "Google"}
            )
        assert "tlv" in str(exc.value)

    @pytest.mark.asyncio
    async def test_set_network_resolves_extended_address(self):
        record: list = []
        client = _client(
            {
                "otbr/info": _INFO,
                "otbr/set_network": {"success": True, "result": None},
            },
            record=record,
        )
        out = await _radio(client)(
            radio="thread",
            action="set_network",
            params={"dataset_id": "d1"},
            confirm=True,
        )
        assert out["extended_address"] == "f00dcafe"
        sent = [m for m in record if m["type"] == "otbr/set_network"]
        assert sent and sent[0]["extended_address"] == "f00dcafe"
        assert sent[0]["dataset_id"] == "d1"

    @pytest.mark.asyncio
    async def test_set_network_no_otbr_degrades(self):
        client = _client({"otbr/info": {"success": True, "result": {}}})
        out = await _radio(client)(
            radio="thread",
            action="set_network",
            params={"dataset_id": "d1"},
            confirm=True,
        )
        assert out["available"] is False

    @pytest.mark.asyncio
    async def test_set_network_requires_confirm(self):
        client = _client({"otbr/info": _INFO})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(
                radio="thread", action="set_network", params={"dataset_id": "d1"}
            )
        assert "confirm" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_create_network_requires_confirm(self):
        client = _client({})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(radio="thread", action="create_network")
        assert "confirm" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_create_network_confirmed_uses_explicit_address(self):
        record: list = []
        client = _client(
            {"otbr/create_network": {"success": True, "result": None}},
            record=record,
        )
        out = await _radio(client)(
            radio="thread",
            action="create_network",
            params={"extended_address": "abcd1234"},
            confirm=True,
        )
        assert out["extended_address"] == "abcd1234"
        sent = [m for m in record if m["type"] == "otbr/create_network"]
        assert sent and sent[0]["extended_address"] == "abcd1234"
        # Explicit address provided -> no otbr/info lookup needed.
        assert not any(m["type"] == "otbr/info" for m in record)

    @pytest.mark.asyncio
    async def test_set_channel_requires_confirm(self):
        client = _client({})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(
                radio="thread", action="set_channel", params={"channel": 20}
            )
        assert "confirm" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_set_channel_missing_channel_raises(self):
        client = _client({})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(radio="thread", action="set_channel", confirm=True)
        assert "channel" in str(exc.value)

    @pytest.mark.asyncio
    async def test_set_channel_confirmed(self):
        record: list = []
        client = _client(
            {
                "otbr/info": _INFO,
                "otbr/set_channel": {"success": True, "result": None},
            },
            record=record,
        )
        out = await _radio(client)(
            radio="thread",
            action="set_channel",
            params={"channel": 20},
            confirm=True,
        )
        assert out["success"] is True
        sent = [m for m in record if m["type"] == "otbr/set_channel"]
        assert sent and sent[0]["channel"] == 20

    @pytest.mark.asyncio
    async def test_unknown_action_lists_supported(self):
        client = _client({})
        with pytest.raises(ToolError) as exc:
            await _radio(client)(radio="thread", action="nope")
        assert "network_status" in str(exc.value)
