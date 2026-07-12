"""Routing tests for ``ha_config_list_helpers`` over the ``ha_mcp_tools`` gate.

When the component advertises the ``helpers_list`` capability,
``ha_config_list_helpers`` serves the whole listing from one
``ha_mcp_tools/helpers_list`` WebSocket call and skips the legacy
``{helper_type}/list`` fetch entirely. The component-served records ship the
issue #1794 stale-id fix additively: each keeps the legacy storage ``id`` and
gains the current ``entity_id`` + display ``name`` from the entity registry.

These tests pin that fast path, the record shape (storage id preserved, current
entity_id/name added), envelope parity with the legacy path, and the
error-taxonomy fallbacks (silent on ``unknown_command``; legacy + ``warnings[]``
on any other command error; legacy pin when the component has no WS surface).

Flow-based helper types (``template``, ``group``, ...) have no legacy
``{type}/list`` command, so they are served exclusively through the component:
the tests pin the flow happy path (``entry_id`` + current name + ``options``
records) and that any missing/failed component path raises a hard
``COMPONENT_NOT_INSTALLED`` error — never a legacy fallback, never a silent
empty list.

The component reports ``covered_types`` (the types a response authoritatively
enumerated). A requested type outside that list (e.g. ``tag``, which has no
state entity for the component's scan) — or a response with no ``covered_types``
at all (older component) — is treated as a component miss: storage types fall
back to the legacy ``{type}/list`` path, flow types raise.

The WS client is an ``AsyncMock`` whose ``send_command`` dispatches on the
command type. The HA client is a spy that tallies the legacy ``{type}/list``
fetch so a test can assert it never ran on the component path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ha_mcp.tools import component_api, tools_config_helpers
from ha_mcp.tools.tools_config_helpers import (
    _shape_collection_helper_record,
    register_config_helper_tools,
)

from ._component_routing_helpers import make_ws, patch_ws

# One collection helper renamed after creation: storage name "Old Name",
# current registry display name "New Name", entity_id input_boolean.foo — the
# exact #1794 shape (legacy list would emit only the storage id + stale name).
_LEGACY_ITEMS = [{"id": "abc123", "name": "Old Name", "icon": "mdi:flash"}]

_CAPS_HELPERS = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["helpers_list"],
    "limits": {},
}


def _component_helpers_result() -> dict[str, Any]:
    return {
        "helpers": [
            {
                "helper_type": "input_boolean",
                "object_id": "abc123",
                "entity_id": "input_boolean.foo",
                "name": "New Name",
                "kind": "collection",
                "config": {"id": "abc123", "name": "Old Name", "icon": "mdi:flash"},
            }
        ],
        "count": 1,
        "covered_types": ["input_boolean"],
    }


def _component_flow_result() -> dict[str, Any]:
    return {
        "helpers": [
            {
                "helper_type": "template",
                "entry_id": "cfgentry123",
                "entity_id": "sensor.templated",
                "name": "Templated Sensor",
                "kind": "flow",
                "options": {"state": "{{ 1 + 1 }}"},
            }
        ],
        "count": 1,
        "covered_types": ["template"],
    }


class RoutingClient:
    """Credentialed HA client spy: tallies every legacy ``{type}/list`` fetch."""

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.list_calls = 0

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        self.list_calls += 1
        msg_type = msg.get("type", "")
        if msg_type == "input_boolean/list":
            return {"success": True, "result": [dict(i) for i in _LEGACY_ITEMS]}
        if msg_type == "tag/list":
            return {"success": True, "result": [{"id": "tag-42", "name": "Front Door"}]}
        return {"success": False, "error": "unexpected list type"}


def _build_list_helpers(client: Any) -> Any:
    registered: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        name = (
            method.__fastmcp__.name
            if hasattr(method, "__fastmcp__")
            else method.__name__
        )
        registered[name] = method

    mcp = MagicMock()
    mcp.add_tool = capture_add_tool
    register_config_helper_tools(mcp, client)
    return registered["ha_config_list_helpers"]


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    """Isolate the module-global caps cache between tests."""
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


def _helpers_calls(ws: AsyncMock) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/helpers_list"
    ]


def _info_calls(ws: AsyncMock) -> list[Any]:
    return [
        c for c in ws.send_command.call_args_list if c.args[0] == "ha_mcp_tools/info"
    ]


@pytest.mark.asyncio
async def test_component_fast_path_skips_legacy_and_probes_caps_once() -> None:
    """Component serves the listing: no legacy fetch, and info probed once."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_result=_CAPS_HELPERS,
        cmd_result=_component_helpers_result(),
    )
    client = RoutingClient()
    list_helpers = _build_list_helpers(client)

    with patch_ws(ws, tools_config_helpers):
        resp = await list_helpers(helper_type="input_boolean")
        resp2 = await list_helpers(helper_type="input_boolean")

    assert resp["success"] is True
    assert resp["helper_type"] == "input_boolean"
    assert resp["count"] == 1
    assert resp2["count"] == 1
    # The legacy {type}/list fetch is never issued on the component path.
    assert client.list_calls == 0
    # Exactly one component helpers_list command per call, one cached info probe.
    assert len(_helpers_calls(ws)) == 2
    assert len(_info_calls(ws)) == 1
    # The command carries only the requested collection type, no flow helpers.
    first_helpers_call = _helpers_calls(ws)[0]
    assert first_helpers_call.kwargs["helper_types"] == ["input_boolean"]
    assert first_helpers_call.kwargs["include_flow_helpers"] is False


@pytest.mark.asyncio
async def test_rename_case_record_keeps_storage_id_and_adds_entity_id_name() -> None:
    """#1794 shape: storage id kept, current entity_id + name added additively."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_result=_CAPS_HELPERS,
        cmd_result=_component_helpers_result(),
    )
    client = RoutingClient()
    list_helpers = _build_list_helpers(client)

    with patch_ws(ws, tools_config_helpers):
        resp = await list_helpers(helper_type="input_boolean")

    (record,) = resp["helpers"]
    # Legacy keys keep their legacy meanings: id is the storage id, type-specific
    # keys survive.
    assert record["id"] == "abc123"
    assert record["icon"] == "mdi:flash"
    # Added: current entity_id, and name reflects the current display name
    # (overriding the stale creation-time "Old Name").
    assert record["entity_id"] == "input_boolean.foo"
    assert record["name"] == "New Name"


@pytest.mark.asyncio
async def test_component_and_legacy_envelope_parity() -> None:
    """Both serving paths return the same top-level envelope keys."""
    ws_component = make_ws(
        "ha_mcp_tools/helpers_list",
        info_result=_CAPS_HELPERS,
        cmd_result=_component_helpers_result(),
    )
    with patch_ws(ws_component, tools_config_helpers):
        component = await _build_list_helpers(RoutingClient())(
            helper_type="input_boolean"
        )

    # info → unknown_command yields no caps, so this run takes the legacy path.
    ws_legacy = make_ws(
        "ha_mcp_tools/helpers_list",
        info_exc=HomeAssistantCommandError(
            "Command failed: no info", "unknown_command"
        ),
    )
    with patch_ws(ws_legacy, tools_config_helpers):
        legacy = await _build_list_helpers(RoutingClient())(helper_type="input_boolean")

    assert set(component.keys()) == set(legacy.keys())
    assert component["success"] == legacy["success"] is True
    assert component["helper_type"] == legacy["helper_type"] == "input_boolean"
    assert component["count"] == legacy["count"] == 1
    # Neither happy path emits a fallback warning.
    assert "warnings" not in component
    assert "warnings" not in legacy


@pytest.mark.asyncio
async def test_unknown_command_falls_back_silently() -> None:
    """unknown_command on the helpers_list call → legacy path, no warning."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_result=_CAPS_HELPERS,
        cmd_exc=HomeAssistantCommandError("Command failed: gone", "unknown_command"),
    )
    client = RoutingClient()
    list_helpers = _build_list_helpers(client)

    with patch_ws(ws, tools_config_helpers):
        resp = await list_helpers(helper_type="input_boolean")

    assert resp["success"] is True
    # Legacy inventory served the request.
    assert client.list_calls == 1
    assert resp["count"] == 1
    # Silent fallback: no component-failure warning.
    assert not any("served via legacy path" in w for w in resp.get("warnings", []))


@pytest.mark.asyncio
async def test_raised_command_falls_back_with_warning() -> None:
    """A non-unknown command error → legacy path AND a warnings[] entry."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_result=_CAPS_HELPERS,
        cmd_exc=HomeAssistantCommandError("Command failed: boom", "internal_error"),
    )
    client = RoutingClient()
    list_helpers = _build_list_helpers(client)

    with patch_ws(ws, tools_config_helpers):
        resp = await list_helpers(helper_type="input_boolean")

    assert resp["success"] is True
    assert client.list_calls == 1
    assert any("served via legacy path" in w for w in resp["warnings"])


@pytest.mark.asyncio
async def test_capsless_component_pins_legacy_path() -> None:
    """Old component (info unknown_command) → legacy path, helpers_list never sent."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_exc=HomeAssistantCommandError(
            "Command failed: no info", "unknown_command"
        ),
    )
    client = RoutingClient()
    list_helpers = _build_list_helpers(client)

    with patch_ws(ws, tools_config_helpers):
        resp = await list_helpers(helper_type="input_boolean")

    assert resp["success"] is True
    assert client.list_calls == 1
    assert resp["count"] == 1
    # The component listing command must never be attempted without the capability.
    assert not _helpers_calls(ws)
    assert not any("served via legacy path" in w for w in resp.get("warnings", []))


@pytest.mark.asyncio
async def test_flow_records_are_dropped_from_component_output() -> None:
    """A flow-helper record must not surface in this collection-only tool."""
    mixed = {
        "helpers": [
            _component_helpers_result()["helpers"][0],
            {
                "helper_type": "template",
                "entry_id": "e1",
                "entity_id": "sensor.templated",
                "name": "Templated",
                "kind": "flow",
                "options": {"state": "{{ 1 }}"},
            },
        ],
        "count": 2,
        "covered_types": ["input_boolean"],
    }
    ws = make_ws(
        "ha_mcp_tools/helpers_list", info_result=_CAPS_HELPERS, cmd_result=mixed
    )
    client = _build_list_helpers(RoutingClient())

    with patch_ws(ws, tools_config_helpers):
        resp = await client(helper_type="input_boolean")

    assert resp["count"] == 1
    (record,) = resp["helpers"]
    assert record["id"] == "abc123"
    assert "entry_id" not in record
    assert "options" not in record


@pytest.mark.asyncio
async def test_flow_type_served_only_by_component() -> None:
    """A flow type is served through the component: entry_id + name + options."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_result=_CAPS_HELPERS,
        cmd_result=_component_flow_result(),
    )
    client = RoutingClient()
    list_helpers = _build_list_helpers(client)

    with patch_ws(ws, tools_config_helpers):
        resp = await list_helpers(helper_type="template")

    assert resp["success"] is True
    assert resp["helper_type"] == "template"
    assert resp["count"] == 1
    (record,) = resp["helpers"]
    assert record["helper_type"] == "template"
    assert record["entry_id"] == "cfgentry123"
    assert record["entity_id"] == "sensor.templated"
    assert record["name"] == "Templated Sensor"
    assert record["options"] == {"state": "{{ 1 + 1 }}"}
    # No storage id (flow helpers have none) and never any legacy fetch.
    assert "id" not in record
    assert client.list_calls == 0
    # The component command requests flow helpers for this type.
    (call,) = _helpers_calls(ws)
    assert call.kwargs["helper_types"] == ["template"]
    assert call.kwargs["include_flow_helpers"] is True


@pytest.mark.asyncio
async def test_flow_type_capsless_raises_component_required() -> None:
    """Flow type without a component surface → hard error, no legacy, no empty."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_exc=HomeAssistantCommandError(
            "Command failed: no info", "unknown_command"
        ),
    )
    client = RoutingClient()
    list_helpers = _build_list_helpers(client)

    with patch_ws(ws, tools_config_helpers), pytest.raises(ToolError) as excinfo:
        await list_helpers(helper_type="template")

    assert "COMPONENT_NOT_INSTALLED" in str(excinfo.value)
    assert "flow-based" in str(excinfo.value)
    # Never falls back to the legacy path and never issues the component command.
    assert client.list_calls == 0
    assert not _helpers_calls(ws)


@pytest.mark.asyncio
async def test_flow_type_component_error_raises_no_legacy_fallback() -> None:
    """A component handler error on a flow type → same hard error, no legacy."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_result=_CAPS_HELPERS,
        cmd_exc=HomeAssistantCommandError("Command failed: boom", "internal_error"),
    )
    client = RoutingClient()
    list_helpers = _build_list_helpers(client)

    with patch_ws(ws, tools_config_helpers), pytest.raises(ToolError) as excinfo:
        await list_helpers(helper_type="template")

    assert "COMPONENT_NOT_INSTALLED" in str(excinfo.value)
    # The legacy path cannot serve flow helpers, so it must not be attempted.
    assert client.list_calls == 0


@pytest.mark.asyncio
async def test_flow_type_unknown_command_raises_component_required() -> None:
    """A downgraded component (unknown_command) on a flow type → same hard error."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_result=_CAPS_HELPERS,
        cmd_exc=HomeAssistantCommandError("Command failed: gone", "unknown_command"),
    )
    client = RoutingClient()
    list_helpers = _build_list_helpers(client)

    with patch_ws(ws, tools_config_helpers), pytest.raises(ToolError) as excinfo:
        await list_helpers(helper_type="template")

    assert "COMPONENT_NOT_INSTALLED" in str(excinfo.value)
    assert client.list_calls == 0


@pytest.mark.asyncio
async def test_uncovered_storage_type_falls_back_to_legacy() -> None:
    """A storage type absent from covered_types (tag) → legacy serves it."""
    # The component ran but its from-states scan can't see tags, so tag is not
    # in covered_types even though the command succeeded with an empty list.
    result = {"helpers": [], "count": 0, "covered_types": ["zone", "person"]}
    ws = make_ws(
        "ha_mcp_tools/helpers_list", info_result=_CAPS_HELPERS, cmd_result=result
    )
    client = RoutingClient()
    list_helpers = _build_list_helpers(client)

    with patch_ws(ws, tools_config_helpers):
        resp = await list_helpers(helper_type="tag")

    # The component was consulted exactly once, then legacy tag/list served.
    assert len(_helpers_calls(ws)) == 1
    assert client.list_calls == 1
    assert resp["helper_type"] == "tag"
    assert resp["count"] == 1
    assert resp["helpers"][0]["id"] == "tag-42"
    # Silent fallback: no component-failure warning.
    assert not any("served via legacy path" in w for w in resp.get("warnings", []))


@pytest.mark.asyncio
async def test_missing_covered_types_falls_back_to_legacy() -> None:
    """An older component with no covered_types → conservative legacy fallback."""
    result = {
        "helpers": _component_helpers_result()["helpers"],
        "count": 1,
    }  # no covered_types key
    ws = make_ws(
        "ha_mcp_tools/helpers_list", info_result=_CAPS_HELPERS, cmd_result=result
    )
    client = RoutingClient()
    list_helpers = _build_list_helpers(client)

    with patch_ws(ws, tools_config_helpers):
        resp = await list_helpers(helper_type="input_boolean")

    # Component consulted once, but its list isn't trusted → legacy serves.
    assert len(_helpers_calls(ws)) == 1
    assert client.list_calls == 1
    assert resp["count"] == 1
    assert resp["helpers"][0]["id"] == "abc123"


@pytest.mark.asyncio
async def test_storage_command_timeout_falls_back_with_warning() -> None:
    """A storage-type component WS timeout → legacy path AND a warnings[] entry."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_result=_CAPS_HELPERS,
        cmd_exc=HomeAssistantCommandTimeout("Command timeout"),
    )
    client = RoutingClient()
    list_helpers = _build_list_helpers(client)

    with patch_ws(ws, tools_config_helpers):
        resp = await list_helpers(helper_type="input_boolean")

    assert resp["success"] is True
    assert client.list_calls == 1
    assert any("served via legacy path" in w for w in resp["warnings"])


@pytest.mark.asyncio
async def test_flow_command_timeout_raises_component_required() -> None:
    """A flow-type component WS timeout → hard component-required error, no legacy."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_result=_CAPS_HELPERS,
        cmd_exc=HomeAssistantCommandTimeout("Command timeout"),
    )
    client = RoutingClient()
    list_helpers = _build_list_helpers(client)

    with patch_ws(ws, tools_config_helpers), pytest.raises(ToolError) as excinfo:
        await list_helpers(helper_type="template")

    assert "COMPONENT_NOT_INSTALLED" in str(excinfo.value)
    assert client.list_calls == 0


@pytest.mark.asyncio
async def test_flow_type_missing_covered_types_raises_component_required() -> None:
    """Flow type + response with no covered_types → component-required, not empty.

    A flow type is always covered when the component enumerated it; a response
    that omits covered_types can't be trusted as "no such helpers", and there is
    no legacy path for flow types, so the tool raises rather than returning an
    empty list.
    """
    result = {"helpers": [], "count": 0}  # no covered_types key
    ws = make_ws(
        "ha_mcp_tools/helpers_list", info_result=_CAPS_HELPERS, cmd_result=result
    )
    client = RoutingClient()
    list_helpers = _build_list_helpers(client)

    with patch_ws(ws, tools_config_helpers), pytest.raises(ToolError) as excinfo:
        await list_helpers(helper_type="template")

    assert "COMPONENT_NOT_INSTALLED" in str(excinfo.value)
    assert client.list_calls == 0


def test_collection_record_prefers_storage_id_over_object_id() -> None:
    """``_shape_collection_helper_record`` consumes the authoritative storage_id.

    person/zone bodies don't carry their own ``id``; the component reads it from
    the real storage collection as ``storage_id``, which must win over both the
    (absent) body ``id`` and the record-level ``object_id``.
    """
    rec = {
        "helper_type": "person",
        "storage_id": "person.abc",
        "object_id": "derived_from_entity",
        "entity_id": "person.alice",
        "name": "Alice",
        "kind": "collection",
        "config": {"name": "Alice", "user_id": "u1"},  # no ``id`` in the body
    }
    out = _shape_collection_helper_record(rec)
    assert out["id"] == "person.abc"
    assert out["entity_id"] == "person.alice"
    assert out["name"] == "Alice"
    assert out["user_id"] == "u1"


def test_collection_record_falls_back_to_object_id_without_storage_id() -> None:
    """Older component (no storage_id) still keys ``id`` off ``object_id``."""
    rec = {
        "helper_type": "input_boolean",
        "object_id": "guest_mode",
        "entity_id": "input_boolean.guest_mode",
        "name": "Guest",
        "kind": "collection",
        "config": {"name": "Guest"},
    }
    out = _shape_collection_helper_record(rec)
    assert out["id"] == "guest_mode"
