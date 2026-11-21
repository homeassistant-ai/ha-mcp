"""Render paths listed for a dashboard's views.

Every dashboard config is saved through Core and read back from it, as the
dashboard tools hand it to ``dashboard_render_paths``.
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.dashboard_screenshot.paths import (
    DashboardRenderTarget,
    dashboard_render_paths,
)

from ._dashboard_screenshot_paths_core import named_target, raw_target, stored_config

FRAGILE_INDEXES = (
    "dashboard view(s) have no usable stable path; their render paths "
    "use fragile numeric indexes."
)


def test_non_stable_target_without_warnings_is_rejected() -> None:
    with pytest.raises(ValueError, match="must carry at least one warning"):
        DashboardRenderTarget(
            url_path="x",
            view_path=None,
            render_path="x",
            view_index=None,
            stable=False,
        )


def test_stable_target_without_warnings_is_allowed() -> None:
    target = DashboardRenderTarget(
        url_path="x",
        view_path=None,
        render_path="x",
        view_index=None,
        stable=True,
    )
    assert target.warnings == ()


@pytest.mark.parametrize(
    ("url_path", "stored_as", "base_path"),
    [
        (None, None, "lovelace"),
        ("default", None, "lovelace"),
        ("wall-panel", "wall-panel", "wall-panel"),
    ],
)
async def test_render_paths_return_canonical_metadata_without_mutating_config(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    url_path: str | None,
    stored_as: str | None,
    base_path: str,
) -> None:
    config = await stored_config(
        hass,
        core_client,
        {
            "title": "House",
            "views": [
                {"title": "Home", "path": "home", "cards": [{"type": "clock"}]},
                {"title": "Lights", "path": "lights"},
            ],
        },
        url_path=stored_as,
    )
    original = deepcopy(config)

    paths, warnings = dashboard_render_paths(url_path, config)

    assert paths == [
        {
            "url_path": base_path,
            "view_index": 0,
            "view_path": "home",
            "title": "Home",
            "render_path": f"{base_path}/home",
            "stable": True,
        },
        {
            "url_path": base_path,
            "view_index": 1,
            "view_path": "lights",
            "title": "Lights",
            "render_path": f"{base_path}/lights",
            "stable": True,
        },
    ]
    assert warnings == []
    assert config == original


async def test_render_paths_report_numeric_fallbacks(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    config = await stored_config(
        hass,
        core_client,
        {
            "views": [
                {"title": "Named", "path": "home"},
                {"title": "Missing"},
                {"title": "Blank", "path": "   "},
                "invalid view shape",
            ]
        },
    )

    paths, warnings = dashboard_render_paths("wall-panel", config)

    assert paths == [
        {
            "url_path": "wall-panel",
            "view_index": 0,
            "view_path": "home",
            "title": "Named",
            "render_path": "wall-panel/home",
            "stable": True,
        },
        {
            "url_path": "wall-panel",
            "view_index": 1,
            "view_path": None,
            "title": "Missing",
            "render_path": "wall-panel/1",
            "stable": False,
        },
        {
            "url_path": "wall-panel",
            "view_index": 2,
            "view_path": None,
            "title": "Blank",
            "render_path": "wall-panel/2",
            "stable": False,
        },
        {
            "url_path": "wall-panel",
            "view_index": 3,
            "view_path": None,
            "title": None,
            "render_path": "wall-panel/3",
            "stable": False,
        },
    ]
    assert warnings == [f"3 {FRAGILE_INDEXES}"]


async def test_render_paths_use_numeric_fallback_for_unsafe_configured_view_path(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    config = await stored_config(
        hass, core_client, {"views": [{"title": "Unsafe", "path": "../config"}]}
    )

    paths, warnings = dashboard_render_paths("wall-panel", config)

    assert paths == [
        {
            "url_path": "wall-panel",
            "view_index": 0,
            "view_path": "../config",
            "title": "Unsafe",
            "render_path": "wall-panel/0",
            "stable": False,
            "invalid_path": True,
        }
    ]
    assert warnings == [f"1 {FRAGILE_INDEXES}"]


async def test_render_paths_use_numeric_fallback_for_duplicate_paths(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    config = await stored_config(
        hass,
        core_client,
        {
            "views": [
                {"title": "First", "path": "home"},
                {"title": "Second", "path": "home"},
            ]
        },
    )

    paths, warnings = dashboard_render_paths("wall-panel", config)

    assert [path["render_path"] for path in paths] == [
        "wall-panel/0",
        "wall-panel/1",
    ]
    assert all(path["stable"] is False for path in paths)
    assert all(path["ambiguous_path"] is True for path in paths)
    assert any("duplicate" in warning for warning in warnings)


async def test_render_paths_describe_strategy_dashboard_base_route(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    config = await stored_config(
        hass, core_client, {"strategy": {"type": "original-states"}}
    )

    paths, warnings = dashboard_render_paths("wall-panel", config)

    assert paths == [
        {
            "url_path": "wall-panel",
            "view_index": None,
            "view_path": None,
            "title": None,
            "render_path": "wall-panel",
            "stable": False,
            "strategy_dashboard": True,
        }
    ]
    assert warnings == [
        "Strategy dashboard views are generated at runtime; only the dashboard "
        "base render path is available."
    ]


@pytest.mark.parametrize(
    "configured_path", ["floor/second", "hass-unused-entities", ".", " home "]
)
async def test_unusable_configured_paths_use_verified_numeric_fallback(
    hass: HomeAssistant, core_client: HomeAssistantClient, configured_path: str
) -> None:
    config = await stored_config(
        hass, core_client, {"views": [{"path": configured_path}]}
    )

    paths, warnings = dashboard_render_paths("wall-panel", config)

    assert paths[0]["view_path"] == configured_path
    assert paths[0]["render_path"] == "wall-panel/0"
    assert paths[0]["stable"] is False
    assert paths[0]["invalid_path"] is True
    assert warnings


async def test_decode_uri_reserved_view_path_uses_numeric_fallback(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The frontend's decodeURI keeps "&" encoded, so a named route would miss
    the view."""
    config = await stored_config(
        hass,
        core_client,
        {
            "views": [
                {"title": "Home", "path": "home"},
                {"title": "Heat", "path": "heat&zone"},
            ]
        },
    )

    paths, warnings = dashboard_render_paths("wall-panel", config)
    target = await named_target(core_client, "wall-panel", "heat&zone")

    assert paths[1]["view_path"] == "heat&zone"
    assert paths[1]["view_index"] == 1
    assert paths[1]["render_path"] == "wall-panel/1"
    assert paths[1]["stable"] is False
    assert paths[1]["invalid_path"] is True
    assert target.render_path == "wall-panel/1"
    assert target.view_index == 1
    assert target.stable is False
    assert "numeric fallback" in target.warnings[0]
    assert warnings


async def test_shadowed_numeric_fallback_uses_equivalent_unshadowed_suffix(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """View 1's index route "1" selects view 0, whose path is "1"."""
    config = await stored_config(
        hass,
        core_client,
        {"views": [{"path": "1"}, {"path": "floor/second"}]},
    )

    paths, _ = dashboard_render_paths("wall-panel", config)
    target = await named_target(core_client, "wall-panel", "floor/second")
    raw = await raw_target(core_client, "wall-panel/1.0")

    assert paths[1]["render_path"] == "wall-panel/1.0"
    assert target.render_path == "wall-panel/1.0"
    assert raw.view_index == 1
