"""When the app refuses to start rather than serve something the user did
not ask for."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator, WebSocketGenerator

from ._proxy import Upstream
from ._start import App, connect_core, make_app, notifications, run_main
from ._variants import Variant


@pytest.fixture
def app(variant: Variant, fs_root: Path, monkeypatch: pytest.MonkeyPatch) -> App:
    return make_app(variant, fs_root, monkeypatch)


def test_unreadable_options_refuse_to_start(app: App, variant: Variant) -> None:
    """With the options unreadable the app cannot tell whether OAuth is on;
    falling back to the defaults would serve the webhook unauthenticated."""
    app.path("/data/options.json").write_text("{ this is not valid json ")

    assert app.start.main() == 1

    assert not app.path(variant.config_file).exists()
    err = app.err()
    assert "refusing to start" in err
    assert "Could not read the add-on configuration" in err


def test_too_short_client_id_refuses_to_start(app: App) -> None:
    app.set_options(
        mcp_server_url="http://127.0.0.1:9583/private_aaaaaaaaaaaaaaaa",
        enable_oauth=True,
        oauth_client_id="too-short",
        oauth_client_secret="some-secret-here-with-length",
    )

    assert app.start.main() == 1

    assert "OAuth Client ID is too short" in app.err()


async def test_stuck_regenerate_toggle_refuses_and_tells_the_user(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_access_token: str,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
    socket_enabled: None,
    component: ModuleType,
    app: App,
    upstream: Upstream,
    variant: Variant,
) -> None:
    """If the Supervisor will not switch "Regenerate OAuth Credentials" back
    off, every restart would issue new credentials and lock the connector
    out; the app stops and posts a notification asking for the manual fix."""
    await connect_core(app, hass, hass_client, hass_access_token, monkeypatch)
    app.supervisor.accepts_options = False
    app.set_options(
        mcp_server_url=upstream.target_url(),
        enable_oauth=True,
        oauth_mode="legacy",
        regenerate_oauth_creds=True,
    )

    assert await run_main(hass, app) == 1

    assert f"{variant.domain}_regen_stuck" in await notifications(hass_ws_client)
    assert not app.path(variant.config_file).exists()
