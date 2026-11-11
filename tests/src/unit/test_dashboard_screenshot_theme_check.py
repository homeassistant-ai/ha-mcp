"""A screenshot's theme is checked against the themes Home Assistant has."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import ClientSessionGenerator

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.dashboard_screenshot import capture
from ha_mcp.dashboard_screenshot.provision import EngineTarget

_IMAGE = b"\x89PNG\r\n\x1a\ntheme-check"

# Themes with both modes, no modes, and only a dark mode.
_THEMES = {
    "Midnight": {"modes": {"light": {}, "dark": {}}},
    "Flat Blue": {"primary-color": "#5294e2"},
    "Night Only": {"modes": {"dark": {}}},
}


async def _load_themes(hass: HomeAssistant, repo: Path) -> None:
    """Core's frontend with ``_THEMES`` configured, served from an empty repo."""
    assert await async_setup_component(
        hass,
        "frontend",
        {"frontend": {"development_repo": str(repo), "themes": _THEMES}},
    )


@pytest.fixture
async def engine(
    monkeypatch: pytest.MonkeyPatch,
    aiohttp_client: ClientSessionGenerator,
    socket_enabled: None,
) -> list[str]:
    """A screenshot engine on loopback that renders every path as ``_IMAGE``.

    Returns one entry per time the capture looked the engine up.
    """
    asked: list[str] = []

    async def render(request: web.Request) -> web.Response:
        return web.Response(body=_IMAGE, content_type="image/png")

    app = web.Application()
    app.router.add_get("/{path:.*}", render)
    server = await aiohttp_client(app)
    url = str(server.make_url("")).rstrip("/")

    async def resolve() -> EngineTarget:
        asked.append("engine")
        return EngineTarget(url=url)

    monkeypatch.setattr(capture, "resolve_engine", resolve)
    return asked


async def test_a_theme_home_assistant_does_not_have_is_refused_before_rendering(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    engine: list[str],
    tmp_path: Path,
) -> None:
    """A misspelled theme otherwise renders the default theme with no warning."""
    await _load_themes(hass, tmp_path)

    with (
        patch.object(
            core_client,
            "send_websocket_message",
            wraps=core_client.send_websocket_message,
        ) as spy,
        pytest.raises(ToolError) as exc_info,
    ):
        await capture.capture_dashboard_images(
            "lovelace/0", theme="Midnite", client=core_client
        )

    body = json.loads(str(exc_info.value))
    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert {"Midnight", "Flat Blue"} <= set(body["installed_themes"])
    spy.assert_awaited_once_with({"type": "frontend/get_themes"})
    assert engine == []


@pytest.mark.parametrize("theme", ["Midnight", "default"])
async def test_installed_and_built_in_themes_render(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    engine: list[str],
    tmp_path: Path,
    theme: str,
) -> None:
    await _load_themes(hass, tmp_path)
    warnings: list[str] = []
    captures = await capture.capture_dashboard_images(
        "lovelace/0",
        theme=theme,
        dark_mode=True,
        client=core_client,
        capture_warnings=warnings,
    )

    assert captures[0].data == _IMAGE
    assert not any(theme in w for w in warnings)


@pytest.mark.parametrize(
    ("theme", "dark_mode", "rendered"),
    [("Flat Blue", True, "no dark variant"), ("Night Only", False, "no light variant")],
    ids=["no-dark-mode", "dark-only"],
)
async def test_a_mode_the_theme_does_not_define_is_reported(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    engine: list[str],
    tmp_path: Path,
    theme: str,
    dark_mode: bool,
    rendered: str,
) -> None:
    """dark_mode naming a mode the theme does not define is reported."""
    await _load_themes(hass, tmp_path)
    warnings: list[str] = []
    await capture.capture_dashboard_images(
        "lovelace/0",
        theme=theme,
        dark_mode=dark_mode,
        client=core_client,
        capture_warnings=warnings,
    )

    assert any(theme in w and rendered in w for w in warnings)


@pytest.mark.parametrize("failure", ["transport-failure", "core-refusal"])
async def test_an_unreadable_theme_list_still_renders_and_says_why(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    engine: list[str],
    tmp_path: Path,
    failure: str,
) -> None:
    """The theme pre-check is optional; its failure must not fail the render.

    Core without the frontend refuses frontend/get_themes.
    """
    warnings: list[str] = []
    if failure == "core-refusal":
        reason = "Unknown command"
        captures = await capture.capture_dashboard_images(
            "lovelace/0",
            theme="Midnight",
            client=core_client,
            capture_warnings=warnings,
        )
    else:
        reason = "websocket closed"
        await _load_themes(hass, tmp_path)
        with patch.object(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("websocket closed")),
        ):
            captures = await capture.capture_dashboard_images(
                "lovelace/0",
                theme="Midnight",
                client=core_client,
                capture_warnings=warnings,
            )

    assert captures[0].data == _IMAGE
    assert any("not checked" in w and reason in w for w in warnings)
