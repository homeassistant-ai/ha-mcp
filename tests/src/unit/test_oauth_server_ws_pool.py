"""The WebSocket pool under ``ha-mcp-oauth``: one connection per user's token.

Every OAuth user reaches Home Assistant with their own token, so the pool
holds a connection per token and must never hand one user's to another.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.core import HomeAssistant

from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import websocket_manager
from ha_mcp.config import get_global_settings


async def long_lived_token(hass: HomeAssistant, name: str) -> str:
    """A Long-Lived Access Token of a new user, as a user types into the consent form."""
    user = await hass.auth.async_create_user(name)
    refresh = await hass.auth.async_create_refresh_token(
        user, client_name=name, token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
    )
    return hass.auth.async_create_access_token(refresh)


async def test_a_second_user_gets_a_connection_of_their_own(
    core_client: HomeAssistantClient, hass_read_only_access_token: str
) -> None:
    url, first_token = core_client.base_url, core_client.token

    first = await websocket_manager.get_client(url=url, token=first_token)
    second = await websocket_manager.get_client(
        url=url, token=hass_read_only_access_token
    )
    first_again = await websocket_manager.get_client(url=url, token=first_token)

    assert second is not first
    assert first.is_connected
    assert first_again is first


async def test_a_full_pool_closes_the_least_recently_used_connection(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(websocket_client, "MAX_POOL_SIZE", 2)
    url = core_client.base_url
    tokens = [core_client.token] + [
        await long_lived_token(hass, name) for name in ("second", "third")
    ]

    oldest = await websocket_manager.get_client(url=url, token=tokens[0])
    await websocket_manager.get_client(url=url, token=tokens[1])
    await websocket_manager.get_client(url=url, token=tokens[2])

    assert not oldest.is_connected
    assert websocket_manager.pooled_client(url, tokens[0]) is None
    assert websocket_manager.pooled_client(url, tokens[2]) is not None


async def test_shutdown_closes_every_connection_when_one_close_fails(
    core_client: HomeAssistantClient,
    hass_read_only_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url = core_client.base_url
    failing = await websocket_manager.get_client(url=url, token=core_client.token)
    other = await websocket_manager.get_client(
        url=url, token=hass_read_only_access_token
    )
    close_failing = failing.disconnect
    refused = AsyncMock(side_effect=OSError("boom"))
    monkeypatch.setattr(failing, "disconnect", refused)

    try:
        await websocket_manager.disconnect()
    finally:
        await close_failing()

    refused.assert_awaited_once()
    assert not other.is_connected
    assert websocket_manager.pooled_client(url, hass_read_only_access_token) is None


async def test_passing_the_default_tls_setting_shares_the_connection(
    core_client: HomeAssistantClient,
) -> None:
    """Callers that pass the resolved default and callers that omit it share one."""
    url, token = core_client.base_url, core_client.token
    default = get_global_settings().verify_ssl

    omitted = await websocket_manager.get_client(url=url, token=token)
    passed = await websocket_manager.get_client(
        url=url, token=token, verify_ssl=default
    )

    assert passed is omitted


async def test_overriding_the_tls_setting_gets_a_connection_of_its_own(
    core_client: HomeAssistantClient,
) -> None:
    """A connection built without certificate checks is never shared with
    callers that expect them."""
    url, token = core_client.base_url, core_client.token
    default = get_global_settings().verify_ssl

    omitted = await websocket_manager.get_client(url=url, token=token)
    overridden = await websocket_manager.get_client(
        url=url, token=token, verify_ssl=not default
    )

    assert overridden is not omitted
    assert omitted.is_connected
    assert overridden.is_connected
