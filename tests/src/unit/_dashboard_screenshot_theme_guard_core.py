"""Saved themes on a real Core, and the engine and Supervisor around them.

Core's ``frontend`` keeps each user's saved theme (``frontend/get_user_data``,
key ``theme``). The screenshot engine signs in as a Core user of its own; a
Puppet render saves that user's theme as light. The engine and the Supervisor
listen on local HTTP servers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from homeassistant.auth.models import User
from homeassistant.components.frontend.storage import async_user_store
from homeassistant.components.websocket_api.const import (
    SIGNAL_WEBSOCKET_CONNECTED,
    SIGNAL_WEBSOCKET_DISCONNECTED,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.setup import async_setup_component

from ha_mcp.dashboard_screenshot.theme_guard import (
    THEME_USER_DATA_KEY,
    EngineCredential,
    ThemeGuard,
)

from ._dashboard_screenshot_common import PNG, _puppet_info, set_settings
from ._tools_addons_core import Supervisor, installed_apps

DARK = {"theme": "default", "dark": True}

# What Puppet's cold-browser render saves: an explicit light default theme.
CLOBBERED = {"theme": "", "dark": False}


@dataclass(frozen=True)
class Account:
    """A Core user, and the URL and token that sign in as it."""

    user: User
    credential: EngineCredential


async def set_up_frontend(hass: HomeAssistant, tmp_path: Path) -> None:
    """Core's frontend, which keeps every user's saved theme. The development
    repo keeps it off the frontend package, whose files no test asks for."""
    config = {"frontend": {"development_repo": str(tmp_path)}}
    assert await async_setup_component(hass, "frontend", config)


async def saved_theme(hass: HomeAssistant, account: Account) -> Any:
    """The theme Core keeps for ``account``."""
    return (await async_user_store(hass, account.user.id)).data.get(THEME_USER_DATA_KEY)


async def save_theme(hass: HomeAssistant, account: Account, value: Any) -> None:
    """``account``'s browser session saves ``value`` as its theme."""
    store = await async_user_store(hass, account.user.id)
    await store.async_set_item(THEME_USER_DATA_KEY, value)


async def render_report(
    hass: HomeAssistant, guard: ThemeGuard, account: Account
) -> str:
    """What the guard reports when a render flips ``account`` from dark."""
    await save_theme(hass, account, DARK)
    await guard.take_snapshot()
    await save_theme(hass, account, CLOBBERED)
    await guard.detect_change()
    assert len(guard.warnings) == 1, guard.warnings
    return guard.warnings[0]


class Sessions:
    """Core's count of WebSocket sessions that signed in, and that ended."""

    def __init__(self) -> None:
        self.opened = 0
        self.closed = 0


@contextmanager
def core_sessions(hass: HomeAssistant) -> Iterator[Sessions]:
    """Count Core's WebSocket sessions meanwhile."""
    sessions = Sessions()

    @callback
    def opened() -> None:
        sessions.opened += 1

    @callback
    def closed() -> None:
        sessions.closed += 1

    stops = [
        async_dispatcher_connect(hass, SIGNAL_WEBSOCKET_CONNECTED, opened),
        async_dispatcher_connect(hass, SIGNAL_WEBSOCKET_DISCONNECTED, closed),
    ]
    try:
        yield sessions
    finally:
        for stop in stops:
            stop()


def closes_running() -> list[asyncio.Task[Any]]:
    """WebSocket closes still running on the loop."""
    return [
        task
        for task in asyncio.all_tasks()
        if getattr(task.get_coro(), "__qualname__", "")
        == "HomeAssistantWebSocketClient.disconnect"
    ]


def serve_puppet(
    monkeypatch: pytest.MonkeyPatch,
    supervisor: Supervisor,
    credential: EngineCredential,
) -> None:
    """HA OS: the Supervisor lists one started Puppet app, whose token and
    Home Assistant URL options are ``credential``'s."""
    info = _puppet_info(state="started", hostname="puppet")
    info["options"] = {
        **info["options"],
        "access_token": credential.token,
        "home_assistant_url": credential.url,
    }
    supervisor.answer("GET", "/addons", installed_apps("local_puppet"))
    supervisor.answer("GET", "/addons/local_puppet/info", info)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "test-supervisor-token")
    monkeypatch.setenv("SUPERVISOR_BASE_URL", f"http://{supervisor.url}")
    set_settings(monkeypatch, dashboard_screenshot_engine_url="")


async def serve_engine(
    aiohttp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    hass: HomeAssistant,
    flips: Account | None,
    status: int = 200,
) -> None:
    """The engine answers every render with ``status`` and a PNG; each render
    saves ``flips``'s theme as light first, as Puppet does."""

    async def render(request: web.Request) -> web.Response:
        if flips is not None:
            await save_theme(hass, flips, CLOBBERED)
        return web.Response(status=status, body=PNG, content_type="image/png")

    app = web.Application()
    app.router.add_get("/{path:.*}", render)
    engine = await aiohttp_client(app)
    set_settings(monkeypatch, dashboard_screenshot_engine_url=str(engine.make_url("/")))
