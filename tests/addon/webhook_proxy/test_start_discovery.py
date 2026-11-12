"""How the app finds the ha-mcp server it relays to, and its public URL.

Each discovery is the app's ``main()`` against the real Core; what it found
is the target URL in the config file it hands the component. The
Supervisor's answers are typed in (``_start.Supervisor``): the Supervisor is
not Core and has no test double. Every server address here is loopback (the
upstream's port), because the running app health-checks the server it found.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._proxy import Upstream
from ._start import App, CoreLink, connect_core, make_app, run_main, server_app_slugs
from ._variants import Variant

LOG_WITH_SECRET = (
    "2026-03-05 12:00:00 [INFO] Starting Home Assistant MCP Server...\n"
    "2026-03-05 12:00:01 [INFO]    Secret Path: /private_zctpwlX7ZkIAr7oqdfLPxw\n"
)
# The line the server actually prints at startup.
LOG_WITH_URL = (
    "Starting MCP server 'ha-mcp' with transport 'http' (stateless) on "
    "http://0.0.0.0:9583/private_WBA1dCWENm_4cuFd6l8JUw\n"
)
STABLE_SECRET = "/private_stable_server_secret"
DEV_SECRET = "/private_dev_server_secret"


@pytest.fixture
def app(variant: Variant, fs_root: Path, monkeypatch: pytest.MonkeyPatch) -> App:
    return make_app(variant, fs_root, monkeypatch, installed=True)


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


def _port(upstream: Upstream) -> int:
    return int(upstream.base_url.rsplit(":", 1)[1])


def _running(secret: str = "", **extra: Any) -> dict[str, Any]:
    options = {"secret_path": secret} if secret else {}
    return {"state": "started", "ip_address": "127.0.0.1", "options": options, **extra}


async def _discovered_target(hass: HomeAssistant, app: App, upstream: Upstream) -> str:
    """Start the app with no server URL configured; the target it found."""
    app.set_options(mcp_port=_port(upstream))
    assert await run_main(hass, app) == 0
    target: str = app.proxy_config()["target_url"]
    return target


async def _start_without_a_server(hass: HomeAssistant, app: App) -> None:
    """The app refuses to start when it finds no server to relay to, and hands
    the component nothing."""
    app.set_options()
    assert await run_main(hass, app) == 1
    assert not app.path(app.variant.config_file).exists()


def _at(upstream: Upstream, secret: str) -> str:
    return f"http://127.0.0.1:{_port(upstream)}{secret}"


@pytest.mark.parametrize("prefix", ["", "abc12345_"], ids=["official", "hashed"])
async def test_prefers_the_stable_server_when_both_run(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream, prefix: str
) -> None:
    """Supervisor hash-prefixes third-party slugs; either way the stable
    server wins over dev."""
    stable, dev = (prefix + slug for slug in server_app_slugs())
    app.supervisor.addons = [{"slug": dev}, {"slug": stable}]
    app.supervisor.info = {stable: _running(STABLE_SECRET), dev: _running(DEV_SECRET)}

    assert await _discovered_target(hass, app, upstream) == _at(upstream, STABLE_SECRET)


@pytest.mark.parametrize("stable_state", ["stopped", "absent"])
async def test_uses_the_dev_server_when_stable_is_not_running(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    stable_state: str,
) -> None:
    stable, dev = server_app_slugs()
    app.supervisor.addons = [{"slug": dev}]
    app.supervisor.info = {dev: _running(DEV_SECRET)}
    if stable_state == "stopped":
        app.supervisor.addons.insert(0, {"slug": stable})
        app.supervisor.info[stable] = {**_running(STABLE_SECRET), "state": "stopped"}

    assert await _discovered_target(hass, app, upstream) == _at(upstream, DEV_SECRET)


async def test_refuses_to_start_without_a_server_app(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
) -> None:
    app.supervisor.addons = [{"slug": "some_other_addon"}]

    await _start_without_a_server(hass, app)

    assert "No running MCP addon found" in app.err()


async def test_host_network_server_is_reached_on_loopback(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    """With ``host_network`` the Supervisor's ip_address is an unreachable
    bridge address; the proxy shares the host network, so loopback works."""
    stable = server_app_slugs()[0]
    app.supervisor.addons = [{"slug": stable}]
    app.supervisor.info = {
        stable: _running(STABLE_SECRET, ip_address="172.30.32.1", host_network=True)
    }

    assert await _discovered_target(hass, app, upstream) == _at(upstream, STABLE_SECRET)


async def test_running_server_without_an_address_is_skipped(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    stable, dev = server_app_slugs()
    app.supervisor.addons = [{"slug": stable}, {"slug": dev}]
    app.supervisor.info = {
        stable: _running(STABLE_SECRET, ip_address=""),
        dev: _running(DEV_SECRET),
    }

    assert await _discovered_target(hass, app, upstream) == _at(upstream, DEV_SECRET)


async def test_tries_the_official_slugs_when_the_app_list_fails(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    stable = server_app_slugs()[0]
    app.supervisor.lists_addons = False
    app.supervisor.info = {stable: _running(STABLE_SECRET)}

    assert await _discovered_target(hass, app, upstream) == _at(upstream, STABLE_SECRET)


@pytest.mark.parametrize("option", ["/private_abc123", "private_abc123"])
async def test_secret_path_option_wins(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream, option: str
) -> None:
    """The server's ``secret_path`` option is used as given, rooted at /."""
    stable = server_app_slugs()[0]
    app.supervisor.addons = [{"slug": stable}]
    app.supervisor.info = {stable: _running(option)}
    app.supervisor.logs = {f"/addons/{stable}/logs": "http://0.0.0.0:9583/private_x\n"}

    target = await _discovered_target(hass, app, upstream)

    assert target == _at(upstream, "/private_abc123")


@pytest.mark.parametrize(
    ("log", "expected"),
    [
        (LOG_WITH_SECRET, "/private_zctpwlX7ZkIAr7oqdfLPxw"),
        (LOG_WITH_URL, "/private_WBA1dCWENm_4cuFd6l8JUw"),
    ],
    ids=["secret-line", "server-url"],
)
async def test_secret_path_is_read_from_the_server_log(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    log: str,
    expected: str,
) -> None:
    stable = server_app_slugs()[0]
    app.supervisor.addons = [{"slug": stable}]
    app.supervisor.info = {stable: _running()}
    app.supervisor.logs = {f"/addons/{stable}/logs": log}

    assert await _discovered_target(hass, app, upstream) == _at(upstream, expected)


async def test_secret_path_falls_back_to_the_latest_log(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    """Some Supervisor versions fail ``/logs``; ``/logs/latest`` still works."""
    stable = server_app_slugs()[0]
    app.supervisor.addons = [{"slug": stable}]
    app.supervisor.info = {stable: _running()}
    app.supervisor.logs = {
        f"/addons/{stable}/logs/latest": "http://0.0.0.0:9583/private_fallback123\n"
    }

    target = await _discovered_target(hass, app, upstream)

    assert target == _at(upstream, "/private_fallback123")


@pytest.mark.parametrize("log", ["2026-03-05 [INFO] Starting server...\n", None])
async def test_refuses_to_start_without_a_secret_path(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    log: str | None,
) -> None:
    """No ``secret_path`` option and no log line naming one."""
    stable = server_app_slugs()[0]
    app.supervisor.addons = [{"slug": stable}]
    app.supervisor.info = {stable: _running()}
    if log is not None:
        app.supervisor.logs = {f"/addons/{stable}/logs": log}

    await _start_without_a_server(hass, app)

    assert "Could not discover secret path" in app.err()


async def test_configured_server_url_skips_discovery(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    app.set_options(mcp_server_url=f"  {upstream.target_url()}  ")

    assert await run_main(hass, app) == 0

    assert app.proxy_config()["target_url"] == upstream.target_url()
    # The one Supervisor call is the start's check for the other flavor.
    assert app.supervisor.gets == ["/addons"]


def _write_cloud_store(app: App, remote_enabled: bool) -> None:
    # Core's cloud preferences store. The cloud integration cannot be imported
    # here (hass_nabucasa is not installed with Core), so its stored shape is
    # written out: Store's envelope and cloud.const's PREF_ENABLE_REMOTE /
    # PREF_REMOTE_DOMAIN keys.
    store = app.path("/config/.storage/cloud")
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps(
            {
                "version": 1,
                "minor_version": 4,
                "key": "cloud",
                "data": {
                    "remote_enabled": remote_enabled,
                    "remote_domain": "abcdef123.ui.nabu.casa",
                },
            }
        )
    )


def test_nabu_casa_remote_url_is_detected(app: App) -> None:
    _write_cloud_store(app, remote_enabled=True)

    assert app.start.get_nabu_casa_url() == "https://abcdef123.ui.nabu.casa"


def test_nabu_casa_url_is_not_used_while_remote_ui_is_off(app: App) -> None:
    _write_cloud_store(app, remote_enabled=False)

    assert app.start.get_nabu_casa_url() is None


def test_no_nabu_casa_url_without_cloud(app: App) -> None:
    assert app.start.get_nabu_casa_url() is None
