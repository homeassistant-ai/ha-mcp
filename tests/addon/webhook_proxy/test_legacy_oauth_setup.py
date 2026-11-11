"""Core setting the proxy up with legacy OAuth: what gets bound, what refuses.

Home Assistant binds new HTTP views cleanly only while it starts, and never
drops one until it restarts. So a setup either binds the OAuth endpoints at
boot, reuses the ones it already bound, or raises the click-to-restart Repair.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import ClientSessionGenerator, WebSocketGenerator

from ._legacy_oauth import (
    UNUSED_TARGET,
    WEBHOOK_ID,
    bound_routes,
    oauth_routes_added,
    obtain_tokens,
    post_webhook,
    reload_with,
    set_up_legacy,
)
from ._proxy import (
    Upstream,
    record_client_sessions,
    set_up_proxy,
    state_path,
    write_proxy_config,
)
from ._variants import VARIANTS, Variant

ROTATED_WEBHOOK_ID = "mcp_webhook_id_bbbb"


def _sibling_domain(variant: Variant) -> str:
    return next(other.domain for other in VARIANTS if other is not variant)


async def _registered_webhooks(hass_ws_client: WebSocketGenerator) -> set[str]:
    ws = await hass_ws_client()
    await ws.send_json_auto_id({"type": "webhook/list"})
    reply = await ws.receive_json()
    assert reply["success"], reply
    return {webhook["webhook_id"] for webhook in reply["result"]}


def _restart_issue(
    issue_registry: ir.IssueRegistry, component: ModuleType, variant: Variant
) -> ir.IssueEntry | None:
    return issue_registry.async_get_issue(variant.domain, component.repairs.ISSUE_ID)


def _seed_stale_restart_repair(
    hass: HomeAssistant, component: ModuleType, variant: Variant, fs_root: Path
) -> Path:
    """The Repair and marker an earlier session left behind."""
    marker = state_path(fs_root, variant.oauth_marker)
    marker.write_text('{"reason": "stale"}')
    component.repairs.create_issue(hass, variant.domain)
    return marker


async def test_legacy_oauth_binds_every_discovery_and_flow_endpoint(
    hass: HomeAssistant,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """The root /authorize and /token serve clients that build them from the
    host; the scoped routes are the advertised ones; the well-known locations
    are where clients look for the documents (#1714). A missing one leaves some
    client unable to sign in."""
    assert await async_setup_component(hass, "webhook", {})
    before = bound_routes(hass)

    entry = await set_up_legacy(hass, variant, fs_root, at_boot=True)

    assert entry.state is ConfigEntryState.LOADED
    base = variant.oauth_base
    prm = f"/.well-known/oauth-protected-resource/api/webhook/{WEBHOOK_ID}"
    assert oauth_routes_added(hass, before) == {
        "/authorize",
        "/token",
        f"{base}/authorize",
        f"{base}/token",
        f"{base}/revoke",
        f"{base}/authorization-server",
        prm,
        *(prm + suffix for suffix in component.readonly_webhook.ENDPOINT_SUFFIXES),
        f"/.well-known/oauth-authorization-server{base}",
        f"/.well-known/openid-configuration{base}",
        f"{base}/.well-known/openid-configuration",
        f"{base}/.well-known/oauth-authorization-server",
    }


async def test_unloadable_signing_key_refuses_to_start_without_the_webhook(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user asked for OAuth; when it cannot be enforced the entry fails
    instead of leaving the webhook open without it, and closes its session."""
    sessions = record_client_sessions(monkeypatch)
    component.oauth.SECRET_FILE.mkdir(parents=True)

    entry = await set_up_legacy(hass, variant, fs_root)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "Failed to enable OAuth" in (entry.reason or "")
    assert WEBHOOK_ID not in await _registered_webhooks(hass_ws_client)
    assert sessions
    assert all(session.closed for session in sessions)


async def test_sibling_flavor_owning_the_root_routes_is_refused_loudly(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the other flavor bound /authorize and /token this session (it may
    since have stopped; Core keeps its views until a restart), binding ours
    would be silently shadowed by views signing with another key. Setup fails
    with a message telling the user what to do."""
    sibling = _sibling_domain(variant)
    hass.data[component.OAUTH_ROUTE_OWNER_KEY] = sibling
    assert await async_setup_component(hass, "webhook", {})
    before = bound_routes(hass)
    sessions = record_client_sessions(monkeypatch)

    entry = await set_up_legacy(hass, variant, fs_root)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "already owns" in (entry.reason or "")
    assert oauth_routes_added(hass, before) == set()
    assert WEBHOOK_ID not in await _registered_webhooks(hass_ws_client)
    assert all(session.closed for session in sessions)
    assert hass.data[component.OAUTH_ROUTE_OWNER_KEY] == sibling


async def test_sibling_claiming_the_routes_during_key_load_is_refused(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The signing key loads in the executor; the sibling's setup can claim
    the routes in that window, after the first ownership check passed. The
    claim is seen and setup refuses rather than binding shadowed views."""
    sibling = _sibling_domain(variant)
    load_key = component.oauth.load_or_create_secret

    def sibling_claims_while_the_key_loads() -> bytes:
        hass.data[component.OAUTH_ROUTE_OWNER_KEY] = sibling
        key: bytes = load_key()
        return key

    monkeypatch.setattr(
        component.oauth, "load_or_create_secret", sibling_claims_while_the_key_loads
    )
    assert await async_setup_component(hass, "webhook", {})
    before = bound_routes(hass)
    sessions = record_client_sessions(monkeypatch)

    entry = await set_up_legacy(hass, variant, fs_root)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "claimed" in (entry.reason or "")
    assert oauth_routes_added(hass, before) == set()
    assert WEBHOOK_ID not in await _registered_webhooks(hass_ws_client)
    assert all(session.closed for session in sessions)
    assert hass.data[component.OAUTH_ROUTE_OWNER_KEY] == sibling


async def test_enabling_oauth_mid_session_raises_the_restart_repair(
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """The root views bound mid-session are not live until Home Assistant
    restarts, so the user gets the click-to-restart Repair, and the marker
    keeps it across an aborted restart."""
    entry = await set_up_legacy(hass, variant, fs_root)

    assert entry.state is ConfigEntryState.LOADED
    issue = _restart_issue(issue_registry, component, variant)
    assert issue is not None
    assert issue.is_fixable
    assert state_path(fs_root, variant.oauth_marker).exists()


async def test_enabling_oauth_at_boot_clears_a_stale_restart_repair(
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    marker = _seed_stale_restart_repair(hass, component, variant, fs_root)

    await set_up_legacy(hass, variant, fs_root, at_boot=True)

    assert _restart_issue(issue_registry, component, variant) is None
    assert not marker.exists()


async def test_oauth_off_clears_a_stale_restart_repair_mid_session(
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """With OAuth turned off nothing waits on a restart, even mid-session."""
    marker = _seed_stale_restart_repair(hass, component, variant, fs_root)
    write_proxy_config(
        fs_root, variant, {"target_url": UNUSED_TARGET, "webhook_id": WEBHOOK_ID}
    )

    await set_up_proxy(hass, variant)

    assert _restart_issue(issue_registry, component, variant) is None
    assert not marker.exists()


async def test_reload_with_unchanged_credentials_reuses_the_live_views(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    issue_registry: ir.IssueRegistry,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """A benign mid-session reload must not ask for a restart: the views bound
    at boot already serve these credentials. Nothing is bound twice, and the
    root /authorize still answers as the legacy server (it 404s otherwise)."""
    entry = await set_up_legacy(hass, variant, fs_root, at_boot=True)
    routes = bound_routes(hass)
    marker = _seed_stale_restart_repair(hass, component, variant, fs_root)

    await reload_with(hass, variant, fs_root, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert _restart_issue(issue_registry, component, variant) is None
    assert not marker.exists()
    assert bound_routes(hass) == routes
    client = await hass_client_no_auth()
    response = await client.get("/authorize")
    assert response.status == 400
    assert "unsupported_response_type" in await response.text()


async def test_issued_token_still_opens_the_webhook_after_a_reload(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
) -> None:
    """The signing key persists on disk, so the provider a reload (or a Home
    Assistant restart) builds still honours the tokens clients hold."""
    target = upstream.target_url()
    entry = await set_up_legacy(hass, variant, fs_root, at_boot=True, target_url=target)
    client = await hass_client_no_auth()
    tokens = await obtain_tokens(client)

    await reload_with(hass, variant, fs_root, entry, target_url=target)
    response = await post_webhook(client, f"Bearer {tokens['access_token']}")

    assert response.status == 200
    assert len(upstream.requests) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_regenerated_credentials_mid_session_raise_the_restart_repair(
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """After a credential regeneration the bound /authorize and /token still
    serve the old identity while the webhook checks the new one, so no client
    can get a token the webhook accepts until Home Assistant restarts."""
    entry = await set_up_legacy(hass, variant, fs_root, at_boot=True)

    await reload_with(
        hass,
        variant,
        fs_root,
        entry,
        client_id="client-id-REGENERATED-0001",
        client_secret="regenerated-secret",
    )

    assert entry.state is ConfigEntryState.LOADED
    assert _restart_issue(issue_registry, component, variant) is not None
    assert state_path(fs_root, variant.oauth_marker).exists()


async def _rotate_webhook_id(
    hass: HomeAssistant, variant: Variant, fs_root: Path
) -> MockConfigEntry:
    """Boot with one webhook id, then reload with a new one mid-session."""
    entry = await set_up_legacy(hass, variant, fs_root, at_boot=True)
    await reload_with(hass, variant, fs_root, entry, webhook_id=ROTATED_WEBHOOK_ID)
    return entry


async def test_rotated_webhook_id_raises_the_restart_repair(
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """The discovery URL embeds the webhook id and Core cannot bind the new
    one before a restart, so the user is asked for one."""
    await _rotate_webhook_id(hass, variant, fs_root)

    assert _restart_issue(issue_registry, component, variant) is not None
    assert (
        f"/.well-known/oauth-protected-resource/api/webhook/{ROTATED_WEBHOOK_ID}"
        not in bound_routes(hass)
    )


async def test_old_discovery_url_stops_answering_after_webhook_id_rotation(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """Whoever held the old id must not learn the new one from the old id's
    discovery URL, which Core keeps routing until a restart."""
    await _rotate_webhook_id(hass, variant, fs_root)
    client = await hass_client_no_auth()

    response = await client.get(
        f"/.well-known/oauth-protected-resource/api/webhook/{WEBHOOK_ID}"
    )

    assert response.status == 404
    assert ROTATED_WEBHOOK_ID not in await response.text()
