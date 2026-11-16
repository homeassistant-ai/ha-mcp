"""Dashboards saved through Core for the screenshot render-path tests."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.dashboard_screenshot.paths import (
    DashboardRenderTarget,
    resolve_dashboard_render_target,
)

from ._config_dir_yaml import write_config_files


def _addressed(url_path: str | None) -> dict[str, Any]:
    return {} if url_path is None else {"url_path": url_path}


async def save_dashboard(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    config: dict[str, Any] | str,
    *,
    url_path: str | None = "wall-panel",
) -> None:
    """Save ``config`` through Core as dashboard ``url_path`` (``None``: the
    default dashboard)."""
    if "lovelace" not in hass.config.components:
        assert await async_setup_component(hass, "lovelace", {})
    if url_path is not None:
        created = await client.send_websocket_message(
            {
                "type": "lovelace/dashboards/create",
                "url_path": url_path,
                "title": "Wall",
            }
        )
        assert created["success"], created
    saved = await client.send_websocket_message(
        {"type": "lovelace/config/save", "config": config, **_addressed(url_path)}
    )
    assert saved["success"], saved


async def stored_config(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    config: dict[str, Any],
    *,
    url_path: str | None = "wall-panel",
) -> dict[str, Any]:
    """``config`` saved through Core, as Core gives it back."""
    await save_dashboard(hass, client, config, url_path=url_path)
    loaded = await client.send_websocket_message(
        {"type": "lovelace/config", "force": True, **_addressed(url_path)}
    )
    assert loaded["success"], loaded
    result: dict[str, Any] = loaded["result"]
    return result


async def broken_default_dashboard(hass: HomeAssistant) -> None:
    """The default dashboard in YAML, from a file that holds no mapping, so
    Core fails to load it."""
    await write_config_files(hass, {"broken-dashboard.yaml": "- not a dashboard\n"})
    dashboard = {"mode": "yaml", "filename": "broken-dashboard.yaml", "title": "Home"}
    assert await async_setup_component(
        hass, "lovelace", {"lovelace": {"dashboards": {"lovelace": dashboard}}}
    )


async def raw_target(
    client: HomeAssistantClient, dashboard_path: str
) -> DashboardRenderTarget:
    """The screenshot target of a raw frontend route."""
    return await resolve_dashboard_render_target(
        client, dashboard_path=dashboard_path, url_path=None, view_path=None
    )


async def named_target(
    client: HomeAssistantClient, url_path: str, view_path: str | None
) -> DashboardRenderTarget:
    """The screenshot target of a dashboard's url_path and view_path."""
    return await resolve_dashboard_render_target(
        client, dashboard_path=None, url_path=url_path, view_path=view_path
    )
