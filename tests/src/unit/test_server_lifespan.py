"""Unit tests for the startup admin-token check in ``server_lifespan`` (#2546)."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp import server_lifespan
from ha_mcp.config import OAUTH_MODE_TOKEN


def _settings(token: str = "llat") -> MagicMock:
    settings = MagicMock()
    settings.homeassistant_token = token
    return settings


@pytest.fixture
def fake_client():
    client = MagicMock()
    client.token_is_admin = AsyncMock(return_value=False)
    client_class = MagicMock()
    client_class.return_value.__aenter__ = AsyncMock(return_value=client)
    client_class.return_value.__aexit__ = AsyncMock(return_value=None)
    with patch.object(server_lifespan, "HomeAssistantClient", client_class):
        yield client


@pytest.mark.asyncio
async def test_startup_check_asks_about_the_configured_token(fake_client):
    with (
        patch.object(server_lifespan, "is_embedded", return_value=False),
        patch.object(server_lifespan, "get_global_settings", return_value=_settings()),
    ):
        await server_lifespan.warn_if_non_admin_token()
    fake_client.token_is_admin.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("embedded", "token"),
    [(True, "llat"), (False, OAUTH_MODE_TOKEN)],
    ids=["embedded", "oauth"],
)
async def test_startup_check_skips_without_a_server_token(fake_client, embedded, token):
    with (
        patch.object(server_lifespan, "is_embedded", return_value=embedded),
        patch.object(
            server_lifespan, "get_global_settings", return_value=_settings(token)
        ),
    ):
        await server_lifespan.warn_if_non_admin_token()
    fake_client.token_is_admin.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_check_never_raises(fake_client):
    fake_client.token_is_admin.side_effect = RuntimeError("boom")
    with (
        patch.object(server_lifespan, "is_embedded", return_value=False),
        patch.object(server_lifespan, "get_global_settings", return_value=_settings()),
    ):
        await server_lifespan.warn_if_non_admin_token()


@pytest.mark.asyncio
async def test_lifespan_runs_the_hacs_nudge_and_cancels_the_check():
    started = asyncio.Event()
    cancelled = False
    hacs_entered = False

    async def never_finishes():
        nonlocal cancelled
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise

    @asynccontextmanager
    async def fake_hacs_lifespan(_server):
        nonlocal hacs_entered
        hacs_entered = True
        yield {}

    with (
        patch.object(server_lifespan, "warn_if_non_admin_token", new=never_finishes),
        patch.object(server_lifespan, "hacs_refresh_lifespan", new=fake_hacs_lifespan),
    ):
        async with server_lifespan.server_lifespan(object()):
            await started.wait()

    assert hacs_entered
    assert cancelled, "exiting the lifespan must cancel a pending admin check"
