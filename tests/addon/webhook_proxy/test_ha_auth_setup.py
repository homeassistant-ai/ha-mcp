"""ha_auth mode set up by Core: Home Assistant is the authorization server.

The proxy only serves the discovery documents, the scoped OAuth routes and a
bearer check against ``hass.auth``. These tests cover what setup, reload, a
mode switch and a refused configuration leave behind on Home Assistant's real
HTTP router, Repairs and the webhook.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import yaml
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._auth_modes import (
    HA_AUTH,
    LEGACY,
    WEBHOOK_ID,
    as_document_urls,
    fetch_json,
    marker_path,
    pkce_pair,
    prm_url,
    proxy_config,
    reload_proxy,
    route_counts,
    scoped_urls,
    submodule,
)
from ._proxy import (
    Upstream,
    record_client_sessions,
    restart_issue,
    start_proxy,
)
from ._start import connect_core, entry_states, in_core, make_app, run_main
from ._variants import Variant

PINNED_LEGACY = "https://pinned-legacy.example"


async def test_every_offered_oauth_mode_sets_the_integration_up_in_that_mode(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_access_token: str,
    socket_enabled: None,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream: Upstream,
) -> None:
    """Each ``oauth_mode`` the app options offer must survive the app's start
    (its mode resolution and the config file it writes) and set the
    integration up in that mode; a literal that drifts between the three
    makes the option a silent no-op."""
    config_yaml = yaml.safe_load((variant.addon_dir / "config.yaml").read_text())
    match = re.fullmatch(r"list\((.+)\)\?", config_yaml["schema"]["oauth_mode"])
    assert match is not None
    offered = match.group(1).split("|")
    # No default: an upgrading install must never inherit a mode it never chose.
    assert "oauth_mode" not in config_yaml["options"]
    label = yaml.safe_load(
        (variant.addon_dir / "translations" / "en.yaml").read_text()
    )["configuration"]["oauth_mode"]
    assert label.get("name") and label.get("description")

    # What each mode's authorization-server document tells a client to do.
    client_auth = {
        "ha_auth": ["none"],
        "legacy": ["client_secret_basic", "client_secret_post"],
    }
    assert sorted(offered) == sorted(client_auth)
    app = make_app(variant, fs_root, monkeypatch, installed=True)
    core = await connect_core(app, hass, hass_client, hass_access_token, monkeypatch)
    document = as_document_urls(variant)[0]
    seen: list[tuple[list[str], tuple[int, Any]]] = []
    for option in offered:
        app.set_options(
            mcp_server_url=upstream.target_url(),
            enable_oauth=True,
            oauth_mode=option,
            oauth_client_id="client-1234567890ABCDEF",
            oauth_client_secret="secret-much-secret",
            remote_url="https://myhome.example",
        )
        app.clock.while_running = lambda: seen.append(
            (
                in_core(hass, lambda: entry_states(hass, variant)),
                core.get_json(document),
            )
        )

        assert await run_main(hass, app) == 0, option

        states, (status, doc) = seen[-1]
        assert states == ["loaded"], option
        assert status == 200, option
        assert doc["token_endpoint_auth_methods_supported"] == client_auth[option]


async def test_ha_auth_binds_scoped_oauth_surface_but_no_root_views(
    hass: HomeAssistant, component: ModuleType, variant: Variant, fs_root: Path
) -> None:
    """ha_auth binds every discovery URL and scoped route once, never the
    legacy root /authorize and /token (so it needs no restart), and clears a
    restart Repair left over from an earlier change."""
    marker = marker_path(fs_root, variant)
    marker.write_text('{"reason": "stale"}')

    entry = await start_proxy(hass, variant, fs_root, proxy_config(oauth=HA_AUTH))

    assert entry.state is ConfigEntryState.LOADED
    routes = route_counts(hass)
    for url in [*as_document_urls(variant), prm_url(), *scoped_urls(variant)]:
        assert routes[url] == 1, url
    assert routes["/authorize"] == routes["/token"] == 0
    # The root-route ownership claim is legacy's: ha_auth must not block the
    # other flavor's legacy mode from binding them.
    assert component.OAUTH_ROUTE_OWNER_KEY not in hass.data
    assert not marker.exists()
    assert restart_issue(hass, variant) is None


async def test_dcr_registration_still_resolves_after_a_reload(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """A client registered through DCR keeps working after the entry reloads:
    the signing key persists, so its client_id still translates to the
    redirect origin core accepts instead of reaching core unrecognised."""
    entry = await start_proxy(hass, variant, fs_root, proxy_config(oauth=HA_AUTH))
    client = await hass_client_no_auth()
    response = await client.post(
        f"{variant.oauth_base}/register",
        json={"redirect_uris": ["https://client.example/cb"]},
    )
    assert response.status == 201
    client_id = (await response.json())["client_id"]

    assert await reload_proxy(
        hass, fs_root, variant, entry, proxy_config(oauth=HA_AUTH)
    )

    _verifier, challenge = pkce_pair()
    response = await client.get(
        f"{variant.oauth_base}/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://client.example/cb",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st",
        },
        allow_redirects=False,
    )
    assert response.status == 302
    location = urlparse(response.headers["Location"])
    assert location.path == "/auth/authorize"
    assert parse_qs(location.query)["client_id"] == ["https://client.example"]


async def test_ha_auth_unload_closes_relay_and_cimd_sessions(
    hass: HomeAssistant,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Anonymous CIMD lookups run on their own session, apart from the relay's
    pool, and unloading the entry closes both (no leaked connector)."""
    sessions = record_client_sessions(monkeypatch)
    entry = await start_proxy(hass, variant, fs_root, proxy_config(oauth=HA_AUTH))
    data = hass.data[variant.domain]
    assert data["cimd_session"] is not data["session"]

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert sessions
    assert all(session.closed for session in sessions)


async def test_ha_auth_setup_failure_refuses_to_leave_the_webhook_open(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user opted into auth: if ha_auth cannot be enabled, setup fails,
    the webhook is torn down rather than left unauthenticated, and both
    sessions are closed."""

    def fail(*_args: Any) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(submodule(component, "oauth"), "register_metadata_views", fail)
    sessions = record_client_sessions(monkeypatch)

    entry = await start_proxy(
        hass,
        variant,
        fs_root,
        proxy_config(target_url=upstream.target_url(), oauth=HA_AUTH),
    )

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason is not None and "Failed to enable OAuth" in entry.reason
    assert variant.domain not in hass.data
    assert sessions
    assert all(session.closed for session in sessions)
    client = await hass_client_no_auth()
    await client.post(f"/api/webhook/{WEBHOOK_ID}", data=b"{}")
    assert upstream.requests == []


async def test_stray_public_base_url_does_not_pin_ha_auth_discovery(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """A hand-edited ha_auth config still carrying legacy's public_base_url
    must not pin the documents: ha_auth serves every hostname the install is
    reached by, so its URLs follow the request host."""
    await start_proxy(
        hass,
        variant,
        fs_root,
        proxy_config(oauth=HA_AUTH, public_base_url="https://stray-pinned.example"),
    )
    client = await hass_client_no_auth()

    status, doc = await fetch_json(
        client, as_document_urls(variant)[0], host="host-abc.example"
    )

    assert status == 200
    assert doc["issuer"] == f"http://host-abc.example{variant.oauth_base}"
    assert doc["token_endpoint_auth_methods_supported"] == ["none"]
    assert "stray-pinned.example" not in json.dumps(doc)


async def test_ha_auth_reload_reuses_the_bound_views(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """A reload must not bind the OAuth views again: Home Assistant cannot
    drop the first binding, so every reload would stack shadowed duplicates."""
    entry = await start_proxy(hass, variant, fs_root, proxy_config(oauth=HA_AUTH))

    assert await reload_proxy(
        hass, fs_root, variant, entry, proxy_config(oauth=HA_AUTH)
    )

    routes = route_counts(hass)
    for url in [*as_document_urls(variant), prm_url(), *scoped_urls(variant)]:
        assert routes[url] == 1, url
    client = await hass_client_no_auth()
    status, _doc = await fetch_json(client, as_document_urls(variant)[0])
    assert status == 200
    assert restart_issue(hass, variant) is None


async def test_rotated_webhook_id_under_ha_auth_raises_restart_repair(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """The discovery URL embeds the webhook id and Home Assistant cannot bind
    one for a new id until it restarts, so a rotation raises the restart
    Repair, and the old id's URL stops answering."""
    entry = await start_proxy(hass, variant, fs_root, proxy_config(oauth=HA_AUTH))
    assert restart_issue(hass, variant) is None

    rotated = proxy_config(oauth=HA_AUTH, webhook_id="mcp_rotated_webhook")
    assert await reload_proxy(hass, fs_root, variant, entry, rotated)

    assert restart_issue(hass, variant) is not None
    assert marker_path(fs_root, variant).exists()
    assert route_counts(hass)[prm_url("mcp_rotated_webhook")] == 0
    client = await hass_client_no_auth()
    status, _body = await fetch_json(client, prm_url())
    assert status == 404


async def test_switch_from_ha_auth_to_legacy_serves_pinned_legacy_documents(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """ha_auth -> legacy binds only legacy's two root routes, and the views
    ha_auth bound now serve legacy's document pinned to its public URL; the
    request host cannot poison it."""
    hass.set_state(CoreState.not_running)  # legacy root views bind at boot
    entry = await start_proxy(hass, variant, fs_root, proxy_config(oauth=HA_AUTH))

    legacy = proxy_config(oauth=LEGACY, public_base_url=PINNED_LEGACY)
    assert await reload_proxy(hass, fs_root, variant, entry, legacy)

    routes = route_counts(hass)
    for url in ["/authorize", "/token", *as_document_urls(variant), prm_url()]:
        assert routes[url] == 1, url
    client = await hass_client_no_auth()
    status, doc = await fetch_json(
        client, as_document_urls(variant)[0], host="other-host.example"
    )
    assert status == 200
    assert doc["issuer"] == f"{PINNED_LEGACY}{variant.oauth_base}"
    assert doc["authorization_endpoint"] == (
        f"{PINNED_LEGACY}{variant.oauth_base}/authorize"
    )
    assert "client_secret_basic" in doc["token_endpoint_auth_methods_supported"]
    assert "client_id_metadata_document_supported" not in doc
    assert restart_issue(hass, variant) is None


async def test_switch_from_legacy_to_ha_auth_serves_host_derived_documents(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """legacy -> ha_auth binds only the DCR route legacy lacks, and the views
    legacy bound now serve ha_auth's document built from the request host,
    never legacy's pinned URL."""
    hass.set_state(CoreState.not_running)
    entry = await start_proxy(
        hass,
        variant,
        fs_root,
        proxy_config(oauth=LEGACY, public_base_url=PINNED_LEGACY),
    )
    assert route_counts(hass)[f"{variant.oauth_base}/register"] == 0

    assert await reload_proxy(
        hass, fs_root, variant, entry, proxy_config(oauth=HA_AUTH)
    )

    routes = route_counts(hass)
    for url in ["/authorize", "/token", *as_document_urls(variant), prm_url()]:
        assert routes[url] == 1, url
    assert routes[f"{variant.oauth_base}/register"] == 1
    client = await hass_client_no_auth()
    status, doc = await fetch_json(
        client, as_document_urls(variant)[0], host="switch-host.example"
    )
    assert status == 200
    assert doc["issuer"] == f"http://switch-host.example{variant.oauth_base}"
    assert doc["authorization_endpoint"] == (
        f"http://switch-host.example{variant.oauth_base}/authorize"
    )
    assert doc["token_endpoint_auth_methods_supported"] == ["none"]
    assert doc["client_id_metadata_document_supported"] is True


@pytest.mark.parametrize(
    ("oauth", "reason"),
    [
        pytest.param({"mode": "bogus"}, "Unknown OAuth mode", id="unknown-mode"),
        pytest.param(
            {**LEGACY, "mode": "ha_auth"}, "Ambiguous", id="ha_auth-with-credentials"
        ),
    ],
)
async def test_unusable_oauth_section_refuses_setup_and_closes_the_webhook(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
    monkeypatch: pytest.MonkeyPatch,
    oauth: dict[str, Any],
    reason: str,
) -> None:
    """An OAuth section the proxy cannot act on unambiguously (an unknown
    mode, or ha_auth still carrying legacy credentials) is refused rather
    than guessed: setup fails, no OAuth view is bound, the webhook is torn
    down and the session closed."""
    sessions = record_client_sessions(monkeypatch)

    entry = await start_proxy(
        hass,
        variant,
        fs_root,
        proxy_config(target_url=upstream.target_url(), oauth=oauth),
    )

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.reason is not None and reason in entry.reason
    assert variant.domain not in hass.data
    assert sessions
    assert all(session.closed for session in sessions)
    routes = route_counts(hass)
    assert all(routes[url] == 0 for url in as_document_urls(variant))
    client = await hass_client_no_auth()
    await client.post(f"/api/webhook/{WEBHOOK_ID}", data=b"{}")
    assert upstream.requests == []


async def test_credentials_without_a_mode_stay_on_legacy(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> None:
    """A config file from before ha_auth existed (credentials, no mode key)
    keeps the legacy embedded server rather than switching an existing user."""
    hass.set_state(CoreState.not_running)
    oauth = {key: value for key, value in LEGACY.items() if key != "mode"}

    entry = await start_proxy(hass, variant, fs_root, proxy_config(oauth=oauth))

    assert entry.state is ConfigEntryState.LOADED
    assert hass.data[component.OAUTH_ROUTE_OWNER_KEY] == variant.domain
    client = await hass_client_no_auth()
    status, doc = await fetch_json(client, as_document_urls(variant)[0])
    assert status == 200
    assert "client_secret_basic" in doc["token_endpoint_auth_methods_supported"]
