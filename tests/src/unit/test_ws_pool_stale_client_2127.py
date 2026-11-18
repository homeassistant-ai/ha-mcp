"""Issue #2127: replacing a dropped Core connection cleans up its old reader."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import websocket_manager


async def _get_client(client: HomeAssistantClient, token: str | None = None):
    return await websocket_manager.get_client(
        url=client.base_url, token=token or client.token, verify_ssl=False
    )


async def test_get_client_disconnects_the_replaced_stale_client(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
):
    """The stale connection is cleaned on its owning loop and replaced once."""
    stale = await _get_client(core_client)
    await stale.disconnect()
    owner = asyncio.get_running_loop()
    original_disconnect = stale.disconnect
    cleanup_loops = []

    async def disconnect():
        cleanup_loops.append(asyncio.get_running_loop())
        await original_disconnect()

    monkeypatch.setattr(stale, "disconnect", disconnect)
    fresh = await _get_client(core_client)
    assert fresh is not stale
    assert cleanup_loops == [owner]
    assert await _get_client(core_client) is fresh
    assert await fresh.ping()


async def test_stale_client_disconnect_failure_does_not_block_replacement(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
):
    """A transport cleanup failure does not prevent reconnecting to Core."""
    stale = await _get_client(core_client)
    await stale.disconnect()
    cleanup = AsyncMock(side_effect=RuntimeError("Event loop is closed"))
    monkeypatch.setattr(stale, "disconnect", cleanup)

    fresh = await _get_client(core_client)
    assert fresh is not stale
    cleanup.assert_awaited_once()
    assert await _get_client(core_client) is fresh
    assert await fresh.ping()


async def test_stale_client_disconnect_cancellation_propagates(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
):
    """Cancellation propagates and the detached stale client is never reused."""
    stale = await _get_client(core_client)
    await stale.disconnect()
    cleanup = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(stale, "disconnect", cleanup)

    with pytest.raises(asyncio.CancelledError):
        await _get_client(core_client)

    fresh = await _get_client(core_client)
    cleanup.assert_awaited_once()
    assert fresh is not stale
    assert await fresh.ping()


async def test_eviction_disconnect_cancellation_propagates(
    core_client: HomeAssistantClient,
    hass_read_only_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
):
    """Cancelling eviction preserves the new connection and removes the old one."""
    monkeypatch.setattr(websocket_client, "MAX_POOL_SIZE", 1)
    evicted = await _get_client(core_client)
    original_disconnect = evicted.disconnect
    cleanup = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(evicted, "disconnect", cleanup)
    try:
        with pytest.raises(asyncio.CancelledError):
            await _get_client(core_client, hass_read_only_access_token)
        cleanup.assert_awaited_once()
        fresh = await _get_client(core_client, hass_read_only_access_token)
        assert fresh is not evicted
        assert await fresh.ping()
        replacement = await _get_client(core_client)
        assert replacement is not evicted
        assert await replacement.ping()
        cleanup.assert_awaited_once()
    finally:
        await original_disconnect()
