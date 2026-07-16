"""Routing tests for ``ha_get_blueprint`` over the ``ha_mcp_tools`` component gate.

core's ``blueprint/list`` returns metadata only — never the body — so
``ha_get_blueprint`` can serve triggers/conditions/actions/sequence only when the
component advertises the ``blueprint_get`` capability, which reads the on-disk
blueprint file and returns the full parsed body merged additively under
``config``. These tests pin that merge, the metadata-only fallback when the
component is absent / lacks the capability / returns a null body, and the
error-taxonomy fallbacks (invalidate on ``unknown_command``; metadata-only on
other command errors).

The WS client is an ``AsyncMock`` whose ``send_command`` dispatches on the
command type; the HA client is a spy serving the legacy ``blueprint/list``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ha_mcp.tools import component_api, tools_blueprints
from ha_mcp.tools.tools_blueprints import register_blueprint_tools

from ._component_routing_helpers import make_ws, patch_ws

_PATH = "user/motion.yaml"
_LIST_RESULT = {
    "success": True,
    "result": {
        _PATH: {
            "metadata": {
                "name": "Motion Light",
                "description": "Turn on a light on motion.",
                "domain": "automation",
                "input": {"motion_sensor": {"name": "Motion Sensor"}},
            }
        }
    },
}

_FULL_BODY = {
    "blueprint": {
        "name": "Motion Light",
        "domain": "automation",
        "input": {"motion_sensor": {"name": "Motion Sensor"}},
    },
    "trigger": [
        {"platform": "state", "entity_id": {"__input__": "motion_sensor"}, "to": "on"}
    ],
    "action": [{"service": "light.turn_on"}],
}

_CAPS_BLUEPRINT = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["blueprint_get"],
    "limits": {},
}
_CAPS_OTHER = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["search"],
    "limits": {},
}


def _component_blueprint_result(config: dict[str, Any] | None) -> dict[str, Any]:
    return {"metadata": _FULL_BODY["blueprint"], "config": config}


class RoutingClient:
    """Credentialed HA client spy: serves the legacy ``blueprint/list`` WS call."""

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.list_calls = 0

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        self.list_calls += 1
        if msg.get("type") == "blueprint/list":
            return {"success": True, "result": dict(_LIST_RESULT["result"])}
        return {"success": False, "error": "unexpected"}


def _build_get_blueprint(client: Any) -> Any:
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
    register_blueprint_tools(mcp, client)
    return registered["ha_get_blueprint"]


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


def _bp_calls(ws: AsyncMock) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/blueprint_get"
    ]


@pytest.mark.asyncio
async def test_component_merges_full_body_under_config() -> None:
    """The component body is merged additively; metadata still comes from list."""
    ws = make_ws(
        "ha_mcp_tools/blueprint_get",
        info_result=_CAPS_BLUEPRINT,
        cmd_result=_component_blueprint_result(_FULL_BODY),
    )
    client = RoutingClient()
    get_blueprint = _build_get_blueprint(client)

    with patch_ws(ws, tools_blueprints):
        resp = await get_blueprint(path=_PATH, domain="automation")

    assert resp["success"] is True
    assert resp["metadata"]["name"] == "Motion Light"
    assert resp["inputs"] == {"motion_sensor": {"name": "Motion Sensor"}}
    # The additive body core's blueprint/list never returns.
    assert resp["config"]["trigger"][0]["entity_id"] == {"__input__": "motion_sensor"}
    assert resp["config"]["action"] == [{"service": "light.turn_on"}]
    call = _bp_calls(ws)[0]
    assert call.kwargs == {"domain": "automation", "path": _PATH}


@pytest.mark.asyncio
async def test_capability_miss_serves_metadata_only() -> None:
    """No blueprint_get capability → metadata + inputs only; body never fetched."""
    ws = make_ws(
        "ha_mcp_tools/blueprint_get",
        info_result=_CAPS_OTHER,
        cmd_result=_component_blueprint_result(_FULL_BODY),
    )
    client = RoutingClient()
    get_blueprint = _build_get_blueprint(client)

    with patch_ws(ws, tools_blueprints):
        resp = await get_blueprint(path=_PATH, domain="automation")

    assert resp["success"] is True
    assert resp["metadata"]["name"] == "Motion Light"
    assert "config" not in resp
    assert not _bp_calls(ws)


@pytest.mark.asyncio
async def test_null_config_from_component_stays_metadata_only() -> None:
    """A jail reject / missing file (component returns config=None) → metadata-only."""
    ws = make_ws(
        "ha_mcp_tools/blueprint_get",
        info_result=_CAPS_BLUEPRINT,
        cmd_result=_component_blueprint_result(None),
    )
    client = RoutingClient()
    get_blueprint = _build_get_blueprint(client)

    with patch_ws(ws, tools_blueprints):
        resp = await get_blueprint(path=_PATH, domain="automation")

    assert resp["success"] is True
    assert "config" not in resp
    assert len(_bp_calls(ws)) == 1


@pytest.mark.asyncio
async def test_unknown_command_invalidates_and_metadata_only() -> None:
    """unknown_command on blueprint_get → invalidate caps + metadata-only."""
    ws = make_ws(
        "ha_mcp_tools/blueprint_get",
        info_result=_CAPS_BLUEPRINT,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient()
    get_blueprint = _build_get_blueprint(client)

    with patch_ws(ws, tools_blueprints):
        resp = await get_blueprint(path=_PATH, domain="automation")

    assert resp["success"] is True
    assert "config" not in resp


@pytest.mark.asyncio
async def test_command_error_metadata_only_silent() -> None:
    """A non-unknown command error / timeout → metadata-only (logged, no raise)."""
    ws = make_ws(
        "ha_mcp_tools/blueprint_get",
        info_result=_CAPS_BLUEPRINT,
        cmd_exc=HomeAssistantCommandTimeout("timeout"),
    )
    client = RoutingClient()
    get_blueprint = _build_get_blueprint(client)

    with patch_ws(ws, tools_blueprints):
        resp = await get_blueprint(path=_PATH, domain="automation")

    assert resp["success"] is True
    assert "config" not in resp


@pytest.mark.asyncio
async def test_list_mode_never_touches_component() -> None:
    """Listing (no path) is unchanged: the component is never probed or called."""
    ws = make_ws(
        "ha_mcp_tools/blueprint_get",
        info_result=_CAPS_BLUEPRINT,
        cmd_result=_component_blueprint_result(_FULL_BODY),
    )
    client = RoutingClient()
    get_blueprint = _build_get_blueprint(client)

    with patch_ws(ws, tools_blueprints):
        resp = await get_blueprint(domain="automation")

    assert resp["success"] is True
    assert resp["count"] == 1
    assert not _bp_calls(ws)
    assert not [
        c for c in ws.send_command.call_args_list if c.args[0] == "ha_mcp_tools/info"
    ]
