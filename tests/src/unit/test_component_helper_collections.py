"""ha_mcp_tools helper_schemas / helper_item / helper_write, and the server client."""

from __future__ import annotations

import asyncio
import functools
import importlib
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
)
from ha_mcp.tools import component_helper_collections as client_mod
from ha_mcp.tools.component_api import ComponentCaps

# The sibling module installs the homeassistant.* stubs the component needs, so
# the component module is imported only after it.
from .test_component_ws_search import (
    _REAL_VOL,
    FakeHass,
    _FakeConnection,
    _Unauthorized,
    functional_ws,  # noqa: F401 - pytest fixture
    wsapi,
)

hc = importlib.import_module("custom_components.ha_mcp_tools.helper_collections")


class ItemNotFound(Exception):
    pass


class FakeCollection:
    def __init__(self, items: dict[str, dict[str, Any]] | None = None) -> None:
        self.data = dict(items or {})
        self.invalid: Exception | None = None

    async def async_create_item(self, data: dict[str, Any]) -> dict[str, Any]:
        if self.invalid:
            raise self.invalid
        item = {"id": data["name"].lower(), **data}
        self.data[item["id"]] = item
        return item

    async def async_update_item(self, item_id: str, data: dict[str, Any]) -> dict:
        if item_id not in self.data:
            raise ItemNotFound(item_id)
        self.data[item_id] = {"id": item_id, **data}
        return self.data[item_id]


class FakeCollectionWs:
    def __init__(self, collection: FakeCollection) -> None:
        self.storage_collection = collection
        self.create_schema = {"name": str}
        self.update_schema = {"name": str}

    async def ws_create_item(self, hass, connection, msg):  # pragma: no cover
        raise AssertionError("never invoked")


def _wrapped(handler):
    """Core's require_admin(async_response(bound method)) wrapping."""

    @functools.wraps(handler)
    def async_response(*args):  # pragma: no cover
        return handler(*args)

    @functools.wraps(async_response)
    def require_admin(*args):  # pragma: no cover
        return async_response(*args)

    return require_admin


def _hass(collection: FakeCollection, helper_type: str = "input_number") -> FakeHass:
    owner = FakeCollectionWs(collection)
    handlers = {f"{helper_type}/create": (_wrapped(owner.ws_create_item), {})}
    return FakeHass(data={"websocket_api": handlers})


class FakeRegistry:
    def __init__(self) -> None:
        self.entries: dict[str, Any] = {}
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def add(self, entity_id: str, platform: str, unique_id: str) -> None:
        self.entries[entity_id] = SimpleNamespace(
            entity_id=entity_id, platform=platform, unique_id=unique_id,
            categories={"automation": "a1"},
        )  # fmt: skip

    def async_get(self, entity_id: str) -> Any:
        return self.entries.get(entity_id)

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str):
        return next(
            (e.entity_id for e in self.entries.values()
             if e.platform == platform and e.unique_id == unique_id),
            None,
        )  # fmt: skip

    def async_update_entity(self, entity_id: str, **changes: Any) -> None:
        self.updates.append((entity_id, changes))


@pytest.fixture(autouse=True)
def _item_not_found(monkeypatch):
    monkeypatch.setattr(hc, "_item_not_found", lambda: ItemNotFound)


def test_collection_owner_unwraps_core_handler() -> None:
    collection = FakeCollection()
    assert (
        hc.collection_owner(_hass(collection), "input_number").storage_collection
        is collection
    )
    assert hc.collection_owner(FakeHass(), "input_number") is None


def test_describe_schemas_marks_unresolvable_types(monkeypatch) -> None:
    monkeypatch.setattr(hc, "_serialize", lambda schema: [{"name": "name"}])
    result = hc.describe_schemas(_hass(FakeCollection()))
    assert result["types"] == {"input_number": {"create": [{"name": "name"}],
                                                "update": [{"name": "name"}]}}  # fmt: skip
    assert "input_number" not in result["unavailable"]
    assert set(result["unavailable"]) == set(hc.SIMPLE_HELPER_TYPES) - {"input_number"}


def test_serialize_falls_back_per_field(monkeypatch) -> None:
    vol = _REAL_VOL  # sibling modules stub voluptuous in sys.modules

    def convert(schema):
        (key,) = schema
        if str(key) == "icon":
            raise ValueError("unable to serialize schema: <function icon>")
        return [{"name": str(key), "type": "float"}]

    monkeypatch.setattr(hc, "_convert", convert)
    schema = {vol.Required("min"): float, vol.Optional("icon"): str}
    assert hc._serialize(schema) == [
        {"name": "min", "type": "float"},
        {"name": "icon", "required": False},
    ]


def test_read_item_by_entity_and_by_id() -> None:
    collection = FakeCollection({"t": {"id": "t", "name": "T", "min": 0}})
    registry = FakeRegistry()
    registry.add("input_number.t", "input_number", "t")
    hass = _hass(collection)
    by_entity = hc.read_item(hass, registry, "input_number", "input_number.t", None)
    assert by_entity == {"success": True, "item_id": "t", "entity_id": "input_number.t",
                         "item": {"id": "t", "name": "T", "min": 0}}  # fmt: skip
    assert hc.read_item(hass, registry, "input_number", None, "t")["entity_id"] == (
        "input_number.t"
    )
    registry.add("sensor.x", "template", "t")
    missing = hc.read_item(hass, registry, "input_number", "sensor.x", None)
    assert missing["error"]["code"] == "not_found"


def test_write_creates_then_applies_registry_fields() -> None:
    collection = FakeCollection()
    registry = FakeRegistry()
    registry.add("input_number.target", "input_number", "target")
    msg = {
        "helper_type": "input_number",
        "action": "create",
        "data": {"name": "Target", "min": 0, "max": 9},
        "registry": {
            "area_id": "kitchen",
            "labels": ["a"],
            "category": "c1",
            "icon": "",
        },
    }
    result = asyncio.run(hc.async_write_item(_hass(collection), registry, msg))
    assert result["success"] is True
    assert result["entity_id"] == "input_number.target"
    assert result["item"]["min"] == 0
    assert result["registry_applied"] == {
        "icon": None, "area_id": "kitchen", "labels": ["a"], "category": "c1",
    }  # fmt: skip
    assert registry.updates == [(
        "input_number.target",
        {"icon": None, "area_id": "kitchen", "labels": {"a"},
         "categories": {"automation": "a1", "helpers": "c1"}},
    )]  # fmt: skip


def test_invalid_errors_include_real_voluptuous(monkeypatch) -> None:
    # Sibling modules stub voluptuous in sys.modules; pin the real one here.
    monkeypatch.setitem(sys.modules, "voluptuous", _REAL_VOL)
    assert _REAL_VOL.Invalid in hc._invalid_errors()


def test_write_reports_invalid_missing_and_unavailable() -> None:
    collection = FakeCollection()
    collection.invalid = ValueError("min must be below max")
    hass = _hass(collection)
    base = {"helper_type": "input_number", "data": {"name": "X"}}
    invalid = asyncio.run(
        hc.async_write_item(hass, FakeRegistry(), {**base, "action": "create"})
    )
    assert invalid["error"] == {"code": "invalid", "message": "min must be below max"}
    missing = asyncio.run(
        hc.async_write_item(
            hass, FakeRegistry(), {**base, "action": "update", "item_id": "nope"}
        )
    )
    assert missing["error"]["code"] == "not_found"
    unavailable = asyncio.run(
        hc.async_write_item(FakeHass(), FakeRegistry(), {**base, "action": "create"})
    )
    assert unavailable["error"]["code"] == "unavailable"


def test_commands_registered_and_advertised(functional_ws) -> None:  # noqa: F811
    commands = {hc.WS_HELPER_SCHEMAS, hc.WS_HELPER_ITEM, hc.WS_HELPER_WRITE}
    assert commands <= set(functional_ws.registered)
    assert {"helper_schemas", "helper_item", "helper_write"} <= set(wsapi.CAPABILITIES)


@pytest.mark.parametrize(
    ("command", "extra"),
    [
        (hc.WS_HELPER_SCHEMAS, {}),
        (hc.WS_HELPER_ITEM, {"helper_type": "zone", "item_id": "home"}),
        (
            hc.WS_HELPER_WRITE,
            {"helper_type": "zone", "action": "create", "data": {}},
        ),
    ],
)
def test_commands_are_admin_gated(functional_ws, command, extra) -> None:  # noqa: F811
    handler = functional_ws.registered[command]
    msg = {"id": 1, "type": command, **extra}
    with pytest.raises(_Unauthorized):
        handler(FakeHass(), _FakeConnection(is_admin=False), msg)
    connection = _FakeConnection()
    handler(FakeHass(), connection, msg)
    assert 1 in connection.results


# --- server client --------------------------------------------------------------


@pytest.fixture
def ws(monkeypatch):
    sock = MagicMock()
    sock.send_command = AsyncMock()
    caps = ComponentCaps(1, "2.2.2", frozenset(client_mod.HELPER_CAPABILITIES), {})
    monkeypatch.setattr(client_mod, "get_component_caps", AsyncMock(return_value=caps))
    monkeypatch.setattr(
        client_mod, "get_websocket_client", AsyncMock(return_value=sock)
    )
    invalidate = MagicMock()
    monkeypatch.setattr(client_mod, "invalidate_caps", invalidate)
    sock.invalidate = invalidate
    return sock


class _Client:
    base_url = "http://ha"
    token = "t"


def _client() -> Any:
    return _Client()


async def test_write_returns_result_and_falls_back_only_when_nothing_ran(ws) -> None:
    ws.send_command.return_value = {
        "success": True,
        "result": {"success": True, "item": {}},
    }
    assert await client_mod.write_helper_item(_client(), "input_boolean", "create", {}) == {
        "success": True, "item": {},
    }  # fmt: skip

    ws.send_command.side_effect = HomeAssistantCommandNotSent("down")
    assert (
        await client_mod.write_helper_item(_client(), "input_boolean", "create", {})
        is None
    )

    ws.send_command.side_effect = HomeAssistantCommandError("x", code="unknown_command")
    assert (
        await client_mod.write_helper_item(_client(), "input_boolean", "create", {})
        is None
    )
    ws.invalidate.assert_called_once()

    ws.send_command.side_effect = None
    ws.send_command.return_value = {"success": True, "result": {
        "success": False, "error": {"code": "unavailable", "message": "none"}}}  # fmt: skip
    assert (
        await client_mod.write_helper_item(_client(), "input_boolean", "create", {})
        is None
    )


async def test_write_after_dispatch_failure_is_outcome_unknown(ws) -> None:
    ws.send_command.side_effect = TimeoutError("lost")
    with pytest.raises(ToolError, match="outcome is unknown"):
        await client_mod.write_helper_item(_client(), "input_boolean", "create", {})


async def test_write_cancellation_propagates(ws) -> None:
    ws.send_command.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await client_mod.write_helper_item(_client(), "input_boolean", "create", {})


async def test_write_invalid_carries_error_context(ws) -> None:
    ws.send_command.return_value = {"success": True, "result": {
        "success": False, "error": {"code": "invalid", "message": "bad min"}}}  # fmt: skip
    with pytest.raises(ToolError, match="bad min") as err:
        await client_mod.write_helper_item(
            _client(),
            "input_number",
            "create",
            {},
            error_context={"data_schema": ["x"]},
        )
    assert "data_schema" in str(err.value)


async def test_schemas_are_cached(ws) -> None:
    client = _client()
    ws.send_command.return_value = {"success": True, "result": {"types": {"a": {}}}}
    assert await client_mod.fetch_helper_schemas(client) == {"a": {}}
    assert await client_mod.fetch_helper_schemas(client) == {"a": {}}
    ws.send_command.assert_awaited_once()
