"""Unit tests for P2 entity bulk/resolver work (issue #1813 phase 0).

Covers:
- ha_get_entity bulk backend via native config/entity_registry/get_entries
- ha_set_entity bulk expose batched into one homeassistant/expose_entity call
- ha_remove_entity bulk removal envelope
- ha_get_entity unique_id -> entity_id resolver mode
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_entities import register_entity_tools


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


def _error_body(exc_info) -> dict:
    return json.loads(str(exc_info.value))


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
        # Shape is projected through the same formatter as the single path.
        by_id = {e["entity_id"]: e for e in result["entity_entries"]}
        assert by_id["light.a"]["enabled"] is True
        assert by_id["light.a"]["platform"] == "hue"
        assert by_id["light.a"]["unique_id"] == "uid_light.a"
        assert by_id["switch.b"]["enabled"] is False
        assert by_id["switch.b"]["disabled_by"] == "user"

    @pytest.mark.asyncio
    async def test_bulk_get_missing_id_maps_to_error(self):
        """A null in the entries map reproduces the per-id not-found contract."""
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "light.a": self._entry("light.a"),
                    "light.missing": None,
                },
            }
        )
        tool = _register(client)["ha_get_entity"]

        result = await tool(entity_id=["light.a", "light.missing"])

        assert result["success"] is True
        assert result["count"] == 1
        assert result["entity_entries"][0]["entity_id"] == "light.a"
        assert result["errors"] == [
            {"entity_id": "light.missing", "error": "Entity not found"}
        ]
        assert "suggestions" in result

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
        """A chunk-level WS failure maps every id in the chunk to an error."""
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
# Item 2 — batched homeassistant/expose_entity in bulk ha_set_entity
# --------------------------------------------------------------------------
class TestBulkExposeBatching:
    @pytest.mark.asyncio
    async def test_mixed_true_false_sends_one_call_per_set(self):
        """expose_to with an on-set and off-set => two expose calls, each with
        the full id list, plus one get_entries refetch."""
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            side_effect=[
                {"success": True},  # expose True set for [a, b]
                {"success": True},  # expose False set for [a, b]
                {  # single get_entries refetch
                    "success": True,
                    "result": {
                        "light.a": {"entity_id": "light.a", "options": {}},
                        "light.b": {"entity_id": "light.b", "options": {}},
                    },
                },
            ]
        )
        tool = _register(client)["ha_set_entity"]

        result = await tool(
            entity_id=["light.a", "light.b"],
            expose_to={"conversation": True, "cloud.alexa": False},
        )

        assert result["success"] is True
        assert result["succeeded_count"] == 2
        calls = [c[0][0] for c in client.send_websocket_message.call_args_list]
        assert client.send_websocket_message.call_count == 3
        assert calls[0]["type"] == "homeassistant/expose_entity"
        assert calls[0]["assistants"] == ["conversation"]
        assert calls[0]["entity_ids"] == ["light.a", "light.b"]
        assert calls[0]["should_expose"] is True
        assert calls[1]["assistants"] == ["cloud.alexa"]
        assert calls[1]["entity_ids"] == ["light.a", "light.b"]
        assert calls[1]["should_expose"] is False
        assert calls[2]["type"] == "config/entity_registry/get_entries"

    @pytest.mark.asyncio
    async def test_batch_expose_failure_fails_all_ids(self):
        """A batch-level expose failure is reported against every id."""
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": {"message": "not supported"}}
        )
        tool = _register(client)["ha_set_entity"]

        result = await tool(
            entity_id=["light.a", "light.b"],
            expose_to={"conversation": True},
        )

        assert result["success"] is False
        assert result["failed_count"] == 2
        assert result["succeeded_count"] == 0
        assert {f["entity_id"] for f in result["failed"]} == {"light.a", "light.b"}
        assert all("not supported" in f["error"] for f in result["failed"])
        # One expose attempt; no refetch after a failure.
        assert client.send_websocket_message.call_count == 1

    @pytest.mark.asyncio
    async def test_labels_and_expose_registry_per_entity_expose_batched(self):
        """labels+expose: per-entity registry updates, then ONE batched expose,
        then one get_entries refetch. updates carry both label + expose info."""
        client = _make_client()

        def _reg_entry(eid):
            return {
                "success": True,
                "result": {
                    "entity_entry": {
                        "entity_id": eid,
                        "name": None,
                        "original_name": "O",
                        "icon": None,
                        "area_id": None,
                        "disabled_by": None,
                        "hidden_by": None,
                        "aliases": [],
                        "labels": ["outdoor"],
                        "options": {},
                    }
                },
            }

        client.send_websocket_message = AsyncMock(
            side_effect=[
                _reg_entry("light.a"),  # registry update for a
                _reg_entry("light.b"),  # registry update for b
                {"success": True},  # single batched expose for [a, b]
                {  # single get_entries refetch
                    "success": True,
                    "result": {
                        "light.a": {
                            "entity_id": "light.a",
                            "labels": ["outdoor"],
                            "options": {"conversation": {"should_expose": True}},
                        },
                        "light.b": {
                            "entity_id": "light.b",
                            "labels": ["outdoor"],
                            "options": {"conversation": {"should_expose": True}},
                        },
                    },
                },
            ]
        )
        tool = _register(client)["ha_set_entity"]

        result = await tool(
            entity_id=["light.a", "light.b"],
            labels=["outdoor"],
            expose_to={"conversation": True},
        )

        assert result["success"] is True
        assert result["succeeded_count"] == 2
        calls = [c[0][0] for c in client.send_websocket_message.call_args_list]
        # 2 registry updates + 1 expose + 1 refetch
        assert [c["type"] for c in calls] == [
            "config/entity_registry/update",
            "config/entity_registry/update",
            "homeassistant/expose_entity",
            "config/entity_registry/get_entries",
        ]
        assert calls[2]["entity_ids"] == ["light.a", "light.b"]
        entries = {e["entity_id"]: e for e in result["succeeded"]}
        updates_a = str(entries["light.a"]["updates"])
        assert "labels=['outdoor']" in updates_a
        assert "expose_to=" in updates_a
        # entity_entry reflects the post-exposure refetch.
        assert entries["light.a"]["entity_entry"]["options"] == {
            "conversation": {"should_expose": True}
        }


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
                {"success": False, "error": "Entity not found"},  # sensor.b skipped
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

        body = _error_body(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "exceeds maximum" in body["error"]["message"]
        client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_bulk_empty_list_rejected(self):
        client = _make_client()
        tool = _register(client)["ha_remove_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(entity_id=[])

        body = _error_body(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "empty" in body["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_bulk_whitespace_id_rejected(self):
        client = _make_client()
        tool = _register(client)["ha_remove_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(entity_id=["sensor.a", "  "])

        body = _error_body(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"


# --------------------------------------------------------------------------
# Item 4 — unique_id -> entity_id resolver in ha_get_entity
# --------------------------------------------------------------------------
class TestUniqueIdResolver:
    def _list_entry(self, eid, unique_id, platform):
        return {
            "entity_id": eid,
            "unique_id": unique_id,
            "platform": platform,
            "name": None,
            "original_name": "O",
            "icon": None,
            "area_id": None,
            "disabled_by": None,
            "hidden_by": None,
            "labels": [],
            "categories": {},
            "options": {},
            "device_id": None,
            "config_entry_id": "ce1",
        }

    @pytest.mark.asyncio
    async def test_resolve_single_match(self):
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    self._list_entry("sensor.temp", "abc123", "zwave_js"),
                    self._list_entry("sensor.other", "zzz", "hue"),
                ],
            }
        )
        tool = _register(client)["ha_get_entity"]

        result = await tool(unique_id="abc123")

        assert result["success"] is True
        assert result["matches"] == 1
        assert result["unique_id"] == "abc123"
        entry = result["entity_entries"][0]
        assert entry["entity_id"] == "sensor.temp"
        assert entry["unique_id"] == "abc123"
        assert entry["platform"] == "zwave_js"
        # Single list fetch only.
        assert client.send_websocket_message.call_count == 1
        assert client.send_websocket_message.call_args[0][0] == {
            "type": "config/entity_registry/list"
        }

    @pytest.mark.asyncio
    async def test_resolve_multi_platform_collision(self):
        """Same unique_id under two platforms => both returned."""
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    self._list_entry("sensor.a", "shared", "hue"),
                    self._list_entry("light.a", "shared", "zwave_js"),
                ],
            }
        )
        tool = _register(client)["ha_get_entity"]

        result = await tool(unique_id="shared")

        assert result["matches"] == 2
        assert {e["entity_id"] for e in result["entity_entries"]} == {
            "sensor.a",
            "light.a",
        }

    @pytest.mark.asyncio
    async def test_resolve_narrowing_by_platform(self):
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    self._list_entry("sensor.a", "shared", "hue"),
                    self._list_entry("light.a", "shared", "zwave_js"),
                ],
            }
        )
        tool = _register(client)["ha_get_entity"]

        result = await tool(unique_id="shared", platform="zwave_js")

        assert result["matches"] == 1
        assert result["platform"] == "zwave_js"
        assert result["entity_entries"][0]["entity_id"] == "light.a"

    @pytest.mark.asyncio
    async def test_resolve_narrowing_by_domain(self):
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    self._list_entry("sensor.a", "shared", "hue"),
                    self._list_entry("light.a", "shared", "zwave_js"),
                ],
            }
        )
        tool = _register(client)["ha_get_entity"]

        result = await tool(unique_id="shared", domain="sensor")

        assert result["matches"] == 1
        assert result["domain"] == "sensor"
        assert result["entity_entries"][0]["entity_id"] == "sensor.a"

    @pytest.mark.asyncio
    async def test_resolve_not_found_raises(self):
        client = _make_client()
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        tool = _register(client)["ha_get_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(unique_id="nope")

        body = _error_body(exc_info)
        assert body["error"]["code"] == "ENTITY_NOT_FOUND"
        assert "nope" in body["error"]["message"]

    @pytest.mark.asyncio
    async def test_mutual_exclusion_both_provided(self):
        client = _make_client()
        tool = _register(client)["ha_get_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(entity_id="sensor.temp", unique_id="abc123")

        body = _error_body(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "exactly one" in body["error"]["message"].lower()
        client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_neither_entity_id_nor_unique_id(self):
        client = _make_client()
        tool = _register(client)["ha_get_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool()

        body = _error_body(exc_info)
        assert body["error"]["code"] == "VALIDATION_MISSING_PARAMETER"
        client.send_websocket_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_domain_filter_without_unique_id_rejected(self):
        client = _make_client()
        tool = _register(client)["ha_get_entity"]

        with pytest.raises(ToolError) as exc_info:
            await tool(entity_id="sensor.temp", domain="sensor")

        body = _error_body(exc_info)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "resolver filter" in body["error"]["message"]
