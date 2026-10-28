"""ha_config_get_dashboard on a dashboard Home Assistant holds no config for (#2632).

Core answers "No config found." (the component: ``ConfigNotFound``) and the
frontend shows its generated default view, so the get answers with
``stored_config`` false instead of failing, through the same envelope as any
other get; an identifier Core does not know is RESOURCE_NOT_FOUND.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools import tools_config_dashboards

from ._component_routing_helpers import make_ws, patch_ws
from .test_ha_dashboards_component_routing import (
    _CAPS_DASHBOARDS,
    _CAPS_NONE,
    _STORAGE_ROW,
    RoutingClient,
    _build_get_dashboard,
)

_NO_CONFIG_COMPONENT_REPLY = {
    "mode": "get",
    "available": True,
    "status": "no_config",
    "url_path": "fresh",
    "config": None,
}


class _FlatEnvelopeClient(RoutingClient):
    """Answers a no-config read the way ``send_websocket_message`` flattens it."""

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("type") == "lovelace/config":
            return {
                "success": False,
                "error": "Command failed: No config found.",
                "error_code": "config_not_found",
            }
        return await super().send_websocket_message(msg)


def _error(exc: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc.value))["error"]  # type: ignore[no-any-return]


async def test_component_no_config_answers_without_a_legacy_read() -> None:
    ws = make_ws(
        "ha_mcp_tools/dashboards",
        info_result=_CAPS_DASHBOARDS,
        cmd_result=_NO_CONFIG_COMPONENT_REPLY,
    )
    client = RoutingClient()
    get_dashboard = _build_get_dashboard(client)

    with patch_ws(ws, tools_config_dashboards):
        resp = await get_dashboard(url_path="fresh")

    assert resp["success"] is True
    assert resp["stored_config"] is False
    assert resp["config"] is None
    assert client.config_calls == []


async def test_legacy_read_answers_no_config_the_same_way() -> None:
    ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    get_dashboard = _build_get_dashboard(_FlatEnvelopeClient())

    with patch_ws(ws, tools_config_dashboards):
        resp = await get_dashboard(url_path="fresh")

    assert resp["success"] is True
    assert resp["stored_config"] is False


async def test_lazily_resolved_id_keeps_resolved_from() -> None:
    """An internal id resolved to a dashboard with no config still reports the
    identifier the caller passed."""
    ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    client = RoutingClient(
        dashboards_list=[{**_STORAGE_ROW, "id": "id-fresh", "url_path": "fresh"}],
        no_config=frozenset({"fresh"}),
    )
    get_dashboard = _build_get_dashboard(client)

    with patch_ws(ws, tools_config_dashboards):
        resp = await get_dashboard(url_path="id-fresh")

    assert resp["url_path"] == "fresh"
    assert resp["resolved_from"] == "id-fresh"
    assert resp["stored_config"] is False


async def test_view_path_on_dashboard_without_config_is_refused() -> None:
    """There are no static views to scope to, so view_path is not dropped silently."""
    ws = make_ws(
        "ha_mcp_tools/dashboards",
        info_result=_CAPS_DASHBOARDS,
        cmd_result=_NO_CONFIG_COMPONENT_REPLY,
    )
    get_dashboard = _build_get_dashboard(RoutingClient())

    with patch_ws(ws, tools_config_dashboards), pytest.raises(ToolError) as exc:
        await get_dashboard(url_path="fresh", view_path="kitchen")

    assert _error(exc)["code"] == "VALIDATION_INVALID_PARAMETER"


async def test_screenshot_request_on_dashboard_without_config_is_answered() -> None:
    """A screenshot request is handled like any get's (here: the feature is
    off), not dropped without a word."""
    ws = make_ws(
        "ha_mcp_tools/dashboards",
        info_result=_CAPS_DASHBOARDS,
        cmd_result=_NO_CONFIG_COMPONENT_REPLY,
    )
    get_dashboard = _build_get_dashboard(RoutingClient())

    with patch_ws(ws, tools_config_dashboards):
        resp = await get_dashboard(url_path="fresh", include_screenshot=True)

    assert any("Screenshot" in w for w in resp.get("warnings", []))


@pytest.mark.parametrize(
    "client",
    [RoutingClient(no_config=frozenset({"fresh"})), _FlatEnvelopeClient()],
    ids=["nested-error", "flat-envelope"],
)
async def test_card_search_on_dashboard_without_config_says_why(
    client: RoutingClient,
) -> None:
    ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    get_dashboard = _build_get_dashboard(client)

    with patch_ws(ws, tools_config_dashboards), pytest.raises(ToolError) as exc:
        await get_dashboard(url_path="fresh", card_type="tile")

    error = _error(exc)
    assert error["code"] == "VALIDATION_FAILED"
    assert "no config" in error["message"]


async def test_card_search_on_unknown_dashboard_is_not_found() -> None:
    ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    get_dashboard = _build_get_dashboard(RoutingClient())

    with patch_ws(ws, tools_config_dashboards), pytest.raises(ToolError) as exc:
        await get_dashboard(url_path="ghost", card_type="tile")

    assert _error(exc)["code"] == "RESOURCE_NOT_FOUND"
