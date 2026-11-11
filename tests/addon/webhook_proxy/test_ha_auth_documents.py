"""What the bound OAuth views serve in ha_auth mode, and after it ends.

Fetched over Home Assistant's real HTTP server, as an MCP client's discovery
fetches them. A bound view cannot be dropped until Home Assistant restarts, so
the views read the live mode on every request; these tests pin what a client
gets in each state the entry can be left in.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

from homeassistant.core import CoreState, HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._auth_modes import (
    HA_AUTH,
    LEGACY,
    WEBHOOK_ID,
    as_document_urls,
    break_dcr_secret,
    fetch_json,
    pkce_pair,
    prm_url,
    proxy_config,
    reload_proxy,
)
from ._proxy import Upstream, start_proxy
from ._variants import Variant


async def test_ha_auth_as_document_is_identical_at_every_discovery_url(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """claude.ai caches the authorization config per URL (#1714), so every
    well-known spelling must serve the one ha_auth document: a public PKCE
    client with CIMD and DCR, whose endpoints all belong to the proxy."""
    await start_proxy(hass, variant, fs_root, proxy_config(oauth=HA_AUTH))
    client = await hass_client_no_auth()
    base = f"http://ha.example{variant.oauth_base}"

    docs = [
        await fetch_json(client, url, host="ha.example")
        for url in as_document_urls(variant)
    ]

    status, canonical = docs[0]
    assert status == 200
    assert canonical["issuer"] == base
    assert canonical["authorization_endpoint"] == f"{base}/authorize"
    assert canonical["token_endpoint"] == f"{base}/token"
    assert canonical["registration_endpoint"] == f"{base}/register"
    assert canonical["revocation_endpoint"] == f"{base}/revoke"
    assert canonical["response_types_supported"] == ["code"]
    assert canonical["grant_types_supported"] == ["authorization_code", "refresh_token"]
    assert canonical["code_challenge_methods_supported"] == ["S256"]
    assert canonical["token_endpoint_auth_methods_supported"] == ["none"]
    assert canonical["client_id_metadata_document_supported"] is True
    assert docs[1:] == [(200, canonical)] * (len(docs) - 1)


async def test_ha_auth_protected_resource_document_names_the_proxy_issuer(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """The webhook's RFC 9728 document points clients at the proxy's own
    authorization server for the host they reached it on."""
    await start_proxy(hass, variant, fs_root, proxy_config(oauth=HA_AUTH))
    client = await hass_client_no_auth()

    status, body = await fetch_json(client, prm_url(), host="ha.example")

    assert status == 200
    assert body == {
        "resource": f"http://ha.example/api/webhook/{WEBHOOK_ID}",
        "authorization_servers": [f"http://ha.example{variant.oauth_base}"],
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://github.com/homeassistant-ai/ha-mcp",
    }


def _consent_query() -> dict[str, str]:
    """A legacy authorization request the root /authorize would accept."""
    _verifier, challenge = pkce_pair()
    return {
        "response_type": "code",
        "client_id": LEGACY["client_id"],
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "st",
    }


async def test_root_authorize_and_token_stop_serving_once_ha_auth_replaces_legacy(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """Legacy's root /authorize and /token stay bound after a switch to
    ha_auth, but must not render consent or mint legacy tokens any more."""
    hass.set_state(CoreState.not_running)
    entry = await start_proxy(hass, variant, fs_root, proxy_config(oauth=LEGACY))
    client = await hass_client_no_auth()
    query = _consent_query()
    serving = await client.get("/authorize", params=query)
    assert serving.status == 200

    assert await reload_proxy(
        hass, fs_root, variant, entry, proxy_config(oauth=HA_AUTH)
    )

    assert (await client.get("/authorize", params=query)).status == 404
    consent = await client.post("/authorize", data={**query, "action": "approve"})
    assert consent.status == 404
    token = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": LEGACY["client_id"],
            "client_secret": LEGACY["client_secret"],
        },
    )
    assert token.status == 404


async def _assert_every_oauth_view_404s(
    hass_client_no_auth: ClientSessionGenerator, variant: Variant
) -> None:
    client = await hass_client_no_auth()
    for url in [*as_document_urls(variant), prm_url()]:
        assert (await client.get(url)).status == 404, url
    query = _consent_query()
    for path in ("/authorize", f"{variant.oauth_base}/authorize"):
        assert (await client.get(path, params=query)).status == 404, path
        consent = await client.post(path, data={**query, "action": "approve"})
        assert consent.status == 404, path
    for path in ("/token", f"{variant.oauth_base}/token"):
        token = await client.post(path, data={"grant_type": "authorization_code"})
        assert token.status == 404, path


async def test_unloaded_entry_leaves_every_bound_oauth_view_answering_404(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """After the entry is unloaded the views stay bound until a restart; each
    must answer like an unregistered route instead of serving a dead mode."""
    hass.set_state(CoreState.not_running)
    entry = await start_proxy(hass, variant, fs_root, proxy_config(oauth=LEGACY))

    assert await hass.config_entries.async_unload(entry.entry_id)

    await _assert_every_oauth_view_404s(hass_client_no_auth, variant)


async def test_oauth_off_without_live_discovery_leaves_stale_views_answering_404(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
) -> None:
    """OAuth turned off after legacy, with none-mode discovery failing to
    start (it fails open): the entry runs with no live mode, the webhook keeps
    forwarding, and every view still bound from legacy answers 404 rather than
    falling back to serving legacy."""
    hass.set_state(CoreState.not_running)
    entry = await start_proxy(hass, variant, fs_root, proxy_config(oauth=LEGACY))
    break_dcr_secret(fs_root, variant)

    off = proxy_config(target_url=upstream.target_url())
    assert await reload_proxy(hass, fs_root, variant, entry, off)

    await _assert_every_oauth_view_404s(hass_client_no_auth, variant)
    client = await hass_client_no_auth()
    response = await client.post(f"/api/webhook/{WEBHOOK_ID}", data=b"{}")
    assert response.status == 200
    assert len(upstream.requests) == 1


async def test_401_challenge_is_identical_in_legacy_and_ha_auth(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """Both modes challenge an unauthenticated webhook call with the same
    pointer to the path-scoped protected-resource document; legacy pins the
    base URL and ha_auth derives it, so for the pinned host they match."""
    hass.set_state(CoreState.not_running)
    legacy = proxy_config(oauth=LEGACY, public_base_url="http://myhost.example")
    entry = await start_proxy(hass, variant, fs_root, legacy)
    client = await hass_client_no_auth()

    async def challenge() -> str:
        response = await client.post(
            f"/api/webhook/{WEBHOOK_ID}",
            data=b"{}",
            headers={"Host": "myhost.example"},
        )
        assert response.status == 401
        return response.headers["WWW-Authenticate"]

    legacy_challenge = await challenge()
    assert await reload_proxy(
        hass, fs_root, variant, entry, proxy_config(oauth=HA_AUTH)
    )

    assert (
        await challenge()
        == legacy_challenge
        == (
            'Bearer realm="MCP Proxy", resource_metadata='
            f'"http://myhost.example{prm_url()}"'
        )
    )
