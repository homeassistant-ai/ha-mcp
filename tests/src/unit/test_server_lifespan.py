"""Unit tests for the startup admin-token check in ``server_lifespan`` (#2546).

The check asks the real ``hass`` (through ``core_client``'s HTTP server) who the
configured token belongs to; a read-only user's token is the non-admin answer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp import server_lifespan
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.config import OAUTH_MODE_TOKEN, reset_global_settings

NON_ADMIN_WARNING = "belongs to a non-admin user"


@pytest.fixture
async def point_server_at(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[str, str | None], None]:
    """Configure the server's settings to talk to the real ``hass`` with a token."""
    assert await async_setup_component(hass, "auth", {})

    def configure(token: str, url: str | None = None) -> None:
        monkeypatch.setenv("HOMEASSISTANT_URL", url or core_client.base_url)
        monkeypatch.setenv("HOMEASSISTANT_TOKEN", token)
        reset_global_settings()

    return configure


async def test_startup_check_asks_about_the_configured_token(
    hass_read_only_access_token: str,
    point_server_at: Callable[[str, str | None], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    point_server_at(hass_read_only_access_token, None)

    await server_lifespan.warn_if_non_admin_token()

    assert NON_ADMIN_WARNING in caplog.text


@pytest.mark.parametrize(
    ("embedded", "token"),
    [(True, "read-only"), (False, OAUTH_MODE_TOKEN)],
    ids=["embedded", "oauth"],
)
async def test_startup_check_skips_without_a_server_token(
    embedded: bool,
    token: str,
    hass_read_only_access_token: str,
    point_server_at: Callable[[str, str | None], None],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    if embedded:
        monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    point_server_at(hass_read_only_access_token if embedded else token, None)
    caplog.set_level(logging.INFO)

    await server_lifespan.warn_if_non_admin_token()

    assert NON_ADMIN_WARNING not in caplog.text
    assert "admin-token check" not in caplog.text
    assert "Could not ask Home Assistant" not in caplog.text


async def test_startup_check_never_raises(
    hass_access_token: str,
    point_server_at: Callable[[str, str | None], None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    point_server_at(hass_access_token, None)
    with patch.object(
        HomeAssistantClient,
        "token_is_admin",
        AsyncMock(side_effect=HomeAssistantConnectionError("down")),
    ):
        await server_lifespan.warn_if_non_admin_token()

    assert "Startup admin-token check failed" in caplog.text


async def test_lifespan_runs_the_hacs_nudge_and_cancels_the_startup_tasks() -> None:
    """The lifespan's own parts are replaced: none of them speaks for Home Assistant."""
    started = {"admin check": asyncio.Event(), "discovery": asyncio.Event()}
    cancelled: set[str] = set()
    hacs_entered = False

    def never_finishes(name: str) -> Callable[[], Coroutine[Any, Any, None]]:
        async def run() -> None:
            started[name].set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.add(name)
                raise

        return run

    @asynccontextmanager
    async def fake_hacs_lifespan(_server: object) -> AsyncIterator[dict[str, Any]]:
        nonlocal hacs_entered
        hacs_entered = True
        yield {}

    with (
        patch.object(
            server_lifespan,
            "warn_if_non_admin_token",
            new=never_finishes("admin check"),
        ),
        patch.object(
            server_lifespan, "announce_mcp_discovery", new=never_finishes("discovery")
        ),
        patch.object(server_lifespan, "hacs_refresh_lifespan", new=fake_hacs_lifespan),
    ):
        async with server_lifespan.server_lifespan(object()):
            for event in started.values():
                await event.wait()

    assert hacs_entered
    assert cancelled == set(started), "exiting the lifespan must cancel both tasks"
