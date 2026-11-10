"""Issue #2611: the entity dialog's "Main entity of the device" switch.

The dialog ("Use device name" in 2026.10) writes ``name`` ``""`` when the entity
has an integration name and ``null`` otherwise, and moves an entity with an area
of its own to its device's area. ``ha_set_entity`` maps ``name=""`` to ``null``
(revert to default), so ``use_device_name`` carries the switch.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.entity_update_fields import build_name_visibility_fields
from ha_mcp.tools.tools_entities import register_entity_tools
from ha_mcp.utils.registry_update_lock import registry_update_lock

ENTRY = {
    "entity_id": "sensor.sun_next_dawn",
    "name": None,
    "original_name": "Next dawn",
    "device_id": "dev1",
    "area_id": None,
}


def _ws_ok() -> dict:
    return {"success": True, "result": {"entity_entry": dict(ENTRY)}}


def _ws_get(entry: dict | None = None) -> dict:
    return {"success": True, "result": dict(ENTRY if entry is None else entry)}


class TestBuildNameField:
    @pytest.mark.parametrize(
        ("entry", "expected", "notes"),
        [
            (ENTRY, {"name": ""}, ["main entity of the device"]),
            # Like the entity dialog: "" only overrides an integration name.
            (
                dict(ENTRY, original_name=None, name="Dawn"),
                {"name": None},
                ["main entity of the device"],
            ),
            # It gives up the entity's own area for the device's.
            (
                dict(ENTRY, area_id="kitchen"),
                {"name": "", "area_id": None},
                [
                    "main entity of the device",
                    "area cleared: follows the device's area",
                ],
            ),
            # No name of its own: already the main entity, the unchanged name
            # goes back so Core returns the entry, and the area stays.
            (
                dict(ENTRY, name="", area_id="kitchen"),
                {"name": ""},
                ["already the main entity"],
            ),
            (
                dict(ENTRY, original_name=None),
                {"name": None},
                ["already the main entity"],
            ),
        ],
    )
    def test_true_writes_what_the_dialog_writes(
        self, entry: dict, expected: dict, notes: list[str]
    ) -> None:
        message: dict = {}
        updates: list[str] = []
        build_name_visibility_fields(
            message, updates, None, None, None, None, True, entry
        )
        assert message == expected
        assert updates == notes

    def test_explicit_area_is_left_for_core_to_judge(self) -> None:
        """Core refuses an area of its own on a main entity; it is sent, not dropped."""
        message: dict = {}
        build_name_visibility_fields(
            message,
            [],
            "office",
            None,
            None,
            None,
            True,
            dict(ENTRY, area_id="kitchen"),
        )
        assert message == {"area_id": "office", "name": ""}

    def test_false_sends_null_name(self) -> None:
        message: dict = {}
        build_name_visibility_fields(
            message, [], None, None, None, None, use_device_name=False
        )
        assert message["name"] is None

    def test_omitted_leaves_name_out(self) -> None:
        message: dict = {}
        build_name_visibility_fields(message, [], None, None, None, None)
        assert "name" not in message

    def test_empty_string_name_still_clears(self) -> None:
        """Existing callers' ``name=''`` keeps meaning revert-to-default."""
        message: dict = {}
        build_name_visibility_fields(message, [], None, "", None, None)
        assert message["name"] is None


class TestSetEntityUseDeviceName:
    @pytest.fixture
    def tools(self):
        registered: dict = {}
        mcp = MagicMock()

        def capture(method):
            fmcp = getattr(method, "__fastmcp__", None)
            registered[(fmcp.name if fmcp else None) or method.__name__] = method

        mcp.add_tool = capture
        client = MagicMock()
        # use_device_name=True reads the entry (device check) before it writes.
        client.send_websocket_message = AsyncMock(side_effect=[_ws_get(), _ws_ok()])
        client.get_config = AsyncMock(return_value={"version": "2026.10.0"})
        register_entity_tools(mcp, client)
        return registered, client

    async def test_true_writes_empty_name_on_2026_10(self, tools) -> None:
        registered, client = tools
        result = await registered["ha_set_entity"](
            entity_id="sensor.sun_next_dawn", use_device_name=True
        )
        sent = client.send_websocket_message.call_args.args[0]
        assert sent["type"] == "config/entity_registry/update"
        assert sent["name"] == ""
        assert result["success"] is True

    async def test_an_entity_outside_the_registry_is_not_found(self, tools) -> None:
        registered, client = tools
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": "Entity not found",
                "error_code": "not_found",
            }
        )
        with pytest.raises(ToolError) as exc_info:
            await registered["ha_set_entity"](
                entity_id="sensor.sun_next_dawn", use_device_name=True
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "ENTITY_NOT_FOUND"
        assert client.send_websocket_message.await_count == 1

    async def test_entry_is_read_under_the_update_lock(self, tools) -> None:
        """The write is built from the entry, so no update may land between."""
        registered, client = tools
        lock = registry_update_lock("entity", "sensor.sun_next_dawn")
        held: list[bool] = []

        async def ws(message: dict, **_: object) -> dict:
            if message["type"] == "config/entity_registry/get":
                held.append(lock.locked())
                return _ws_get()
            return _ws_ok()

        client.send_websocket_message = AsyncMock(side_effect=ws)
        await registered["ha_set_entity"](
            entity_id="sensor.sun_next_dawn", use_device_name=True
        )
        assert held == [True]

    async def test_false_writes_null_name(self, tools) -> None:
        registered, client = tools
        client.send_websocket_message = AsyncMock(return_value=_ws_ok())
        await registered["ha_set_entity"](
            entity_id="sensor.sun_next_dawn", use_device_name=False
        )
        sent = client.send_websocket_message.call_args.args[0]
        assert sent["name"] is None

    @pytest.mark.parametrize("version", ["2026.9.4", "2025.12.1"])
    async def test_refused_before_2026_10(self, tools, version: str) -> None:
        """Older Cores store ``""`` but render it as the default name, so the
        switch would silently do the opposite of what was asked."""
        registered, client = tools
        client.get_config = AsyncMock(return_value={"version": version})
        with pytest.raises(ToolError) as exc_info:
            await registered["ha_set_entity"](
                entity_id="sensor.sun_next_dawn", use_device_name=True
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "2026.10" in error["message"]
        client.send_websocket_message.assert_not_called()

    async def test_unparseable_version_named_as_such(self, tools) -> None:
        """A Core version string that cannot be parsed is refused, and the
        error says so rather than claiming the Core is too old."""
        registered, client = tools
        client.get_config = AsyncMock(return_value={"version": "custom-build"})
        with pytest.raises(ToolError) as exc_info:
            await registered["ha_set_entity"](
                entity_id="sensor.sun_next_dawn", use_device_name=True
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "could not be parsed" in error["message"]
        assert "custom-build" in error["message"]
        client.send_websocket_message.assert_not_called()

    async def test_true_refused_without_device(self, tools) -> None:
        """Core would render an empty name: nothing to follow."""
        registered, client = tools
        client.send_websocket_message = AsyncMock(
            return_value=_ws_get(dict(ENTRY, device_id=None))
        )
        with pytest.raises(ToolError) as exc_info:
            await registered["ha_set_entity"](
                entity_id="sensor.sun_next_dawn", use_device_name=True
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "device" in error["message"]
        assert all(
            c.args[0]["type"] != "config/entity_registry/update"
            for c in client.send_websocket_message.call_args_list
        )

    async def test_false_is_not_version_gated(self, tools) -> None:
        registered, client = tools
        client.send_websocket_message = AsyncMock(return_value=_ws_ok())
        client.get_config = AsyncMock(return_value={"version": "2026.9.4"})
        await registered["ha_set_entity"](
            entity_id="sensor.sun_next_dawn", use_device_name=False
        )
        assert client.send_websocket_message.call_args.args[0]["name"] is None

    async def test_rejected_together_with_name(self, tools) -> None:
        registered, client = tools
        with pytest.raises(ToolError) as exc_info:
            await registered["ha_set_entity"](
                entity_id="sensor.sun_next_dawn", name="Dawn", use_device_name=True
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "use_device_name" in error["message"]
        client.send_websocket_message.assert_not_called()

    async def test_rejected_in_bulk(self, tools) -> None:
        registered, client = tools
        with pytest.raises(ToolError) as exc_info:
            await registered["ha_set_entity"](
                entity_id=["sensor.a", "sensor.b"], use_device_name=True
            )
        error = json.loads(str(exc_info.value))["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        client.send_websocket_message.assert_not_called()


class TestGetEntityServesCoreEntry:
    @pytest.fixture
    def tools(self):
        registered: dict = {}
        mcp = MagicMock()

        def capture(method):
            fmcp = getattr(method, "__fastmcp__", None)
            registered[(fmcp.name if fmcp else None) or method.__name__] = method

        mcp.add_tool = capture
        client = MagicMock()
        register_entity_tools(mcp, client)
        return registered, client

    async def test_entry_is_served_as_core_returns_it(self, tools) -> None:
        registered, client = tools
        entry = dict(ENTRY, name="", device_id="dev1")
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": entry}
        )
        result = await registered["ha_get_entity"](entity_id="sensor.sun_next_dawn")
        assert result["entity_entry"] == entry
