"""The secrets the app keeps across restarts: OAuth client credentials, the
webhook id, the signing key and the proxy config handed to the component.

Each start is the app's ``main()`` against the real Core. What it keeps is
read where the user and the component meet it: ``/data`` and the config file
the app hands the component.
"""

from __future__ import annotations

import importlib
import json
import logging
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._proxy import Upstream
from ._start import (
    App,
    CoreLink,
    connect_core,
    file_mode,
    make_app,
    no_restricted_create_under,
    run_main,
)
from ._variants import Variant

OLD_CREDS = {"client_id": "old-id-1234567890", "client_secret": "old-secret"}


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


async def _legacy_start(
    hass: HomeAssistant, app: App, upstream: Upstream, **options: Any
) -> tuple[str, str]:
    """Start the app with legacy OAuth on; the client credentials it handed
    the component (the ones it prints for the user's connector)."""
    app.set_options(
        mcp_server_url=upstream.target_url(),
        enable_oauth=True,
        oauth_mode="legacy",
        **options,
    )
    assert await run_main(hass, app) == 0
    oauth = app.proxy_config()["oauth"]
    return oauth["client_id"], oauth["client_secret"]


async def _plain_start(hass: HomeAssistant, app: App, upstream: Upstream) -> str:
    """Start the app with OAuth off; the webhook id it handed the component."""
    app.set_options(mcp_server_url=upstream.target_url())
    assert await run_main(hass, app) == 0
    webhook_id: str = app.proxy_config()["webhook_id"]
    return webhook_id


def _store_old_creds(app: App) -> None:
    """The credentials an earlier start left in /data."""
    app.path("/data/oauth_creds.json").write_text(json.dumps(OLD_CREDS))


async def test_configured_credentials_are_used_trimmed(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    creds = await _legacy_start(
        hass,
        app,
        upstream,
        oauth_client_id="  user-supplied-id-1234567890  ",
        oauth_client_secret="  pw  ",
    )

    assert creds == ("user-supplied-id-1234567890", "pw")


async def test_blank_credentials_are_generated_strong_enough_to_start(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    """A generated client id passes the app's 16-character floor (the start
    succeeds) and the secret carries 256 bits (43 base64url characters)."""
    client_id, secret = await _legacy_start(hass, app, upstream)

    assert len(client_id) >= 16
    assert len(secret) >= 43


async def test_generated_credentials_survive_a_restart(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    """A connector set up with generated credentials keeps working after the
    app restarts."""
    first = await _legacy_start(hass, app, upstream)

    assert await _legacy_start(hass, app, upstream) == first


async def test_configured_credentials_replace_the_stored_ones(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    await _legacy_start(hass, app, upstream)
    configured = await _legacy_start(
        hass,
        app,
        upstream,
        oauth_client_id="user-id-1234567890123",
        oauth_client_secret="user-chosen",
    )

    assert configured == ("user-id-1234567890123", "user-chosen")
    # A later start with the fields cleared keeps what the user last set.
    assert await _legacy_start(hass, app, upstream) == configured


async def test_rotating_only_the_secret_keeps_the_stored_client_id(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    client_id, _secret = await _legacy_start(hass, app, upstream)

    rotated = await _legacy_start(
        hass, app, upstream, oauth_client_secret="rotated-secret"
    )

    assert rotated == (client_id, "rotated-secret")


async def test_unreadable_stored_credentials_are_replaced_with_a_warning(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
) -> None:
    app.path("/data/oauth_creds.json").write_text("not valid json{{{")

    client_id, secret = await _legacy_start(hass, app, upstream)

    assert len(client_id) >= 16
    assert len(secret) >= 43
    assert "Could not read existing OAuth creds" in app.err()


async def test_stored_credentials_file_is_private(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    await _legacy_start(hass, app, upstream)

    assert file_mode(app.path("/data/oauth_creds.json")) == 0o600


async def test_credentials_persist_with_a_warning_where_0600_is_unavailable(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
) -> None:
    """tmpfs or a non-POSIX filesystem cannot honour a restricted create; the
    credentials are still kept, and the wider permissions are reported."""
    with no_restricted_create_under(app.path("/data")):
        creds = await _legacy_start(hass, app, upstream)

    assert await _legacy_start(hass, app, upstream) == creds
    assert (
        "Could not create the OAuth creds file with restricted permissions" in app.err()
    )


async def test_regenerate_gives_new_credentials(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    _store_old_creds(app)

    client_id, secret = await _legacy_start(
        hass, app, upstream, regenerate_oauth_creds=True
    )

    assert client_id != OLD_CREDS["client_id"]
    assert secret != OLD_CREDS["client_secret"]


async def test_regenerate_without_stored_credentials_is_harmless(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
) -> None:
    """Nothing to wipe is no error: the app starts with fresh credentials."""
    client_id, secret = await _legacy_start(
        hass, app, upstream, regenerate_oauth_creds=True
    )

    stored = json.loads(app.path("/data/oauth_creds.json").read_text())
    assert stored == {"client_id": client_id, "client_secret": secret}
    assert "Failed to wipe" not in app.err()


async def test_regenerate_toggle_is_switched_off_keeping_other_options(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    """Left on, the toggle would regenerate the credentials on every restart."""
    await _legacy_start(
        hass,
        app,
        upstream,
        remote_url="https://example.com",
        regenerate_oauth_creds=True,
    )

    options = json.loads(app.path("/data/options.json").read_text())
    assert options["remote_url"] == "https://example.com"
    assert app.supervisor.posts == [
        (
            "/addons/self/options",
            {"options": {**options, "regenerate_oauth_creds": False}},
        )
    ]


async def test_webhook_id_is_generated_once_and_kept(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    """The webhook URL the user pasted into their MCP client must not change
    across restarts."""
    webhook_id = await _plain_start(hass, app, upstream)

    assert len(webhook_id) > 10
    assert await _plain_start(hass, app, upstream) == webhook_id


async def test_webhook_id_from_an_earlier_install_is_kept(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    app.path("/data/webhook_id.txt").write_text("mcp_existing_test_id_12345")

    assert await _plain_start(hass, app, upstream) == "mcp_existing_test_id_12345"


async def test_empty_webhook_id_file_gets_a_new_id(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    app.path("/data/webhook_id.txt").write_text("")

    webhook_id = await _plain_start(hass, app, upstream)

    assert len(webhook_id) > 10
    assert await _plain_start(hass, app, upstream) == webhook_id


async def test_proxy_config_handed_to_the_component_is_private(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    """The proxy config carries the OAuth client secret when legacy OAuth is on."""
    await _legacy_start(hass, app, upstream)

    assert file_mode(app.path(app.variant.config_file)) == 0o600


async def test_proxy_config_is_written_with_a_warning_where_0600_is_unavailable(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
) -> None:
    with no_restricted_create_under(app.path("/config")):
        webhook_id = await _plain_start(hass, app, upstream)

    assert app.proxy_config() == {
        "target_url": upstream.target_url(),
        "webhook_id": webhook_id,
    }
    assert "Could not create the proxy config file" in app.err()


def _oauth(component: ModuleType) -> ModuleType:
    return importlib.import_module(f"{component.__name__}.oauth")


def test_signing_key_file_is_private(component: ModuleType) -> None:
    oauth = _oauth(component)

    oauth.load_or_create_secret()

    assert file_mode(oauth.SECRET_FILE) == 0o600


def test_regenerated_signing_key_file_is_made_private_again(
    component: ModuleType,
) -> None:
    """A short (invalid) key left world-readable is regenerated, and the
    regenerated file is 0600: an overwrite in place would keep the old mode."""
    oauth = _oauth(component)
    oauth.SECRET_FILE.write_bytes(b"short")
    oauth.SECRET_FILE.chmod(0o644)

    oauth.load_or_create_secret()

    assert file_mode(oauth.SECRET_FILE) == 0o600


def test_signing_key_persists_with_a_warning_where_0600_is_unavailable(
    component: ModuleType, fs_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    oauth = _oauth(component)

    with (
        no_restricted_create_under(fs_root),
        caplog.at_level(logging.WARNING, logger=oauth.__name__),
    ):
        secret = oauth.load_or_create_secret()

    assert len(secret) == 32
    assert oauth.SECRET_FILE.read_bytes() == secret
    assert any("restricted permissions" in r.getMessage() for r in caplog.records)
