"""The app's start, end to end: ``main()`` against the real Core.

Core sets the component up through the config flow the app drives over
Core's REST API; the Supervisor's answers are typed in.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.typing import ClientSessionGenerator, WebSocketGenerator

from ._proxy import Upstream, set_up_proxy, write_proxy_config
from ._start import (
    App,
    CoreLink,
    connect_core,
    entry_states,
    in_core,
    make_app,
    notifications,
    run_main,
    server_app_slugs,
)
from ._variants import Variant

SECRET = "/private_zctpwlX7ZkIAr7oqdfLPxw"


@pytest.fixture
def app(variant: Variant, fs_root: Path, monkeypatch: pytest.MonkeyPatch) -> App:
    return make_app(variant, fs_root, monkeypatch)


@pytest.fixture
async def core(
    app: App,
    component: ModuleType,
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
    socket_enabled: None,
) -> CoreLink:
    return await connect_core(app, hass, hass_client, hass_access_token, monkeypatch)


async def test_started_app_relays_its_webhook_to_the_discovered_server(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    """With no server URL configured, the app finds the running server app,
    hands Core the address, and the webhook URL reaches that server."""
    port = int(upstream.base_url.rsplit(":", 1)[1])
    stable = server_app_slugs()[0]
    app.supervisor.addons = [{"slug": stable}]
    app.supervisor.info = {
        stable: {
            "state": "started",
            "ip_address": "127.0.0.1",
            "options": {"secret_path": SECRET},
        }
    }
    app.set_options(mcp_port=port)
    app.deploy_component()
    statuses: list[int] = []

    def remote_client_calls() -> None:
        webhook_id = app.path("/data/webhook_id.txt").read_text()
        statuses.append(
            core.post(
                f"/api/webhook/{webhook_id}", b'{"jsonrpc":"2.0","method":"ping"}'
            )
        )

    app.clock.while_running = remote_client_calls

    assert await run_main(hass, app) == 0

    assert statuses == [200]
    assert [request.path for request in upstream.requests] == [SECRET]


@pytest.mark.parametrize("oauth_option", [{}, {"enable_oauth": False}])
async def test_oauth_off_hands_the_component_the_plain_config(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    oauth_option: dict[str, bool],
) -> None:
    """OAuth stays off unless enabled, and the config file keeps the shape
    every earlier version wrote: no OAuth section, no pinned public URL."""
    app.set_options(mcp_server_url=upstream.target_url(), **oauth_option)
    app.deploy_component()

    assert await run_main(hass, app) == 0

    assert set(app.proxy_config()) == {"target_url", "webhook_id"}


async def test_start_after_a_clean_stop_keeps_the_oauth_webhook_up(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
) -> None:
    """A clean stop deletes the heartbeat, which takes the component's OAuth
    views dark. The start must renew it before it probes those views, or it
    misreads every fresh start as stale code and tears the webhook down."""
    app.start.HEARTBEAT_FILE.unlink()
    app.set_options(mcp_server_url=upstream.target_url(), enable_oauth=True)
    app.deploy_component()
    running: list[list[str]] = []
    app.clock.while_running = lambda: running.append(
        in_core(hass, lambda: entry_states(hass, variant))
    )

    assert await run_main(hass, app) == 0

    assert running == [["loaded"]]
    assert app.restarts_awaited == 0
    assert not app.path(variant.oauth_marker).exists()
    assert f"{variant.domain}_oauth_stale" not in await notifications(hass_ws_client)


async def test_ha_auth_start_needs_no_app_credentials(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
) -> None:
    """A first OAuth enable signs in with the Home Assistant login: no client
    credentials are generated or handed over, and the log says to leave the
    connector's OAuth fields blank."""
    app.set_options(mcp_server_url=upstream.target_url(), enable_oauth=True)
    app.deploy_component()

    assert await run_main(hass, app) == 0

    assert app.proxy_config()["oauth"] == {"mode": "ha_auth"}
    assert not app.path("/data/oauth_creds.json").exists()
    out = app.out()
    assert "Home Assistant login" in out
    assert "BLANK" in out
    # Users act on these exact lines.
    assert "Revoke access anytime from your Home Assistant profile's" in out
    assert "any value works — Home Assistant ignores it." in out


async def test_legacy_start_hands_over_and_prints_the_credentials(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
) -> None:
    app.set_options(
        mcp_server_url=upstream.target_url(),
        enable_oauth=True,
        oauth_client_id="client-1234567890ABCDEF",
        oauth_client_secret="secret-much-secret",
    )
    app.deploy_component()

    assert await run_main(hass, app) == 0

    assert app.proxy_config()["oauth"] == {
        "mode": "legacy",
        "client_id": "client-1234567890ABCDEF",
        "client_secret": "secret-much-secret",
    }
    # The lines the user copies into the connector.
    out = app.out()
    assert "OAuth Client ID:     client-1234567890ABCDEF" in out
    assert "OAuth Client Secret: secret-much-secret" in out


async def test_ha_auth_ignores_the_external_url_and_says_so(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
) -> None:
    """ha_auth discovery is derived from the request's host, so a configured
    External URL is not pinned into the config."""
    app.set_options(
        mcp_server_url=upstream.target_url(),
        enable_oauth=True,
        remote_url="https://example.nabu.casa",
    )
    app.deploy_component()

    assert await run_main(hass, app) == 0

    config = app.proxy_config()
    assert config["oauth"] == {"mode": "ha_auth"}
    assert "public_base_url" not in config
    assert "External URL is ignored in ha_auth mode" in app.out()


async def test_updated_component_raises_the_restart_repair(
    hass: HomeAssistant,
    issue_registry: ir.IssueRegistry,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
    fs_root: Path,
) -> None:
    """After an app update the new component files are on disk but Core still
    runs the previous code until it restarts; the loaded component files a
    click-to-restart Repair."""
    write_proxy_config(
        fs_root,
        variant,
        {"target_url": upstream.target_url(), "webhook_id": "mcp_previous_run"},
    )
    await set_up_proxy(hass, variant)
    app.deploy_component(version="0.0.1")
    app.set_options(mcp_server_url=upstream.target_url())

    assert await run_main(hass, app) == 0

    assert issue_registry.async_get_issue(variant.domain, "update_restart_required")
    assert "INTEGRATION UPDATED" in app.out()
    bundled = json.loads((variant.component_dir / "manifest.json").read_text())
    deployed = json.loads((app.deployed_component / "manifest.json").read_text())
    assert deployed["version"] == bundled["version"]
