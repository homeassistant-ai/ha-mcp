"""The webhook's bearer gate in legacy OAuth mode, driven over HTTP."""

from __future__ import annotations

import socket
from pathlib import Path
from types import ModuleType

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._legacy_oauth import (
    PUBLIC_BASE_URL,
    WEBHOOK_ID,
    obtain_tokens,
    post_webhook,
    set_up_legacy,
)
from ._proxy import Upstream, set_up_proxy, write_proxy_config
from ._variants import Variant


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "Basic Y2xpZW50OnNlY3JldA==",
        "Bearer no-dot-token",
        "Bearer not.a.valid.token",
        "Bearer é.AA",
    ],
    ids=["missing", "basic-scheme", "no-signature", "bad-signature", "non-ascii"],
)
async def test_webhook_without_a_valid_bearer_gets_the_discovery_challenge(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    authorization: str | None,
) -> None:
    """Any request without a usable bearer gets the 401 whose
    ``resource_metadata`` pointer starts the client's OAuth discovery, and never
    reaches the MCP server. A non-ASCII bearer once raised inside the gate,
    which Core's webhook handler turns into an empty 200: no challenge, so the
    client never learned to sign in."""
    await set_up_legacy(
        hass,
        variant,
        fs_root,
        target_url=upstream.target_url(),
        public_base_url=PUBLIC_BASE_URL,
    )
    client = await hass_client_no_auth()

    response = await post_webhook(client, authorization)

    assert response.status == 401
    challenge = response.headers["WWW-Authenticate"]
    assert challenge.startswith("Bearer ")
    assert (
        f'resource_metadata="{PUBLIC_BASE_URL}/.well-known/oauth-protected-resource'
        f'/api/webhook/{WEBHOOK_ID}"'
    ) in challenge
    assert upstream.requests == []


async def test_webhook_relays_a_request_bearing_an_issued_access_token(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
) -> None:
    entry = await set_up_legacy(
        hass, variant, fs_root, target_url=upstream.target_url()
    )
    client = await hass_client_no_auth()
    tokens = await obtain_tokens(client)

    response = await post_webhook(client, f"Bearer {tokens['access_token']}")

    assert response.status == 200
    assert len(upstream.requests) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_refresh_token_is_not_accepted_as_a_webhook_bearer(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
) -> None:
    """The 30-day refresh token must not stand in for the one-hour access
    token, or a leaked refresh token grants a month of direct access."""
    await set_up_legacy(hass, variant, fs_root, target_url=upstream.target_url())
    client = await hass_client_no_auth()
    tokens = await obtain_tokens(client)

    response = await post_webhook(client, f"Bearer {tokens['refresh_token']}")

    assert response.status == 401
    assert upstream.requests == []


def _refused_target() -> str:
    """A target URL on a loopback port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/private_zctpwlX7ZkIAr7oqdfLPxw"


async def test_upstream_down_502_does_not_suggest_restarting_home_assistant(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """#1694: the restart hint is for stale OAuth registrations only; a restart
    does not bring a stopped MCP server back, so the 502 must not send the user
    off restarting Home Assistant."""
    write_proxy_config(
        fs_root, variant, {"target_url": _refused_target(), "webhook_id": WEBHOOK_ID}
    )
    entry = await set_up_proxy(hass, variant)
    client = await hass_client_no_auth()

    response = await post_webhook(client, None)

    assert response.status == 502
    assert "restart Home Assistant" not in await response.text()
    assert await hass.config_entries.async_unload(entry.entry_id)
