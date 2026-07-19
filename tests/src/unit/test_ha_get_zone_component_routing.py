"""Routing tests for ``ha_get_zone`` over the ``ha_mcp_tools`` component gate.

Core's ``zone/list`` WS command serves only the storage collection, so
YAML-defined zones — including the auto-synthesized ``home`` zone — are
structurally absent from it. When the component advertises the ``helpers_list``
capability, ``ha_get_zone`` enumerates zones through one
``ha_mcp_tools/helpers_list`` call instead: YAML zones come back with
``storage_id=None`` and are surfaced as additive rows carrying an
``editable`` / ``source`` discriminator, while storage zones keep their exact
legacy body plus the discriminator.

These tests pin that fast path, the record shape (storage byte-identical modulo
the additive discriminator; YAML ``home`` present and flagged), get-by-id via the
component, and the error-taxonomy fallbacks (silent on ``unknown_command``;
legacy + ``warnings[]`` on any other command error; legacy pin when the
component has no WS surface or omits ``covered_types``).

The WS client is an ``AsyncMock`` whose ``send_command`` dispatches on the
command type; the HA client is a spy that tallies the legacy ``zone/list`` fetch
so a test can assert it never ran on the component path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api, tools_zones
from ha_mcp.tools.tools_zones import register_zone_tools

from ._component_routing_helpers import (
    make_ws,
    patch_ws,
    patch_ws_establish_failure,
)

# The single storage zone the legacy ``zone/list`` path can see. Its body is the
# exact legacy record shape the component supplies as ``config``.
_LEGACY_STORAGE_ZONE = {
    "id": "work",
    "name": "Work",
    "latitude": 40.0,
    "longitude": -74.0,
    "radius": 100,
    "passive": False,
    "icon": "mdi:briefcase",
}

_CAPS_HELPERS = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["helpers_list"],
    "limits": {},
}


def _component_zone_result() -> dict[str, Any]:
    """Component ``helpers_list`` zone output: one storage zone + the YAML home zone."""
    return {
        "helpers": [
            {
                "helper_type": "zone",
                "kind": "collection",
                "entity_id": "zone.work",
                "object_id": "work",
                "name": "Work",
                "storage_id": "work",
                "config": dict(_LEGACY_STORAGE_ZONE),
            },
            {
                "helper_type": "zone",
                "kind": "collection",
                "entity_id": "zone.home",
                "object_id": "home",
                "name": "Home",
                # YAML/config zone: state-only record. The component backfills
                # storage_id with the registry unique_id or object_id (it is
                # NOT None), and the state-attribute body carries core's
                # editable=False — the actual YAML discriminator.
                "storage_id": "home",
                "config": {
                    "name": "Home",
                    "latitude": 41.0,
                    "longitude": -75.0,
                    "radius": 100,
                    "passive": False,
                    "editable": False,
                },
            },
        ],
        "count": 2,
        "covered_types": ["input_boolean", "person", "zone"],
    }


class RoutingClient:
    """Credentialed HA client spy: tallies every legacy ``zone/list`` fetch."""

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.list_calls = 0

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        self.list_calls += 1
        if msg.get("type") == "zone/list":
            return {"success": True, "result": [dict(_LEGACY_STORAGE_ZONE)]}
        return {"success": False, "error": "unexpected list type"}


def _build_get_zone(client: Any) -> Any:
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
    register_zone_tools(mcp, client)
    return registered["ha_get_zone"]


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    """Isolate the module-global caps cache between tests."""
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


def _zone_calls(ws: AsyncMock) -> list[Any]:
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
async def test_component_fast_path_lists_yaml_and_storage_zones() -> None:
    """Component serves the listing: YAML home zone included, no legacy fetch."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_result=_CAPS_HELPERS,
        cmd_result=_component_zone_result(),
    )
    client = RoutingClient()
    get_zone = _build_get_zone(client)

    with patch_ws(ws, tools_zones):
        resp = await get_zone()
        resp2 = await get_zone()

    assert resp["success"] is True
    assert resp["count"] == 2
    assert resp2["count"] == 2
    names = {z.get("name") for z in resp["zones"]}
    assert names == {"Work", "Home"}
    # The legacy zone/list fetch is never issued on the component path.
    assert client.list_calls == 0
    # Exactly one component command per call, one cached info probe.
    assert len(_zone_calls(ws)) == 2
    assert len(_info_calls(ws)) == 1
    # The command asks only for zones, no flow helpers.
    first = _zone_calls(ws)[0]
    assert first.kwargs["helper_types"] == ["zone"]
    assert first.kwargs["include_flow_helpers"] is False


@pytest.mark.asyncio
async def test_storage_zone_byte_identical_plus_discriminator() -> None:
    """A storage zone keeps its exact legacy body; only the discriminator is added."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_result=_CAPS_HELPERS,
        cmd_result=_component_zone_result(),
    )
    get_zone = _build_get_zone(RoutingClient())

    with patch_ws(ws, tools_zones):
        resp = await get_zone()

    work = next(z for z in resp["zones"] if z.get("id") == "work")
    assert work["editable"] is True
    assert work["source"] == "storage"
    # Stripping the additive discriminator leaves the legacy zone/list body.
    stripped = {k: v for k, v in work.items() if k not in ("editable", "source")}
    assert stripped == _LEGACY_STORAGE_ZONE


@pytest.mark.asyncio
async def test_yaml_home_zone_additive_row_with_discriminator() -> None:
    """The YAML home zone appears as an additive, non-editable row."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_result=_CAPS_HELPERS,
        cmd_result=_component_zone_result(),
    )
    get_zone = _build_get_zone(RoutingClient())

    with patch_ws(ws, tools_zones):
        resp = await get_zone()

    home = next(z for z in resp["zones"] if z.get("name") == "Home")
    assert home["editable"] is False
    assert home["source"] == "yaml"
    assert home["latitude"] == 41.0
    # YAML zones carry the object_id so they remain fetchable by zone_id.
    assert home["id"] == "home"


@pytest.mark.asyncio
async def test_get_specific_storage_zone_via_component() -> None:
    """A get-by-id request resolves against the component's storage zone."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_result=_CAPS_HELPERS,
        cmd_result=_component_zone_result(),
    )
    client = RoutingClient()
    get_zone = _build_get_zone(client)

    with patch_ws(ws, tools_zones):
        resp = await get_zone(zone_id="work")

    assert resp["success"] is True
    assert resp["zone_id"] == "work"
    assert resp["zone"]["name"] == "Work"
    assert resp["zone"]["source"] == "storage"
    assert client.list_calls == 0


@pytest.mark.asyncio
async def test_get_missing_zone_raises_not_found() -> None:
    """A get-by-id for an absent zone raises RESOURCE_NOT_FOUND (unchanged)."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_result=_CAPS_HELPERS,
        cmd_result=_component_zone_result(),
    )
    get_zone = _build_get_zone(RoutingClient())

    with patch_ws(ws, tools_zones), pytest.raises(ToolError) as excinfo:
        await get_zone(zone_id="does_not_exist")

    assert "Zone not found" in str(excinfo.value)


@pytest.mark.asyncio
async def test_capsless_component_pins_legacy_path() -> None:
    """Old component (info unknown_command) → legacy path, only storage zones."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_exc=HomeAssistantCommandError(
            "Command failed: no info", "unknown_command"
        ),
    )
    client = RoutingClient()
    get_zone = _build_get_zone(client)

    with patch_ws(ws, tools_zones):
        resp = await get_zone()

    assert resp["success"] is True
    assert resp["count"] == 1
    assert resp["zones"] == [dict(_LEGACY_STORAGE_ZONE)]
    assert client.list_calls == 1
    # The component listing command must never be attempted without the capability.
    assert not _zone_calls(ws)
    assert not any("served via legacy path" in w for w in resp.get("warnings", []))


@pytest.mark.asyncio
async def test_unknown_command_falls_back_silently() -> None:
    """unknown_command on the zone list call → legacy path, no warning."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_result=_CAPS_HELPERS,
        cmd_exc=HomeAssistantCommandError("Command failed: gone", "unknown_command"),
    )
    client = RoutingClient()
    get_zone = _build_get_zone(client)

    with patch_ws(ws, tools_zones):
        resp = await get_zone()

    assert resp["success"] is True
    # Legacy inventory served the request (storage zone only).
    assert client.list_calls == 1
    assert resp["count"] == 1
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
    get_zone = _build_get_zone(client)

    with patch_ws(ws, tools_zones):
        resp = await get_zone()

    assert resp["success"] is True
    assert client.list_calls == 1
    assert resp["count"] == 1
    assert any("served via legacy path" in w for w in resp["warnings"])


@pytest.mark.asyncio
async def test_command_timeout_falls_back_with_warning() -> None:
    """A component WS zone-list timeout → legacy path AND a warnings[] entry."""
    ws = make_ws(
        "ha_mcp_tools/helpers_list",
        info_result=_CAPS_HELPERS,
        cmd_exc=HomeAssistantCommandTimeout("Command timeout"),
    )
    client = RoutingClient()
    get_zone = _build_get_zone(client)

    with patch_ws(ws, tools_zones):
        resp = await get_zone()

    assert resp["success"] is True
    assert client.list_calls == 1
    assert any("served via legacy path" in w for w in resp["warnings"])


@pytest.mark.asyncio
async def test_ws_establish_failure_falls_back_with_warning() -> None:
    """A plain establish ``Exception`` from ``get_websocket_client()`` (after caps
    are cached) → legacy ``zone/list`` AND a ``warnings[]`` entry, not a propagated
    error. The legacy zone list rides the swallowing bridge, so it does not die
    identically on a pooled-WS drop."""
    caps_ws = make_ws("ha_mcp_tools/helpers_list", info_result=_CAPS_HELPERS)
    client = RoutingClient()
    get_zone = _build_get_zone(client)

    with patch_ws_establish_failure(
        caps_ws,
        tools_zones,
        HomeAssistantConnectionError("Failed to connect to Home Assistant WebSocket"),
    ):
        resp = await get_zone()

    assert resp["success"] is True
    assert client.list_calls == 1
    assert any("served via legacy path" in w for w in resp["warnings"])


@pytest.mark.asyncio
async def test_missing_covered_types_falls_back_to_legacy() -> None:
    """An older component with no covered_types → conservative legacy fallback."""
    result = _component_zone_result()
    del result["covered_types"]
    ws = make_ws(
        "ha_mcp_tools/helpers_list", info_result=_CAPS_HELPERS, cmd_result=result
    )
    client = RoutingClient()
    get_zone = _build_get_zone(client)

    with patch_ws(ws, tools_zones):
        resp = await get_zone()

    # Component consulted once, but its list isn't trusted → legacy serves.
    assert len(_zone_calls(ws)) == 1
    assert client.list_calls == 1
    assert resp["count"] == 1
    assert resp["zones"][0]["id"] == "work"
    # Silent conservative fallback: no component-failure warning.
    assert not any("served via legacy path" in w for w in resp.get("warnings", []))
