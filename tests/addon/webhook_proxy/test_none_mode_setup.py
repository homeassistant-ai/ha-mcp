"""None mode (OAuth off) set up by Core: invisible auto-approve discovery.

With OAuth off the secret webhook URL is the only credential, but MCP clients
that insist on OAuth discovery still get the proxy's own corrected documents
and an authorization server that approves without a login (#1969). These
tests cover what setup binds, how it fails, how it switches with ha_auth, and
that the webhook itself stays unauthenticated.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._auth_modes import (
    HA_AUTH,
    LEGACY,
    WEBHOOK_ID,
    as_document_urls,
    break_dcr_secret,
    fetch_json,
    marker_path,
    prm_url,
    proxy_config,
    reload_proxy,
    route_counts,
    scoped_urls,
)
from ._proxy import Upstream, restart_issue, start_proxy
from ._variants import Variant


async def test_oauth_off_binds_discovery_and_autoapprove_routes_but_no_root_views(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """OAuth off binds the discovery URLs and the scoped auto-approve routes
    (DCR included), never legacy's root /authorize and /token, and clears a
    stale restart Repair: none mode needs no restart."""
    marker = marker_path(fs_root, variant)
    marker.write_text('{"reason": "stale"}')

    entry = await start_proxy(hass, variant, fs_root, proxy_config())

    assert entry.state is ConfigEntryState.LOADED
    routes = route_counts(hass)
    for url in [*as_document_urls(variant), prm_url(), *scoped_urls(variant)]:
        assert routes[url] == 1, url
    assert routes["/authorize"] == routes["/token"] == 0
    assert not marker.exists()
    assert restart_issue(hass, variant) is None
    client = await hass_client_no_auth()
    registration = await client.post(
        f"{variant.oauth_base}/register",
        json={"redirect_uris": ["https://client.example/cb"]},
    )
    assert registration.status == 201


async def test_rotated_webhook_id_with_oauth_off_raises_the_shared_restart_repair(
    hass: HomeAssistant, component: ModuleType, variant: Variant, fs_root: Path
) -> None:
    """A webhook id rotated with OAuth off needs a restart too (its discovery
    URL binds only at startup). It raises the shared restart Repair, whose
    text must cover a rotation, not only enabling OAuth."""
    entry = await start_proxy(hass, variant, fs_root, proxy_config())
    assert restart_issue(hass, variant) is None

    rotated = proxy_config(webhook_id="mcp_rotated_webhook")
    assert await reload_proxy(hass, fs_root, variant, entry, rotated)

    issue = restart_issue(hass, variant)
    assert issue is not None
    assert issue.translation_key is not None
    strings = json.loads((variant.component_dir / "strings.json").read_text())
    text = strings["issues"][issue.translation_key]
    blurb = text["title"] + text["fix_flow"]["step"]["confirm"]["description"]
    assert "webhook ID was rotated" in blurb, (
        "a none-mode operator would be told OAuth was enabled"
    )


async def test_failed_autoapprove_setup_keeps_the_plain_webhook_forwarding(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
) -> None:
    """None-mode discovery is an extra on an already unauthenticated proxy:
    when it cannot start, setup still succeeds and the webhook keeps
    forwarding; only the discovery documents stay dark."""
    break_dcr_secret(fs_root, variant)

    entry = await start_proxy(
        hass, variant, fs_root, proxy_config(target_url=upstream.target_url())
    )

    assert entry.state is ConfigEntryState.LOADED
    client = await hass_client_no_auth()
    response = await client.post(f"/api/webhook/{WEBHOOK_ID}", data=b"{}")
    assert response.status == 200
    assert len(upstream.requests) == 1
    status, _doc = await fetch_json(client, as_document_urls(variant)[0])
    assert status == 404


async def test_forged_bearer_is_ignored_with_oauth_off(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
) -> None:
    """The auto-approve token is cosmetic: with OAuth off the webhook never
    401s, whatever bearer a client sends, and forwards the call."""
    await start_proxy(
        hass, variant, fs_root, proxy_config(target_url=upstream.target_url())
    )
    client = await hass_client_no_auth()

    response = await client.post(
        f"/api/webhook/{WEBHOOK_ID}",
        data=b"{}",
        headers={"Authorization": "Bearer whatever-cosmetic-or-forged"},
    )

    assert response.status == 200
    assert len(upstream.requests) == 1


async def test_none_and_ha_auth_switch_documents_without_rebinding(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """Switching none -> ha_auth -> none needs no restart: the views bound
    once serve whichever mode is live, so the document changes with it."""
    entry = await start_proxy(hass, variant, fs_root, proxy_config())
    client = await hass_client_no_auth()
    url = as_document_urls(variant)[0]

    async def grants() -> list[str]:
        status, doc = await fetch_json(client, url)
        assert status == 200
        assert doc["authorization_endpoint"] == (
            f"http://proxy.example{variant.oauth_base}/authorize"
        )
        grant_types: list[str] = doc["grant_types_supported"]
        return grant_types

    # None mode has no refresh cycle; ha_auth hands out core's refresh tokens.
    assert await grants() == ["authorization_code"]
    assert await reload_proxy(
        hass, fs_root, variant, entry, proxy_config(oauth=HA_AUTH)
    )
    assert await grants() == ["authorization_code", "refresh_token"]
    assert await reload_proxy(hass, fs_root, variant, entry, proxy_config())
    assert await grants() == ["authorization_code"]
    assert all(route_counts(hass)[u] == 1 for u in as_document_urls(variant))
    assert restart_issue(hass, variant) is None


async def test_no_discovery_url_without_the_webhook_id_reveals_it_in_any_mode(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """With OAuth off the webhook id is the only credential (#1976): no
    document reachable without already knowing it may contain it, in any
    mode, since a later switch to none mode would promote a published id
    into the credential."""
    entry = await start_proxy(hass, variant, fs_root, proxy_config())
    client = await hass_client_no_auth()

    for config in (
        proxy_config(),
        proxy_config(oauth=HA_AUTH),
        proxy_config(oauth=LEGACY),
    ):
        assert await reload_proxy(hass, fs_root, variant, entry, config)
        for url in as_document_urls(variant):
            status, doc = await fetch_json(client, url)
            assert status == 200, url
            assert WEBHOOK_ID not in json.dumps(doc), f"{url} leaks the webhook id"


async def test_path_scoped_protected_resource_serves_with_oauth_off(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """The protected-resource document whose URL carries the webhook id keeps
    serving in none mode: its caller already holds the id, and it is the
    document claude.ai's none-mode discovery follows."""
    await start_proxy(hass, variant, fs_root, proxy_config())
    client = await hass_client_no_auth()

    status, body = await fetch_json(client, prm_url(), host="legit.example")

    assert status == 200
    assert body["resource"] == f"http://legit.example/api/webhook/{WEBHOOK_ID}"
    assert body["authorization_servers"] == [
        f"http://legit.example{variant.oauth_base}"
    ]
