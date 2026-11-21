"""``ha-mcp-oauth`` registration and tokens, through the provider's routes.

The tokens are stateless: each carries the user's Long-Lived Access Token,
signed by the server, so what a token lets through is decided by the token.
"""

from __future__ import annotations

import json
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from freezegun import freeze_time

from ha_mcp.auth import HomeAssistantOAuthProvider
from ha_mcp.auth.provider import (
    ACCESS_TOKEN_EXPIRY_SECONDS,
    REFRESH_TOKEN_EXPIRY_SECONDS,
)

from ._oauth_server_core import connector, register
from ._oauth_server_core import oauth_client as oauth_client
from ._oauth_server_core import oauth_provider as oauth_provider

LLAT = "the-users-long-lived-token"
START = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)


def b64(raw: bytes) -> str:
    return urlsafe_b64encode(raw).decode().rstrip("=")


def assert_refused(resp: httpx.Response) -> None:
    """MCP: an invalid or expired grant is a 401, so the client signs in again."""
    assert resp.status_code == 401
    assert resp.json()["error"] == "invalid_grant"


async def test_a_client_registered_without_scopes_gets_every_scope(
    oauth_provider: HomeAssistantOAuthProvider, oauth_client: httpx.AsyncClient
) -> None:
    """ChatGPT registers without scopes and asks for them when it authorizes."""
    resp = await register(oauth_client, client_name="ChatGPT")

    assert resp.status_code == 201
    stored = await oauth_provider.get_client(resp.json()["client_id"])
    assert stored is not None
    assert stored.scope == "homeassistant mcp"


async def test_a_client_asking_for_an_unknown_scope_cannot_register(
    oauth_client: httpx.AsyncClient,
) -> None:
    resp = await register(oauth_client, scope="invalid_scope homeassistant")

    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client_metadata"


async def test_signing_in_issues_tokens_carrying_only_the_users_token(
    oauth_provider: HomeAssistantOAuthProvider, oauth_client: httpx.AsyncClient
) -> None:
    """The Home Assistant URL stays server-side: no token names one (SSRF)."""
    tokens = await (await connector(oauth_client)).sign_in(LLAT)

    assert tokens["token_type"] == "Bearer"
    assert tokens["refresh_token"]
    access = await oauth_provider.verify_token(tokens["access_token"])
    assert access is not None
    assert access.claims == {"ha_token": LLAT}


async def test_each_refresh_hands_out_tokens_that_keep_the_users_token(
    oauth_provider: HomeAssistantOAuthProvider, oauth_client: httpx.AsyncClient
) -> None:
    """ChatGPT refreshes with the newest refresh token, again and again."""
    client = await connector(oauth_client)
    tokens = await client.sign_in(LLAT)

    for _ in range(2):
        resp = await client.refresh(tokens["refresh_token"])
        assert resp.status_code == 200, resp.text
        tokens = resp.json()
        access = await oauth_provider.verify_token(tokens["access_token"])
        assert access is not None
        assert access.claims["ha_token"] == LLAT


async def test_a_refresh_token_is_not_an_access_token(
    oauth_provider: HomeAssistantOAuthProvider, oauth_client: httpx.AsyncClient
) -> None:
    """A refresh token lives a week; it must not stand in for an access token."""
    tokens = await (await connector(oauth_client)).sign_in(LLAT)

    assert await oauth_provider.verify_token(tokens["access_token"]) is not None
    assert await oauth_provider.verify_token(tokens["refresh_token"]) is None


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(
            b64(json.dumps({"ha_token": "forged", "iat": 1}).encode()), id="unsigned"
        ),
        pytest.param(b64(b"[1, 2, 3]"), id="not-an-object"),
        pytest.param("invalid_random_string", id="not-a-token"),
    ],
)
async def test_a_token_the_server_did_not_sign_is_refused(
    oauth_provider: HomeAssistantOAuthProvider, token: str
) -> None:
    assert await oauth_provider.verify_token(token) is None


async def test_an_access_token_is_refused_once_it_expires(
    oauth_provider: HomeAssistantOAuthProvider, oauth_client: httpx.AsyncClient
) -> None:
    with freeze_time(START, tick=True) as clock:
        tokens = await (await connector(oauth_client)).sign_in(LLAT)
        assert await oauth_provider.verify_token(tokens["access_token"]) is not None
        clock.move_to(START + timedelta(seconds=ACCESS_TOKEN_EXPIRY_SECONDS + 60))

        assert await oauth_provider.verify_token(tokens["access_token"]) is None


async def test_a_refresh_token_is_refused_once_it_expires(
    oauth_client: httpx.AsyncClient,
) -> None:
    with freeze_time(START, tick=True) as clock:
        client = await connector(oauth_client)
        tokens = await client.sign_in(LLAT)
        clock.move_to(START + timedelta(seconds=REFRESH_TOKEN_EXPIRY_SECONDS + 60))

        resp = await client.refresh(tokens["refresh_token"])

    assert_refused(resp)


async def test_a_refresh_token_works_only_for_the_client_it_was_issued_to(
    oauth_client: httpx.AsyncClient,
) -> None:
    owner = await connector(oauth_client)
    other = await connector(oauth_client)
    tokens = await owner.sign_in(LLAT)

    stolen = await other.refresh(tokens["refresh_token"])
    own = await owner.refresh(tokens["refresh_token"])

    assert_refused(stolen)
    assert own.status_code == 200


async def test_a_garbled_refresh_token_is_refused(
    oauth_client: httpx.AsyncClient,
) -> None:
    client = await connector(oauth_client)

    resp = await client.refresh("not_valid_base64_!!!")

    assert_refused(resp)


async def test_revoking_leaves_the_tokens_working(
    oauth_provider: HomeAssistantOAuthProvider, oauth_client: httpx.AsyncClient
) -> None:
    """The tokens hold no server state: the user revokes their Home Assistant
    token to cut access (SECURITY.md, "OAuth Bearer token design")."""
    client = await connector(oauth_client)
    tokens = await client.sign_in(LLAT)

    resp = await oauth_client.post(
        "/revoke",
        data={
            "token": tokens["refresh_token"],
            "client_id": client.client_id,
            "client_secret": "",
        },
    )

    assert resp.status_code == 200
    assert await oauth_provider.verify_token(tokens["access_token"]) is not None
    assert (await client.refresh(tokens["refresh_token"])).status_code == 200
