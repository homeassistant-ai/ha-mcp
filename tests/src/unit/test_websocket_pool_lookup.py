"""``WebSocketManager.pooled_client``: the pool's connection, read without connecting.

The component capability cache compares it with the connection its caps were
probed over (#2967), on every cached read, so it must never open one.
"""

from __future__ import annotations

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import websocket_manager


async def test_returns_the_pooled_connection_without_connecting(
    core_client: HomeAssistantClient,
) -> None:
    url, token = core_client.base_url, core_client.token
    assert websocket_manager.pooled_client(url, token, False) is None

    pooled = await websocket_manager.get_client(url=url, token=token, verify_ssl=False)

    found = websocket_manager.pooled_client(url=url, token=token, verify_ssl=False)

    assert found is pooled
    assert found.is_connected


async def test_a_dropped_connection_is_returned_until_replaced(
    core_client: HomeAssistantClient,
) -> None:
    url, token = core_client.base_url, core_client.token
    pooled = await websocket_manager.get_client(url=url, token=token, verify_ssl=False)
    await pooled.disconnect()
    assert not pooled.is_connected

    assert websocket_manager.pooled_client(url, token, False) is pooled

    replaced = await websocket_manager.get_client(
        url=url, token=token, verify_ssl=False
    )
    assert replaced is not pooled
    assert replaced.is_connected
    assert websocket_manager.pooled_client(url, token, False) is replaced


async def test_other_credentials_have_no_connection(
    core_client: HomeAssistantClient,
) -> None:
    url, token = core_client.base_url, core_client.token
    pooled = await websocket_manager.get_client(url=url, token=token, verify_ssl=False)

    assert websocket_manager.pooled_client(url, "other", False) is None
    assert websocket_manager.pooled_client(url, token, True) is None
    assert websocket_manager.pooled_client(url, token, False) is pooled
