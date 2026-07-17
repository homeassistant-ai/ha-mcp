"""Cross-seam contract tests for the ``ha_mcp_tools/dashboards`` capability.

Modeled on ``test_component_readapi_contract.py`` (which parallel tasks also
edit, so this lives in its own file): the REAL component ``_dashboards_prep`` +
``_do_dashboards`` are driven against a fake lovelace ``hass`` underneath the
mocked WS transport, then the REAL dashboard tools consume the result — so a
vocabulary/shape drift on either side of the seam fails here rather than shipping
a mis-shaped response.

Covered seams: ``list`` parity (component storage-only rows == the legacy
``lovelace/dashboards/list`` shape), ``get`` parity (storage body + the default
dashboard via ``url_path=None``), a YAML dashboard's per-call fall back to the
legacy read, cross-dashboard ``search`` parity (the component's in-process walk
vs the server-side legacy walk over identical fixture configs), the set tool's
existence check, and the auto-backup capture read.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp import backup_manager
from ha_mcp.tools import component_api, tools_config_dashboards
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools

from ._component_routing_helpers import make_ws, patch_ws
from .test_component_ws_phase2_async import (
    FakeDashboard,
    FakeLovelaceData,
    _storage_dash,
)
from .test_component_ws_search import FakeHass, wsapi
from .test_ha_dashboards_component_routing import RoutingClient, _build_get_dashboard

_CAPS_NONE = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": [],
    "limits": {},
}

# The legacy ``lovelace/dashboards/list`` storage item shape (real HA stores
# ``mode`` alongside the metadata). The component's ``_dashboard_list_rows``
# reproduces exactly this for a storage dashboard, so the consumer's storage
# filter must leave the two paths' row sets identical.
_HOME_ROW = {
    "id": "id-home",
    "url_path": "home",
    "title": "Home",
    "icon": "mdi:home",
    "show_in_sidebar": True,
    "require_admin": False,
    "mode": "storage",
}

_HOME_BODY = {
    "title": "Home",
    "views": [
        {
            "title": "Living",
            "cards": [
                {"type": "entities", "entities": ["light.kitchen", "light.hall"]},
                {
                    "type": "vertical-stack",
                    "cards": [
                        {"type": "camera", "camera_image": "camera.front_door"}
                    ],
                },
            ],
            "sections": [
                {"cards": [{"type": "markdown", "content": "kitchen notes"}]}
            ],
        }
    ],
}
_OFFICE_BODY = {
    "title": "Office",
    "views": [{"title": "Desk", "cards": [{"type": "light", "entity": "light.desk"}]}],
}
_DEFAULT_BODY = {"views": [{"title": "Overview", "cards": []}]}
_YAML_BODY = {"views": [{"cards": [{"type": "markdown", "content": "yaml secret"}]}]}


def _real_component_ws(hass: FakeHass) -> AsyncMock:
    """A WS mock whose ``dashboards`` command is served by the REAL component.

    ``info`` returns the real ``_do_info()`` (advertising ``dashboards``); the
    ``dashboards`` command runs the real async ``_dashboards_prep`` then the real
    pure ``_do_dashboards`` against ``hass`` — the seam under test is everything
    between that return value and the tool response.
    """
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": wsapi._do_info(hass)}
        assert command_type == "ha_mcp_tools/dashboards"
        params = dict(kwargs)
        prep_out = await wsapi._dashboards_prep(hass, params)
        return {"success": True, "result": wsapi._do_dashboards(hass, params, **prep_out)}

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


def _component_hass(dashboards_map: dict[Any, Any]) -> FakeHass:
    """A ``FakeHass`` whose ``hass.data['lovelace']`` carries the dashboards map."""
    return FakeHass(data={"lovelace": FakeLovelaceData(dashboards_map)})


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    component_api._NEGATIVE_CACHE_TS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    component_api._NEGATIVE_CACHE_TS.clear()


# --- list parity --------------------------------------------------------------
@pytest.mark.asyncio
async def test_list_parity_storage_only() -> None:
    """Component list (storage rows only, YAML filtered) == the legacy list shape."""
    hass = _component_hass(
        {
            "home": _storage_dash("home", "Home"),
            "yaml-dash": FakeDashboard(
                "yaml-dash", "yaml", config={"url_path": "yaml-dash", "title": "Y"}
            ),
        }
    )
    ws = _real_component_ws(hass)
    comp_client = RoutingClient()
    with patch_ws(ws, tools_config_dashboards):
        comp_resp = await _build_get_dashboard(comp_client)(list_only=True)

    # Legacy path: the component is absent, so the storage-only legacy list runs.
    legacy_ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    legacy_client = RoutingClient(dashboards_list=[_HOME_ROW])
    with patch_ws(legacy_ws, tools_config_dashboards):
        legacy_resp = await _build_get_dashboard(legacy_client)(list_only=True)

    assert comp_resp["dashboards"] == [_HOME_ROW]
    assert comp_resp["dashboards"] == legacy_resp["dashboards"]
    assert comp_client.list_calls == 0  # served in-process
    assert legacy_client.list_calls == 1


@pytest.mark.asyncio
async def test_list_icon_less_dashboard_shape_diverges() -> None:
    """An icon-less dashboard's row shape diverges by key presence, not value.

    The component builds each row as ``{key: meta.get(key) ...}``
    (``websocket_api.py:_dashboard_list_rows``), so a dashboard whose stored
    config never set an ``icon`` still carries the key with value ``None``.
    Real HA's ``lovelace/dashboards/list`` response for the same dashboard
    omits a never-set key entirely. This pins that documented, functionally
    harmless divergence (``row.get("icon")`` is ``None`` on both paths) rather
    than forcing src to manufacture parity.
    """
    hass = _component_hass(
        {
            "bare": FakeDashboard(
                "bare",
                "storage",
                config={
                    "id": "id-bare",
                    "url_path": "bare",
                    "title": "Bare",
                    "show_in_sidebar": True,
                    "require_admin": False,
                },  # no "icon" key at all
            )
        }
    )
    ws = _real_component_ws(hass)
    comp_client = RoutingClient()
    with patch_ws(ws, tools_config_dashboards):
        comp_resp = await _build_get_dashboard(comp_client)(list_only=True)

    comp_row = comp_resp["dashboards"][0]
    assert "icon" in comp_row
    assert comp_row["icon"] is None

    # Legacy: the real HA lovelace/dashboards/list response for a dashboard
    # that never had an icon set simply omits the key.
    bare_legacy_row = {
        "id": "id-bare",
        "url_path": "bare",
        "title": "Bare",
        "show_in_sidebar": True,
        "require_admin": False,
        "mode": "storage",
    }
    legacy_ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    legacy_client = RoutingClient(dashboards_list=[bare_legacy_row])
    with patch_ws(legacy_ws, tools_config_dashboards):
        legacy_resp = await _build_get_dashboard(legacy_client)(list_only=True)

    legacy_row = legacy_resp["dashboards"][0]
    assert "icon" not in legacy_row

    # The rows diverge by key presence...
    assert comp_row != legacy_row
    # ...but agree on the effective value either way.
    assert comp_row.get("icon") == legacy_row.get("icon") is None


# --- get parity ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_parity_storage_body() -> None:
    """Component get body == the legacy lovelace/config body for a storage dash."""
    hass = _component_hass({"home": _storage_dash("home", "Home", body=_HOME_BODY)})
    ws = _real_component_ws(hass)
    comp_client = RoutingClient()
    with patch_ws(ws, tools_config_dashboards):
        comp_resp = await _build_get_dashboard(comp_client)(url_path="home")

    legacy_ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    legacy_client = RoutingClient(configs={"home": _HOME_BODY})
    with patch_ws(legacy_ws, tools_config_dashboards):
        legacy_resp = await _build_get_dashboard(legacy_client)(url_path="home")

    assert comp_resp["config"] == _HOME_BODY
    assert comp_resp["config"] == legacy_resp["config"]
    assert comp_resp["config_hash"] == legacy_resp["config_hash"]
    assert comp_client.config_calls == []  # served in-process


@pytest.mark.asyncio
async def test_get_parity_default_dashboard() -> None:
    """The default dashboard (component ``None`` key) is served via url_path=None."""
    hass = _component_hass(
        {None: _storage_dash("lovelace", "Default", body=_DEFAULT_BODY)}
    )
    ws = _real_component_ws(hass)
    comp_client = RoutingClient()
    with patch_ws(ws, tools_config_dashboards):
        comp_resp = await _build_get_dashboard(comp_client)()

    legacy_ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    legacy_client = RoutingClient(configs={None: _DEFAULT_BODY})
    with patch_ws(legacy_ws, tools_config_dashboards):
        legacy_resp = await _build_get_dashboard(legacy_client)()

    assert comp_resp["config"] == _DEFAULT_BODY
    assert comp_resp["config"] == legacy_resp["config"]
    # The component was consulted with no url_path (the default).
    assert comp_client.config_calls == []


@pytest.mark.asyncio
async def test_get_yaml_dashboard_falls_back_to_legacy() -> None:
    """A YAML dashboard's real ``yaml_excluded`` get drops to the legacy read."""
    hass = _component_hass(
        {
            "yaml-dash": FakeDashboard(
                "yaml-dash",
                "yaml",
                config={"url_path": "yaml-dash"},
                body=_YAML_BODY,
            )
        }
    )
    ws = _real_component_ws(hass)
    # Same client serves the legacy fallback read for the YAML body.
    client = RoutingClient(configs={"yaml-dash": _YAML_BODY})
    with patch_ws(ws, tools_config_dashboards):
        resp = await _build_get_dashboard(client)(url_path="yaml-dash")

    assert resp["config"] == _YAML_BODY  # legacy read served it
    assert client.config_calls == ["yaml-dash"]
    # The component WAS consulted first (one dashboards frame), then refused.
    dash_frames = [
        c for c in ws.send_command.call_args_list if c.args[0] == "ha_mcp_tools/dashboards"
    ]
    assert len(dash_frames) == 1


# --- cross-dashboard search parity -------------------------------------------
@pytest.mark.asyncio
async def test_search_parity_component_vs_legacy_walk() -> None:
    """The component's in-process walk and the server-side legacy walk return the
    SAME matches over identical fixture configs."""
    dmap = {
        "home": _storage_dash("home", "Home", body=_HOME_BODY),
        "office": _storage_dash("office", "Office", body=_OFFICE_BODY),
    }

    # Component path: the real _do_dashboards search over the fake lovelace hass.
    hass = _component_hass(dmap)
    ws = _real_component_ws(hass)
    comp_client = RoutingClient()
    with patch_ws(ws, tools_config_dashboards):
        comp_resp = await _build_get_dashboard(comp_client)(
            mode="search", query="light"
        )

    # Legacy path: list + per-dashboard get + the same walk, server-side.
    legacy_ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    legacy_client = RoutingClient(
        dashboards_list=[
            {**_storage_dash("home", "Home").config, "mode": "storage"},
            {**_storage_dash("office", "Office").config, "mode": "storage"},
        ],
        configs={"home": _HOME_BODY, "office": _OFFICE_BODY},
    )
    with patch_ws(legacy_ws, tools_config_dashboards):
        legacy_resp = await _build_get_dashboard(legacy_client)(
            mode="search", query="light"
        )

    assert comp_resp["matches"]  # non-empty (light.kitchen / light.hall / light.desk)
    assert comp_resp["matches"] == legacy_resp["matches"]
    assert comp_resp["truncated"] == legacy_resp["truncated"]
    # Legacy paid the N+1 cost; the component served one frame.
    assert comp_client.list_calls == 0 and comp_client.config_calls == []
    assert legacy_client.list_calls == 1
    assert sorted(legacy_client.config_calls) == ["home", "office"]


@pytest.mark.asyncio
async def test_search_parity_case_insensitive_query() -> None:
    """An uppercase query still matches lowercase card content on both paths."""
    dmap = {
        "home": _storage_dash("home", "Home", body=_HOME_BODY),
        "office": _storage_dash("office", "Office", body=_OFFICE_BODY),
    }

    hass = _component_hass(dmap)
    ws = _real_component_ws(hass)
    comp_client = RoutingClient()
    with patch_ws(ws, tools_config_dashboards):
        comp_resp = await _build_get_dashboard(comp_client)(mode="search", query="LIGHT")

    legacy_ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    legacy_client = RoutingClient(
        dashboards_list=[
            {**_storage_dash("home", "Home").config, "mode": "storage"},
            {**_storage_dash("office", "Office").config, "mode": "storage"},
        ],
        configs={"home": _HOME_BODY, "office": _OFFICE_BODY},
    )
    with patch_ws(legacy_ws, tools_config_dashboards):
        legacy_resp = await _build_get_dashboard(legacy_client)(mode="search", query="LIGHT")

    assert comp_resp["matches"]  # uppercase query still hits lowercase content
    assert comp_resp["matches"] == legacy_resp["matches"]


@pytest.mark.asyncio
async def test_search_parity_truncation_cap() -> None:
    """>200 matches truncate identically on both paths (mirrors the component cap)."""
    cap = tools_config_dashboards._SEARCH_ALL_MATCH_CAP
    entities = [f"light.e{i}" for i in range(cap + 25)]
    body = {"views": [{"cards": [{"type": "entities", "entities": entities}]}]}
    dmap = {"home": _storage_dash("home", "Home", body=body)}

    hass = _component_hass(dmap)
    ws = _real_component_ws(hass)
    comp_client = RoutingClient()
    with patch_ws(ws, tools_config_dashboards):
        comp_resp = await _build_get_dashboard(comp_client)(mode="search", query="light.e")

    legacy_ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    legacy_client = RoutingClient(
        dashboards_list=[{**_storage_dash("home", "Home").config, "mode": "storage"}],
        configs={"home": body},
    )
    with patch_ws(legacy_ws, tools_config_dashboards):
        legacy_resp = await _build_get_dashboard(legacy_client)(mode="search", query="light.e")

    assert comp_resp["truncated"] is True
    assert legacy_resp["truncated"] is True
    assert len(comp_resp["matches"]) == cap
    assert comp_resp["matches"] == legacy_resp["matches"]


@pytest.mark.asyncio
async def test_search_default_dashboard_asymmetry() -> None:
    """Documented asymmetry: the component walk covers the default (None-keyed)
    dashboard; the component-less legacy walk excludes it, because
    ``fetch_dashboards_list`` never returns the default so it is never fetched
    for the server-side walk (see MODE 4 docstring caveat on
    ``ha_config_get_dashboard``)."""
    default_body = {
        "views": [{"cards": [{"type": "entities", "entities": ["light.default_only"]}]}]
    }
    dmap = {
        None: _storage_dash("lovelace", "Default", body=default_body),
        "home": _storage_dash("home", "Home", body=_HOME_BODY),
    }

    hass = _component_hass(dmap)
    ws = _real_component_ws(hass)
    comp_client = RoutingClient()
    with patch_ws(ws, tools_config_dashboards):
        comp_resp = await _build_get_dashboard(comp_client)(
            mode="search", query="light.default_only"
        )

    # Legacy: fetch_dashboards_list never returns the default (None key), so
    # its dashboard is never fetched for the walk.
    legacy_ws = make_ws("ha_mcp_tools/dashboards", info_result=_CAPS_NONE)
    legacy_client = RoutingClient(
        dashboards_list=[{**_storage_dash("home", "Home").config, "mode": "storage"}],
        configs={"home": _HOME_BODY},
    )
    with patch_ws(legacy_ws, tools_config_dashboards):
        legacy_resp = await _build_get_dashboard(legacy_client)(
            mode="search", query="light.default_only"
        )

    assert comp_resp["matches"]
    assert comp_resp["matches"][0]["url_path"] is None  # the default's own key
    assert legacy_resp["matches"] == []  # documented exclusion


# --- set tool existence check -------------------------------------------------
@pytest.mark.asyncio
async def test_set_existence_check_via_real_component() -> None:
    """The set tool's existence check resolves against the real component list."""
    hass = _component_hass(
        {
            "home": _storage_dash("home", "Home"),
            "yaml-dash": FakeDashboard(
                "yaml-dash", "yaml", config={"url_path": "yaml-dash"}
            ),
        }
    )
    ws = _real_component_ws(hass)
    tools = DashboardConfigTools(RoutingClient())
    with patch_ws(ws, tools_config_dashboards):
        exists_home, rows = await tools._lookup_existing_dashboards("home", None)
        missing, _ = await tools._lookup_existing_dashboards("ghost", None)
        builtin, _ = await tools._lookup_existing_dashboards("lovelace", None)

    assert exists_home is True
    assert rows == [_HOME_ROW]  # YAML filtered before the existence scan
    assert missing is False
    assert builtin is True  # the built-in default is special-cased


# --- auto-backup capture read -------------------------------------------------
@pytest.mark.asyncio
async def test_backup_capture_via_real_component() -> None:
    """The auto-backup capture reads the body through the real component get."""
    hass = _component_hass({"home": _storage_dash("home", "Home", body=_HOME_BODY)})
    ws = _real_component_ws(hass)
    client = RoutingClient()
    with patch_ws(ws, tools_config_dashboards):
        captured = await backup_manager._fetch_dashboard(client, "home")

    assert captured == _HOME_BODY
    assert client.config_calls == []  # served in-process


@pytest.mark.asyncio
async def test_backup_capture_yaml_falls_back_to_legacy() -> None:
    """A YAML dashboard capture drops to the legacy read (component refuses body)."""
    hass = _component_hass(
        {
            "yaml-dash": FakeDashboard(
                "yaml-dash", "yaml", config={"url_path": "yaml-dash"}, body=_YAML_BODY
            )
        }
    )
    ws = _real_component_ws(hass)
    # dashboards/list (resolve) returns no storage rows → capture uses the id as-is;
    # the legacy lovelace/config read serves the YAML body.
    client = RoutingClient(configs={"yaml-dash": _YAML_BODY})
    with patch_ws(ws, tools_config_dashboards):
        captured = await backup_manager._fetch_dashboard(client, "yaml-dash")

    assert captured == _YAML_BODY
    assert client.config_calls == ["yaml-dash"]
