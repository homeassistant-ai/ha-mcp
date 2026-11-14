"""Unit tests for P2 entity bulk/resolver work (issue #1813 phase 0).

Covers:
- ha_get_entity bulk backend via native config/entity_registry/get_entries
- ha_remove_entity bulk removal envelope
- ha_get_entity unique_id -> entity_id resolver mode
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_entities import register_entity_tools

from ._tool_errors import error_body


def _make_client():
    client = MagicMock()
    client.send_websocket_message = AsyncMock()
    return client


def _register(client):
    """Register entity tools against a mock MCP, returning {name: method}."""
    mcp = MagicMock()
    registered: dict = {}

    def capture_add_tool(method):
        fmcp = getattr(method, "__fastmcp__", None)
        name = (fmcp.name if fmcp else None) or method.__name__
        registered[name] = method

    mcp.add_tool = capture_add_tool
    register_entity_tools(mcp, client)
    return registered


# --------------------------------------------------------------------------
# Item 1 — ha_get_entity bulk via config/entity_registry/get_entries
# --------------------------------------------------------------------------
class TestBulkGetViaGetEntries:
    def _entry(self, eid, **overrides):
        entry = {
            "entity_id": eid,
            "name": None,
            "original_name": "Orig",
            "icon": None,
            "area_id": None,
            "disabled_by": None,
            "hidden_by": None,
            "aliases": [],
            "labels": [],
            "categories": {},
            "device_class": None,
            "original_device_class": None,
            "options": {},
            "platform": "hue",
            "device_id": None,
            "config_entry_id": None,
            "unique_id": f"uid_{eid}",
        }
        entry.update(overrides)
        return entry

    @pytest.mark.asyncio
    async def test_bulk_get_uses_single_get_entries_call(self):
        """Two ids => ONE get_entries WS message (not one get per id)."""
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "light.a": self._entry("light.a"),
                    "switch.b": self._entry("switch.b", disabled_by="user"),
                },
            }
        )
        tool = _register(client)["ha_get_entity"]

        result = await tool(entity_id=["light.a", "switch.b"])

        assert result["success"] is True
        assert result["count"] == 2
        # Exactly one WS call, and it is the native bulk command.
        assert client.send_websocket_message.call_count == 1
        msg = client.send_websocket_message.call_args[0][0]
        assert msg["type"] == "config/entity_registry/get_entries"
        assert msg["entity_ids"] == ["light.a", "switch.b"]

    @pytest.mark.asyncio
    async def test_bulk_get_preserves_request_order(self):
        """Response order follows the requested id order, not the map order."""
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "light.b": self._entry("light.b"),
                    "light.a": self._entry("light.a"),
                },
            }
        )
        tool = _register(client)["ha_get_entity"]

        result = await tool(entity_id=["light.a", "light.b"])

        assert [e["entity_id"] for e in result["entity_entries"]] == [
            "light.a",
            "light.b",
        ]

    @pytest.mark.asyncio
    async def test_bulk_get_whole_call_failure_errors_every_id(self):
        """A chunk-level WS failure maps every id in the chunk to an error.

        Those ids carry Core's message, not "Entity not found", so no exposure
        lookup for unregistered entities runs; this mock would fail it too.
        """
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": {"message": "registry offline"}}
        )
        tool = _register(client)["ha_get_entity"]

        result = await tool(entity_id=["light.a", "light.b"])

        assert result["count"] == 0
        assert {e["entity_id"] for e in result["errors"]} == {"light.a", "light.b"}
        assert all("registry offline" in e["error"] for e in result["errors"])

    @pytest.mark.asyncio
    async def test_single_get_still_uses_registry_get(self):
        """Single-entity path is untouched: still config/entity_registry/get."""
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": self._entry("light.a")}
        )
        tool = _register(client)["ha_get_entity"]

        result = await tool(entity_id="light.a")

        assert result["success"] is True
        assert result["entity_id"] == "light.a"
        assert client.send_websocket_message.call_args[0][0]["type"] == (
            "config/entity_registry/get"
        )


# --------------------------------------------------------------------------
# Item 3 — bulk removal in ha_remove_entity
# --------------------------------------------------------------------------
class TestBulkRemove:
    @pytest.mark.asyncio
    async def test_bulk_mixed_outcome(self):
        """removed / skipped (not-found idempotent) / errors are classified."""
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            side_effect=[
                {"success": True, "result": None},  # sensor.a removed
                # sensor.b skipped on Core's not_found code
                {
                    "success": False,
                    "error": "Entity missing",
                    "error_code": "not_found",
                },
                {"success": False, "error": "Permission denied"},  # sensor.c error
            ]
        )
        tool = _register(client)["ha_remove_entity"]

        result = await tool(entity_id=["sensor.a", "sensor.b", "sensor.c"])

        assert result["success"] is False
        assert result["total"] == 3
        assert result["removed"] == ["sensor.a"]
        assert result["skipped"] == ["sensor.b"]
        assert result["errors"] == [
            {
                "entity_id": "sensor.c",
                "code": "SERVICE_CALL_FAILED",
                "message": "Permission denied",
            }
        ]
        # Sequential: exactly one remove WS call per id.
        assert client.send_websocket_message.call_count == 3
        for call in client.send_websocket_message.call_args_list:
            assert call[0][0]["type"] == "config/entity_registry/remove"

    @pytest.mark.asyncio
    async def test_bulk_all_removed_is_success(self):
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": None}
        )
        tool = _register(client)["ha_remove_entity"]

        result = await tool(entity_id=["sensor.a", "sensor.b"])

        assert result["success"] is True
        assert result["removed"] == ["sensor.a", "sensor.b"]
        assert result["skipped"] == []
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_single_string_removal_byte_identical(self):
        """A plain string still returns the original single-entity envelope."""
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": None}
        )
        tool = _register(client)["ha_remove_entity"]

        result = await tool(entity_id="sensor.a")

        assert result == {"success": True, "entity_id": "sensor.a"}
        assert client.send_websocket_message.call_args[0][0] == {
            "type": "config/entity_registry/remove",
            "entity_id": "sensor.a",
        }

    @pytest.mark.asyncio
    async def test_bulk_cap_enforced(self):
        client = _make_client()
        tool = _register(client)["ha_remove_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(entity_id=[f"sensor.e{i}" for i in range(101)])

        body = error_body(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "exceeds maximum" in body["error"]["message"]
        client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_bulk_empty_list_rejected(self):
        client = _make_client()
        tool = _register(client)["ha_remove_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(entity_id=[])

        body = error_body(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "empty" in body["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_bulk_whitespace_id_rejected(self):
        client = _make_client()
        tool = _register(client)["ha_remove_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(entity_id=["sensor.a", "  "])

        body = error_body(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"


# --------------------------------------------------------------------------
# Item 4 — unique_id -> entity_id resolver in ha_get_entity
# --------------------------------------------------------------------------
class TestUniqueIdResolver:
    @pytest.mark.asyncio
    async def test_mutual_exclusion_both_provided(self):
        client = _make_client()
        tool = _register(client)["ha_get_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(entity_id="sensor.temp", unique_id="abc123")

        body = error_body(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "exactly one" in body["error"]["message"].lower()
        client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_domain_filter_without_unique_id_rejected(self):
        client = _make_client()
        tool = _register(client)["ha_get_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(entity_id="sensor.temp", domain="sensor")

        body = error_body(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "resolver filter" in body["error"]["message"]
