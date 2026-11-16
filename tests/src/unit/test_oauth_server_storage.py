"""What ``ha-mcp-oauth`` keeps on disk: registered clients and the signing secret.

Both live in the data directory so a container restart does not sign every
connector out (#1261).
"""

from __future__ import annotations

import logging
import stat

import httpx
import pytest

from ha_mcp.auth import HomeAssistantOAuthProvider
from ha_mcp.utils.data_paths import get_data_dir

from ._oauth_server_core import BASE_URL, connector, oauth_http
from ._oauth_server_core import oauth_client as oauth_client
from ._oauth_server_core import oauth_provider as oauth_provider

LLAT = "the-users-long-lived-token"
CLIENTS = "oauth_clients.json"
SECRET = "oauth_hmac_secret"


def restarted() -> HomeAssistantOAuthProvider:
    """The provider of a restarted server: same data directory, nothing in memory."""
    return HomeAssistantOAuthProvider(base_url=BASE_URL)


def stored_secret() -> bytes:
    return bytes.fromhex((get_data_dir() / SECRET).read_text().strip())


async def test_tokens_and_clients_survive_a_restart(
    oauth_client: httpx.AsyncClient,
) -> None:
    """ChatGPT reconnects after a restart with the refresh token it holds."""
    client = await connector(oauth_client)
    tokens = await client.sign_in(LLAT)

    provider = restarted()
    async with oauth_http(provider) as http:
        client.http = http
        refreshed = await client.refresh(tokens["refresh_token"])
        again = await client.sign_in(LLAT)

    access = await provider.verify_token(tokens["access_token"])
    assert access is not None
    assert access.claims["ha_token"] == LLAT
    assert refreshed.status_code == 200, refreshed.text
    assert again["access_token"]


async def test_tokens_are_refused_once_the_signing_secret_is_lost(
    oauth_client: httpx.AsyncClient,
) -> None:
    tokens = await (await connector(oauth_client)).sign_in(LLAT)

    (get_data_dir() / SECRET).unlink()

    assert await restarted().verify_token(tokens["access_token"]) is None


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("{invalid json{{", id="corrupt"),
        pytest.param('{"bad-id": {"not_a_real_field": 99}}', id="not-a-client"),
    ],
)
def test_an_unreadable_client_registry_starts_empty(
    content: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The server still starts; the clients register again."""
    (get_data_dir() / CLIENTS).write_text(content)

    with caplog.at_level(logging.WARNING, logger="ha_mcp.auth.provider"):
        provider = restarted()

    assert provider.clients == {}
    assert "Could not load OAuth client registry" in caplog.text


async def test_sign_in_works_when_the_registry_cannot_be_written(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A read-only data volume costs the registrations at restart, not the sign-in."""
    (get_data_dir() / CLIENTS).mkdir()

    with caplog.at_level(logging.WARNING, logger="ha_mcp.auth.provider"):
        async with oauth_http(restarted()) as http:
            tokens = await (await connector(http)).sign_in(LLAT)

    assert tokens["access_token"]
    assert "Failed to persist OAuth client registry" in caplog.text


@pytest.mark.parametrize("name", [CLIENTS, SECRET])
async def test_stored_credentials_are_readable_by_the_owner_only(
    oauth_client: httpx.AsyncClient, name: str
) -> None:
    """The registry can hold client secrets; the secret signs every token."""
    await connector(oauth_client)

    assert stat.S_IMODE((get_data_dir() / name).stat().st_mode) == 0o600


def test_first_start_stores_a_full_length_signing_secret() -> None:
    restarted()

    assert len(stored_secret()) == 32


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("not-valid-hex!!", id="not-hex"),
        pytest.param("", id="empty"),
        pytest.param("00" * 16, id="too-short"),
    ],
)
async def test_an_unusable_signing_secret_is_replaced(content: str) -> None:
    """The server starts with a new secret, and keeps it across the next restart."""
    (get_data_dir() / SECRET).write_text(content)

    async with oauth_http(restarted()) as http:
        tokens = await (await connector(http)).sign_in(LLAT)

    assert len(stored_secret()) == 32
    assert await restarted().verify_token(tokens["access_token"]) is not None
