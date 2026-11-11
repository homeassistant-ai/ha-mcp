"""How the app finds the ha-mcp server it relays to, and its public URL.

The Supervisor's answers are typed in (``_start.Supervisor``): the Supervisor
is not Core and has no test double.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ._start import App, make_app, server_app_slugs
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


@pytest.fixture
def app(variant: Variant, fs_root: Path, monkeypatch: pytest.MonkeyPatch) -> App:
    return make_app(variant, fs_root, monkeypatch)


def _running(ip: str, **extra: object) -> dict[str, object]:
    return {"state": "started", "ip_address": ip, "options": {}, **extra}


@pytest.mark.parametrize("prefix", ["", "abc12345_"], ids=["official", "hashed"])
def test_prefers_the_stable_server_when_both_run(app: App, prefix: str) -> None:
    """Supervisor hash-prefixes third-party slugs; either way the stable
    server wins over dev."""
    stable, dev = (prefix + slug for slug in server_app_slugs())
    app.supervisor.addons = [{"slug": dev}, {"slug": stable}]
    app.supervisor.info = {
        stable: _running("172.30.33.1"),
        dev: _running("172.30.33.2"),
    }

    slug, ip, _info = app.step("_discover_addon")()

    assert (slug, ip) == (stable, "172.30.33.1")


@pytest.mark.parametrize("stable_state", ["stopped", "absent"])
def test_uses_the_dev_server_when_stable_is_not_running(
    app: App, stable_state: str
) -> None:
    stable, dev = server_app_slugs()
    app.supervisor.addons = [{"slug": dev}]
    app.supervisor.info = {dev: _running("172.30.33.2")}
    if stable_state == "stopped":
        app.supervisor.addons.insert(0, {"slug": stable})
        app.supervisor.info[stable] = {**_running("172.30.33.1"), "state": "stopped"}

    slug, ip, _info = app.step("_discover_addon")()

    assert (slug, ip) == (dev, "172.30.33.2")


def test_finds_nothing_without_a_server_app(app: App) -> None:
    app.supervisor.addons = [{"slug": "some_other_addon"}]

    assert app.step("_discover_addon")() == (None, None, None)


def test_host_network_server_is_reached_on_loopback(app: App) -> None:
    """With ``host_network`` the Supervisor's ip_address is an unreachable
    bridge address; the proxy shares the host network, so loopback works."""
    stable = server_app_slugs()[0]
    app.supervisor.addons = [{"slug": stable}]
    app.supervisor.info = {stable: _running("172.30.32.1", host_network=True)}

    assert app.step("_discover_addon")()[1] == "127.0.0.1"


def test_running_server_without_an_address_is_skipped(app: App) -> None:
    stable = server_app_slugs()[0]
    app.supervisor.addons = [{"slug": stable}]
    app.supervisor.info = {stable: _running("")}

    assert app.step("_discover_addon")()[0] is None


def test_tries_the_official_slugs_when_the_app_list_fails(app: App) -> None:
    stable = server_app_slugs()[0]
    app.supervisor.lists_addons = False
    app.supervisor.info = {stable: _running("172.30.33.1")}

    assert app.step("_discover_addon")()[:2] == (stable, "172.30.33.1")


@pytest.mark.parametrize("option", ["/private_abc123", "private_abc123"])
def test_secret_path_option_wins(app: App, option: str) -> None:
    """The server's ``secret_path`` option is used as given, rooted at /."""
    app.supervisor.logs = {"/addons/ha_mcp/logs": "http://0.0.0.0:9583/private_x\n"}

    path = app.step("_discover_secret_path")(
        "ha_mcp", {"options": {"secret_path": option}}
    )

    assert path == "/private_abc123"


@pytest.mark.parametrize(
    ("log", "expected"),
    [
        (LOG_WITH_SECRET, "/private_zctpwlX7ZkIAr7oqdfLPxw"),
        (LOG_WITH_URL, "/private_WBA1dCWENm_4cuFd6l8JUw"),
    ],
    ids=["secret-line", "server-url"],
)
def test_secret_path_is_read_from_the_server_log(
    app: App, log: str, expected: str
) -> None:
    app.supervisor.logs = {"/addons/ha_mcp/logs": log}

    assert app.step("_discover_secret_path")("ha_mcp", {"options": {}}) == expected


def test_secret_path_falls_back_to_the_latest_log(app: App) -> None:
    """Some Supervisor versions fail ``/logs``; ``/logs/latest`` still works."""
    app.supervisor.logs = {
        "/addons/ha_mcp/logs/latest": "http://0.0.0.0:9583/private_fallback123\n"
    }

    path = app.step("_discover_secret_path")("ha_mcp", {"options": {}})

    assert path == "/private_fallback123"


@pytest.mark.parametrize("log", ["2026-03-05 [INFO] Starting server...\n", None])
def test_no_secret_path_without_option_or_log_line(app: App, log: str | None) -> None:
    if log is not None:
        app.supervisor.logs = {"/addons/ha_mcp/logs": log}

    assert app.step("_discover_secret_path")("ha_mcp", {"options": {}}) is None


def test_configured_server_url_skips_discovery(app: App) -> None:
    target = app.step("_resolve_target_url")(
        "  http://192.168.1.100:9583/private_custom  ", 9583
    )

    assert target == "http://192.168.1.100:9583/private_custom"
    assert app.supervisor.gets == []


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
