"""Unit tests for the startup admin-token check in ``server_lifespan`` (#2546).

The check asks the real ``hass`` (through ``core_client``'s HTTP server) who the
configured token belongs to; a read-only user's token is the non-admin answer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
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


@pytest.fixture
async def silent_server() -> AsyncIterator[str]:
    """A listener that accepts connections and never answers."""

    async def accept_and_stay_silent(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.read()
        writer.close()

    server = await asyncio.start_server(accept_and_stay_silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.close()


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


async def test_lifespan_runs_the_hacs_nudge_and_cancels_the_startup_tasks(
    hass: HomeAssistant,
    hass_access_token: str,
    silent_server: str,
    point_server_at: Callable[[str, str | None], None],
) -> None:
    point_server_at(hass_access_token, silent_server)
    tasks_before = asyncio.all_tasks()

    async with server_lifespan.server_lifespan(object()) as state:
        # The admin check is blocked on the silent listener.
        await asyncio.sleep(0.2)
        running = asyncio.all_tasks() - tasks_before
        assert running, "the startup tasks must be running inside the lifespan"

    assert state == {}
    await asyncio.sleep(0.1)
    leftover = {task for task in asyncio.all_tasks() - tasks_before if not task.done()}
    assert not leftover, "exiting the lifespan must cancel the startup tasks"
