"""Screenshot targets for a dashboard Home Assistant holds no config for (#2632).

Core answers "No config found." for it and the frontend renders its generated
default view, so the screenshot renders that view instead of calling the
dashboard missing.
"""

from __future__ import annotations

import json

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.dashboard_screenshot.paths import resolve_dashboard_render_target


@pytest.fixture
async def dashboard_without_config(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A storage dashboard "wall-panel" that Core holds no config for."""
    assert await async_setup_component(hass, "lovelace", {})
    created = await core_client.send_websocket_message(
        {
            "type": "lovelace/dashboards/create",
            "url_path": "wall-panel",
            "title": "Wall panel",
        }
    )
    assert created["success"] is True
    no_config = await core_client.send_websocket_message(
        {"type": "lovelace/config", "force": True, "url_path": "wall-panel"}
    )
    assert no_config["success"] is False
    assert no_config["error_code"] == "config_not_found"


async def test_dashboard_without_config_renders_its_default_view(
    core_client: HomeAssistantClient, dashboard_without_config: None
) -> None:
    target = await resolve_dashboard_render_target(
        core_client,
        dashboard_path=None,
        url_path="wall-panel",
        view_path=None,
    )

    assert target.render_path == "wall-panel"
    assert target.stable is False
    assert target.warnings


async def test_view_path_on_dashboard_without_config_is_refused(
    core_client: HomeAssistantClient, dashboard_without_config: None
) -> None:
    """There are no static views to resolve, so view_path is the caller's error."""
    with pytest.raises(ToolError) as exc_info:
        await resolve_dashboard_render_target(
            core_client,
            dashboard_path=None,
            url_path="wall-panel",
            view_path="home",
        )

    assert (
        json.loads(str(exc_info.value))["error"]["code"]
        == "VALIDATION_INVALID_PARAMETER"
    )


async def test_raw_route_of_dashboard_without_config_keeps_its_root(
    core_client: HomeAssistantClient, dashboard_without_config: None
) -> None:
    target = await resolve_dashboard_render_target(
        core_client,
        dashboard_path="wall-panel/0",
        url_path=None,
        view_path=None,
    )

    assert target.url_path == "wall-panel"
    assert target.render_path == "wall-panel/0"
