"""``OAuthProxyClient``: each ``ha-mcp-oauth`` request reaches Home Assistant as
the user who signed in, at the URL the server is configured with."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.__main__ import OAuthProxyClient
from ha_mcp._vendor.fastmcp.server.auth.auth import AccessToken
from ha_mcp.auth import HomeAssistantOAuthProvider
from ha_mcp.client.rest_client import HomeAssistantAuthError, HomeAssistantClient

from ._oauth_server_core import connector, signed_in
from ._oauth_server_core import oauth_client as oauth_client
from ._oauth_server_core import oauth_provider as oauth_provider


def user_token(ha_token: str) -> AccessToken:
    return AccessToken(
        token="issued-token",
        client_id="connector",
        scopes=["homeassistant"],
        expires_at=None,
        claims={"ha_token": ha_token},
    )


@pytest.fixture
async def proxy(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> AsyncIterator[OAuthProxyClient]:
    """The proxy ``ha-mcp-oauth`` hands its tools, pointed at the real ``hass``."""
    assert await async_setup_component(hass, "auth", {})
    proxy = OAuthProxyClient(core_client.base_url)
    try:
        yield proxy
    finally:
        await proxy.close()


async def test_a_signed_in_user_reads_home_assistant_with_the_token_they_gave(
    hass: HomeAssistant,
    hass_access_token: str,
    oauth_provider: HomeAssistantOAuthProvider,
    oauth_client: httpx.AsyncClient,
    proxy: OAuthProxyClient,
) -> None:
    """From the consent form to a tool call: the token typed in is the one used."""
    hass.states.async_set("light.kitchen", "on")
    tokens = await (await connector(oauth_client)).sign_in(hass_access_token)
    access = await oauth_provider.verify_token(tokens["access_token"])
    assert access is not None

    with signed_in(access):
        states = await proxy.get_states()

    assert "light.kitchen" in {state["entity_id"] for state in states}


async def test_websocket_commands_run_as_the_signed_in_user(
    proxy: OAuthProxyClient,
    hass_access_token: str,
    hass_read_only_access_token: str,
) -> None:
    """Two users, two connections: neither borrows the other's rights."""
    admin_flags = []
    for ha_token in (hass_access_token, hass_read_only_access_token):
        with signed_in(user_token(ha_token)):
            reply = await proxy.send_websocket_message({"type": "auth/current_user"})
        admin_flags.append(reply["result"]["is_admin"])

    assert admin_flags == [True, False]


async def test_each_user_keeps_one_client(
    proxy: OAuthProxyClient,
    hass_access_token: str,
    hass_read_only_access_token: str,
) -> None:
    with signed_in(user_token(hass_access_token)):
        first = proxy.current_client()
        again = proxy.current_client()
    with signed_in(user_token(hass_read_only_access_token)):
        other = proxy.current_client()

    assert first is again
    assert other is not first


async def test_a_request_without_sign_in_is_refused(proxy: OAuthProxyClient) -> None:
    with pytest.raises(HomeAssistantAuthError, match="No OAuth token"):
        await proxy.get_states()


async def test_a_token_without_home_assistant_credentials_is_refused(
    proxy: OAuthProxyClient,
) -> None:
    token = user_token("unused")
    token.claims = {}

    with (
        signed_in(token),
        pytest.raises(HomeAssistantAuthError, match="No Home Assistant credentials"),
    ):
        await proxy.get_states()


async def test_closing_the_proxy_closes_every_users_client(
    proxy: OAuthProxyClient,
    hass_access_token: str,
    hass_read_only_access_token: str,
) -> None:
    """Shutdown releases each user's HTTP connection pool."""
    clients = []
    for ha_token in (hass_access_token, hass_read_only_access_token):
        with signed_in(user_token(ha_token)):
            clients.append(proxy.current_client())

    await proxy.close()

    assert all(client.httpx_client.is_closed for client in clients)
    with signed_in(user_token(hass_access_token)):
        assert proxy.current_client() not in clients
