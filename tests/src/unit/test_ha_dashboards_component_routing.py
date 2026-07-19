"""Routing tests for the dashboard tools over the ``ha_mcp_tools`` gate.

The dashboard reads (``ha_config_get_dashboard`` list / get / cross-dashboard
search, plus the shared ``fetch_dashboards_list`` that the set/delete
existence-checks funnel through) prefer a single in-process
``ha_mcp_tools/dashboards`` frame when the component advertises the
``dashboards`` capability, and otherwise fall back to the legacy
``lovelace/dashboards/list`` / ``lovelace/config`` WS reads. These tests pin the
component-served shapes (list keeps YAML metadata rows — matching legacy —, get
body, search matches), the per-call YAML fallback (a ``yaml_excluded`` get drops
to legacy), the legacy search walk never reading a YAML dashboard's body, and the
error-taxonomy fallbacks — capability miss, ``unknown_command`` (invalidate caps +
legacy), a command error/timeout, and a transport failure (both a
``HomeAssistantConnectionError`` off the frame and a plain ``Exception`` from
``get_websocket_client()`` failing to establish the socket) → ``None`` (legacy):
the legacy dashboards path rides the never-raising bridge and the auto-backup
capture consumer must not be blocked, so a transport failure falls back rather
than propagating.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api, tools_config_dashboards
from ha_mcp.tools.tools_config_dashboards import (
    DashboardConfigTools,
    register_config_dashboard_tools,
)

from ._component_routing_helpers import (
    make_ws,
    patch_ws,
    patch_ws_establish_failure,
)

_CAPS_DASHBOARDS = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["dashboards"],
    "limits": {},
}
_CAPS_NONE = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": [],
    "limits": {},
}

# One storage + one YAML row, as the component's ``list`` mode returns them
# (every runtime dashboard tagged with an additive ``mode``).
_STORAGE_ROW = {
    "id": "id-home",
    "url_path": "home",
    "title": "Home",
    "icon": "mdi:home",
    "show_in_sidebar": True,
    "require_admin": False,
    "mode": "storage",
}
_YAML_ROW = {
    "id": "id-yaml",
    "url_path": "yaml-dash",
    "title": "YAML",
    "icon": None,
    "show_in_sidebar": True,
    "require_admin": False,
    "mode": "yaml",
}

_HOME_BODY = {
    "title": "Home",
    "views": [
        {
            "title": "Living",
            "cards": [{"type": "entities", "entities": ["light.kitchen"]}],
        }
    ],
}
_YAML_BODY = {"views": [{"cards": [{"type": "markdown", "content": "yaml only"}]}]}


class RoutingClient:
    """Credentialed HA client spy: serves the legacy lovelace WS reads."""

    def __init__(
        self,
        *,
        dashboards_list: list[dict[str, Any]] | None = None,
        configs: dict[str | None, dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self._dashboards_list = list(dashboards_list or [])
        self._configs = dict(configs or {})
        self.list_calls = 0
        self.config_calls: list[str | None] = []

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        t = msg.get("type")
        if t == "lovelace/dashboards/list":
            self.list_calls += 1
            return {"success": True, "result": list(self._dashboards_list)}
        if t == "lovelace/config":
            url_path = msg.get("url_path")
            self.config_calls.append(url_path)
            if url_path in self._configs:
                return {"success": True, "result": self._configs[url_path]}
            return {
                "success": False,
                "error": {"message": f"Unknown config specified: {url_path}"},
            }
        raise AssertionError(f"unexpected ws message {t!r}")


def _build_get_dashboard(client: Any) -> Any:
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
    register_config_dashboard_tools(mcp, client)
    return registered["ha_config_get_dashboard"]


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    component_api._NEGATIVE_CACHE_TS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    component_api._NEGATIVE_CACHE_TS.clear()


def _dash_calls(ws: Any) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/dashboards"
    ]


# --- list ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_served_via_component_keeps_yaml() -> None:
    """list_only served from the component ``list`` frame keeps YAML metadata rows.

    Listing metadata is safe (only bodies carry resolved ``!secret``), and the
    legacy ``lovelace/dashboards/list`` includes YAML rows too — dropping them
    here diverged the two paths.
    """
    ws = make_ws(
        "ha_mcp_tools/dashboards",
        info_result=_CAPS_DASHBOARDS,
        cmd_result={
            "mode": "list",
            "available": True,
            "dashboards": [_STORAGE_ROW, _YAML_ROW],
        },
    )
    client = RoutingClient()
    get_dashboard = _build_get_dashboard(client)

    with patch_ws(ws, tools_config_dashboards):
        resp = await get_dashboard(list_only=True)

    assert resp["dashboards"] == [_STORAGE_ROW, _YAML_ROW]
    assert resp["count"] == 2
    assert client.list_calls == 0
    assert _dash_calls(ws)[0].kwargs == {"mode": "list"}


@pytest.mark.asyncio
async def test_list_capability_miss_uses_legacy() -> None:
    """No ``dashboards`` capability → the legacy lovelace/dashboards/list read."""
    ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    client = RoutingClient(dashboards_list=[_STORAGE_ROW])
    get_dashboard = _build_get_dashboard(client)

    with patch_ws(ws, tools_config_dashboards):
        resp = await get_dashboard(list_only=True)

    assert resp["dashboards"] == [_STORAGE_ROW]
    assert client.list_calls == 1
    assert not _dash_calls(ws)


@pytest.mark.asyncio
async def test_list_unknown_command_invalidates_and_falls_back() -> None:
    """unknown_command on the dashboards frame → invalidate caps + legacy list."""
    ws = make_ws(
        "ha_mcp_tools/dashboards",
        info_result=_CAPS_DASHBOARDS,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient(dashboards_list=[_STORAGE_ROW])
    get_dashboard = _build_get_dashboard(client)

    with patch_ws(ws, tools_config_dashboards):
        resp = await get_dashboard(list_only=True)

    assert resp["dashboards"] == [_STORAGE_ROW]
    assert client.list_calls == 1
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_list_command_error_falls_back_to_legacy() -> None:
    """A non-unknown command error/timeout → legacy list, caps NOT invalidated."""
    ws = make_ws(
        "ha_mcp_tools/dashboards",
        info_result=_CAPS_DASHBOARDS,
        cmd_exc=HomeAssistantCommandTimeout("slow"),
    )
    client = RoutingClient(dashboards_list=[_STORAGE_ROW])
    get_dashboard = _build_get_dashboard(client)

    with patch_ws(ws, tools_config_dashboards):
        resp = await get_dashboard(list_only=True)

    assert resp["dashboards"] == [_STORAGE_ROW]
    assert client.list_calls == 1
    # A transient command error keeps the (positive) caps entry cached.
    assert client in component_api._CAPS_CACHE


# --- get ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_served_via_component() -> None:
    """Get served from the component ``get`` frame (status ok), no legacy read."""
    ws = make_ws(
        "ha_mcp_tools/dashboards",
        info_result=_CAPS_DASHBOARDS,
        cmd_result={
            "mode": "get",
            "available": True,
            "status": "ok",
            "url_path": "home",
            "config": _HOME_BODY,
        },
    )
    client = RoutingClient()
    get_dashboard = _build_get_dashboard(client)

    with patch_ws(ws, tools_config_dashboards):
        resp = await get_dashboard(url_path="home")

    assert resp["action"] == "get"
    assert resp["url_path"] == "home"
    assert resp["config"] == _HOME_BODY
    assert resp["config_hash"] is not None
    assert client.config_calls == []
    assert _dash_calls(ws)[0].kwargs == {"mode": "get", "url_path": "home"}


@pytest.mark.asyncio
async def test_get_default_dashboard_sends_no_url_path() -> None:
    """url_path omitted/'default' maps to the component's None (default) dashboard."""
    ws = make_ws(
        "ha_mcp_tools/dashboards",
        info_result=_CAPS_DASHBOARDS,
        cmd_result={
            "mode": "get",
            "available": True,
            "status": "ok",
            "url_path": None,
            "config": _HOME_BODY,
        },
    )
    client = RoutingClient()
    get_dashboard = _build_get_dashboard(client)

    with patch_ws(ws, tools_config_dashboards):
        resp = await get_dashboard(url_path="default")

    assert resp["config"] == _HOME_BODY
    # The tool's "default" alias resolves to the component default (no url_path).
    assert _dash_calls(ws)[0].kwargs == {"mode": "get"}
    assert client.config_calls == []


@pytest.mark.asyncio
async def test_get_yaml_dashboard_falls_back_to_legacy() -> None:
    """A ``yaml_excluded`` component get drops to the legacy lovelace/config read."""
    ws = make_ws(
        "ha_mcp_tools/dashboards",
        info_result=_CAPS_DASHBOARDS,
        cmd_result={
            "mode": "get",
            "available": True,
            "status": "yaml_excluded",
            "url_path": "yaml-dash",
            "config": None,
        },
    )
    client = RoutingClient(configs={"yaml-dash": _YAML_BODY})
    get_dashboard = _build_get_dashboard(client)

    with patch_ws(ws, tools_config_dashboards):
        resp = await get_dashboard(url_path="yaml-dash")

    # The component refused the YAML body; the legacy read served it.
    assert resp["config"] == _YAML_BODY
    assert client.config_calls == ["yaml-dash"]


@pytest.mark.asyncio
async def test_get_not_found_falls_back_to_legacy() -> None:
    """A ``not_found`` component get lets the legacy path produce the real error."""
    ws = make_ws(
        "ha_mcp_tools/dashboards",
        info_result=_CAPS_DASHBOARDS,
        cmd_result={
            "mode": "get",
            "available": True,
            "status": "not_found",
            "url_path": "ghost",
            "config": None,
        },
    )
    client = RoutingClient()  # legacy lovelace/config → config_not_found
    get_dashboard = _build_get_dashboard(client)

    from fastmcp.exceptions import ToolError

    with patch_ws(ws, tools_config_dashboards), pytest.raises(ToolError):
        await get_dashboard(url_path="ghost")
    assert client.config_calls == ["ghost"]


class _ForceCapturingClient(RoutingClient):
    """Records the ``force`` flag on each ``lovelace/config`` read."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.config_force_flags: list[Any] = []

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("type") == "lovelace/config":
            self.config_force_flags.append(msg.get("force"))
        return await super().send_websocket_message(msg)


@pytest.mark.asyncio
async def test_get_force_reload_bypasses_component() -> None:
    """``force_reload=True`` skips the component ``get`` fast path entirely.

    The component ``get`` carries no force semantic, so a forced read must go
    straight to the legacy ``lovelace/config`` request with ``force=True`` to bust
    HA's Lovelace cache. Even though the component WOULD serve the body, it is never
    consulted (no dashboards frame at all — not even the caps probe).
    """
    ws = make_ws(
        "ha_mcp_tools/dashboards",
        info_result=_CAPS_DASHBOARDS,
        cmd_result={
            "mode": "get",
            "available": True,
            "status": "ok",
            "url_path": "home",
            "config": _HOME_BODY,  # component would serve this if consulted
        },
    )
    client = _ForceCapturingClient(configs={"home": _HOME_BODY})
    get_dashboard = _build_get_dashboard(client)

    with patch_ws(ws, tools_config_dashboards):
        resp = await get_dashboard(url_path="home", force_reload=True)

    assert resp["config"] == _HOME_BODY
    # The component was bypassed entirely; the legacy forced read served it.
    assert not _dash_calls(ws)
    assert client.config_calls == ["home"]
    assert client.config_force_flags == [True]


# --- cross-dashboard search ---------------------------------------------------
@pytest.mark.asyncio
async def test_search_served_via_component() -> None:
    """mode='search' served from the component ``search`` frame, no legacy reads."""
    matches = [
        {
            "url_path": "home",
            "title": "Home",
            "view_index": 0,
            "view_title": "Living",
            "card_path": "views[0].cards[0]",
            "card_type": "entities",
            "matched_field": "entities",
            "matched_value": "light.kitchen",
        }
    ]
    ws = make_ws(
        "ha_mcp_tools/dashboards",
        info_result=_CAPS_DASHBOARDS,
        cmd_result={
            "mode": "search",
            "available": True,
            "matches": matches,
            "truncated": False,
        },
    )
    client = RoutingClient()
    get_dashboard = _build_get_dashboard(client)

    with patch_ws(ws, tools_config_dashboards):
        resp = await get_dashboard(mode="search", query="light.kitchen")

    assert resp["action"] == "search_all"
    assert resp["query"] == "light.kitchen"
    assert resp["matches"] == matches
    assert resp["match_count"] == 1
    assert resp["truncated"] is False
    assert client.list_calls == 0
    assert client.config_calls == []
    assert _dash_calls(ws)[0].kwargs == {"mode": "search", "query": "light.kitchen"}


@pytest.mark.asyncio
async def test_search_capability_miss_uses_legacy_walk() -> None:
    """No capability → list + per-dashboard get + the same walk, server-side."""
    ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    client = RoutingClient(
        dashboards_list=[_STORAGE_ROW],
        configs={"home": _HOME_BODY},
    )
    get_dashboard = _build_get_dashboard(client)

    with patch_ws(ws, tools_config_dashboards):
        resp = await get_dashboard(mode="search", query="light.kitchen")

    assert resp["action"] == "search_all"
    assert resp["match_count"] == 1
    m = resp["matches"][0]
    assert m["url_path"] == "home"
    assert m["card_type"] == "entities"
    assert m["matched_field"] == "entities"
    assert m["matched_value"] == "light.kitchen"
    assert m["card_path"] == "views[0].cards[0]"
    # Legacy walk: one dashboards/list + one lovelace/config per storage dash.
    assert client.list_calls == 1
    assert client.config_calls == ["home"]
    assert not _dash_calls(ws)


@pytest.mark.asyncio
async def test_legacy_search_walk_skips_yaml_body() -> None:
    """The component-less search walk never reads a YAML dashboard's config body.

    HA resolves ``!secret`` when it loads a YAML Lovelace config, so fetching one
    for the walk could surface resolved secrets in a match. The walk must skip any
    row tagged ``mode == "yaml"`` WITHOUT a ``lovelace/config`` read (the same
    exclusion the component applies in-process).
    """
    ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    client = RoutingClient(
        dashboards_list=[_STORAGE_ROW, _YAML_ROW],
        configs={"home": _HOME_BODY},  # deliberately no body for the YAML dash
    )
    get_dashboard = _build_get_dashboard(client)

    with patch_ws(ws, tools_config_dashboards):
        resp = await get_dashboard(mode="search", query="light.kitchen")

    assert resp["action"] == "search_all"
    assert resp["match_count"] == 1
    assert resp["matches"][0]["url_path"] == "home"
    # Only the storage dashboard's body was read; the YAML row was skipped.
    assert client.config_calls == ["home"]
    assert "yaml-dash" not in client.config_calls


@pytest.mark.asyncio
async def test_legacy_search_walk_skips_untagged_row() -> None:
    """Fail-closed: a legacy row with NO ``mode`` key is skipped WITHOUT a body read.

    Core's schemas normally stamp ``mode`` on every ``lovelace/dashboards/list``
    row (storage items default ``mode: storage``; YAML entries require
    ``mode: yaml``), but a storage item persisted before core's mode default
    existed can surface untagged. An untagged row is not provably storage, and
    reading a YAML body resolves ``!secret`` to plaintext, so the walk reads a
    body ONLY when the row is EXPLICITLY tagged ``mode == "storage"`` — the
    untagged row's ``lovelace/config`` is never requested.
    """
    ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    untagged_row = {k: v for k, v in _STORAGE_ROW.items() if k != "mode"}
    client = RoutingClient(
        dashboards_list=[untagged_row],
        configs={"home": _HOME_BODY},  # a body the walk must NOT reach
    )
    get_dashboard = _build_get_dashboard(client)

    with patch_ws(ws, tools_config_dashboards):
        resp = await get_dashboard(mode="search", query="light.kitchen")

    assert resp["action"] == "search_all"
    assert resp["match_count"] == 0
    # The untagged row's body was NEVER requested (fail-closed).
    assert client.config_calls == []


@pytest.mark.asyncio
async def test_search_requires_query() -> None:
    """mode='search' with no query is a structured validation error (no WS)."""
    from fastmcp.exceptions import ToolError

    ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_DASHBOARDS)
    client = RoutingClient()
    get_dashboard = _build_get_dashboard(client)

    with patch_ws(ws, tools_config_dashboards), pytest.raises(ToolError):
        await get_dashboard(mode="search", query="   ")
    assert not _dash_calls(ws)
    assert client.list_calls == 0


# --- transport failure falls back to legacy ----------------------------------
@pytest.mark.asyncio
async def test_connection_error_falls_back_to_legacy() -> None:
    """A WS-down error on the dashboards frame returns ``None`` (legacy fallback),
    not a raise — the legacy dashboards path rides the never-raising bridge, so it
    does not die identically, and the auto-backup capture consumer must not be
    blocked from its write by an escaping transport error."""
    ws = make_ws(
        "ha_mcp_tools/dashboards",
        info_result=_CAPS_DASHBOARDS,
        cmd_exc=HomeAssistantConnectionError("ws down"),
    )
    client = RoutingClient()

    with patch_ws(ws, tools_config_dashboards):
        result = await tools_config_dashboards._dashboards_via_component(client, "list")

    assert result is None
    # A transient connection error is not a downgrade — caps stay cached.
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_ws_establish_failure_falls_back_to_legacy() -> None:
    """A plain establish ``Exception`` from ``get_websocket_client()`` (after caps
    are cached) returns ``None`` (legacy fallback), not a raise."""
    caps_ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_DASHBOARDS)
    client = RoutingClient()

    with patch_ws_establish_failure(
        caps_ws,
        tools_config_dashboards,
        HomeAssistantConnectionError("Failed to connect to Home Assistant WebSocket"),
    ):
        result = await tools_config_dashboards._dashboards_via_component(client, "list")

    assert result is None
    assert client in component_api._CAPS_CACHE


# --- shared existence check (set/delete funnel through fetch_dashboards_list) --
@pytest.mark.asyncio
async def test_existence_check_uses_component_list() -> None:
    """``_lookup_existing_dashboards`` resolves existence from the component list."""
    ws = make_ws(
        "ha_mcp_tools/dashboards",
        info_result=_CAPS_DASHBOARDS,
        cmd_result={
            "mode": "list",
            "available": True,
            "dashboards": [_STORAGE_ROW, _YAML_ROW],
        },
    )
    client = RoutingClient()
    tools = DashboardConfigTools(client)

    with patch_ws(ws, tools_config_dashboards):
        exists_home, rows = await tools._lookup_existing_dashboards("home", None)
        missing, _ = await tools._lookup_existing_dashboards("nope", None)
        # The built-in "lovelace" default is special-cased as always existing.
        builtin, _ = await tools._lookup_existing_dashboards("lovelace", None)

    assert exists_home is True
    # YAML rows are kept in the list (metadata), matching the legacy row set.
    assert rows == [_STORAGE_ROW, _YAML_ROW]
    assert missing is False
    assert builtin is True
    assert client.list_calls == 0


# --- MODE-2 single-dashboard search: storage-mode config guard ---------------
# The single-dashboard search (entity_id / card_type / heading) fetches the
# target dashboard's lovelace/config directly. With include_config=True the
# matched-card config bodies would surface — a YAML dashboard's body can carry
# HA-resolved !secret plaintext — so the guard withholds them unless the target
# is PROVABLY storage-mode (its dashboards-list row is tagged mode="storage").
_MODE2_BODY = {
    "title": "Home",
    "views": [
        {
            "title": "Living",
            "cards": [{"type": "entities", "entities": ["light.kitchen"]}],
        }
    ],
}


async def _run_mode2_search(client: Any, url_path: str) -> dict[str, Any]:
    """Run the single-dashboard search (include_config=True) over the legacy path."""
    legacy_ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    with patch_ws(legacy_ws, tools_config_dashboards):
        return await _build_get_dashboard(client)(
            url_path=url_path, entity_id="light.kitchen", include_config=True
        )


@pytest.mark.asyncio
async def test_mode2_search_storage_dashboard_surfaces_config() -> None:
    """A storage-mode dashboard surfaces matched-card config (include_config=True)."""
    client = RoutingClient(
        dashboards_list=[_STORAGE_ROW], configs={"home": _MODE2_BODY}
    )

    resp = await _run_mode2_search(client, "home")

    assert resp["match_count"] == 1
    assert "card_config" in resp["matches"][0]
    assert "warnings" not in resp
    # The storage-mode resolution paid one list read.
    assert client.list_calls == 1


@pytest.mark.asyncio
async def test_mode2_search_yaml_dashboard_suppresses_config() -> None:
    """A YAML dashboard's matched-card config is withheld (it may carry resolved
    !secret plaintext); the match location is still reported, with a note."""
    client = RoutingClient(
        dashboards_list=[_YAML_ROW], configs={"yaml-dash": _MODE2_BODY}
    )

    resp = await _run_mode2_search(client, "yaml-dash")

    assert resp["match_count"] == 1
    assert "card_config" not in resp["matches"][0]
    assert any("withheld" in w for w in resp.get("warnings", []))


@pytest.mark.asyncio
async def test_mode2_search_untagged_dashboard_suppresses_config() -> None:
    """A dashboard whose list row carries no mode tag is not provably storage, so
    its matched-card config is withheld (fail-closed)."""
    untagged_row = {"url_path": "untagged", "title": "Untagged"}
    client = RoutingClient(
        dashboards_list=[untagged_row], configs={"untagged": _MODE2_BODY}
    )

    resp = await _run_mode2_search(client, "untagged")

    assert resp["match_count"] == 1
    assert "card_config" not in resp["matches"][0]
    assert any("withheld" in w for w in resp.get("warnings", []))


@pytest.mark.asyncio
async def test_mode2_search_include_config_false_skips_storage_resolution() -> None:
    """include_config=False already strips card_config, so the storage-mode
    resolution (an extra list read) is skipped and no note is added."""
    client = RoutingClient(configs={"yaml-dash": _MODE2_BODY})  # no dashboards_list

    legacy_ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    with patch_ws(legacy_ws, tools_config_dashboards):
        resp = await _build_get_dashboard(client)(
            url_path="yaml-dash", entity_id="light.kitchen", include_config=False
        )

    assert resp["match_count"] == 1
    assert "card_config" not in resp["matches"][0]
    assert client.list_calls == 0
    assert "warnings" not in resp
