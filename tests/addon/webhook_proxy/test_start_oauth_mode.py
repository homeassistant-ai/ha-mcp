"""Which OAuth mode an enabled OAuth starts in: an upgrade never moves an
existing legacy user to ha_auth, which would break their connector.

Each start is the app's ``main()`` against the real Core; the mode is the one
in the config file it hands the component. A first enable with no legacy
trace starting in ha_auth is ``test_start_main``'s
``test_ha_auth_start_needs_no_app_credentials``.
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
from ._start import App, CoreLink, connect_core, make_app, run_main
from ._variants import Variant

LEGACY = "legacy"
HA_AUTH = "ha_auth"
# The two halves of a legacy client a user may have configured.
CONFIGURED_ID = {"oauth_client_id": "cid-1234567890ABCDEF"}
CONFIGURED_SECRET = {"oauth_client_secret": "sec"}


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


async def _mode(
    hass: HomeAssistant, app: App, upstream: Upstream, **options: Any
) -> str:
    """Start the app with OAuth enabled; the mode it set the component up in."""
    app.set_options(mcp_server_url=upstream.target_url(), enable_oauth=True, **options)
    assert await run_main(hass, app) == 0
    mode: str = app.proxy_config()["oauth"]["mode"]
    return mode


def _store_legacy_creds(app: App) -> None:
    app.path("/data/oauth_creds.json").write_text(
        json.dumps({"client_id": "stored-client-1234567890", "client_secret": "stored"})
    )


async def test_explicit_ha_auth_wins_over_legacy_traces(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    _store_legacy_creds(app)

    mode = await _mode(
        hass, app, upstream, oauth_mode=HA_AUTH, **CONFIGURED_ID, **CONFIGURED_SECRET
    )

    assert mode == HA_AUTH


async def test_explicit_legacy_wins_on_a_first_enable(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    assert await _mode(hass, app, upstream, oauth_mode=LEGACY) == LEGACY


async def test_stored_legacy_credentials_keep_legacy(
    hass: HomeAssistant, app: App, core: CoreLink, upstream: Upstream
) -> None:
    _store_legacy_creds(app)

    assert await _mode(hass, app, upstream) == LEGACY


@pytest.mark.parametrize(
    "credential", [CONFIGURED_ID, CONFIGURED_SECRET], ids=["client-id", "client-secret"]
)
async def test_a_configured_legacy_credential_keeps_legacy(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    credential: dict[str, str],
) -> None:
    assert await _mode(hass, app, upstream, **credential) == LEGACY


@pytest.mark.parametrize(
    ("credentials", "expected"),
    [({}, HA_AUTH), ({**CONFIGURED_ID, **CONFIGURED_SECRET}, LEGACY)],
    ids=["first-enable", "legacy-credentials"],
)
async def test_unknown_mode_falls_back_to_detection(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    credentials: dict[str, str],
    expected: str,
) -> None:
    """The Supervisor's enum check normally blocks it; if one gets through,
    the app must still start, in the mode detection picks."""
    assert (
        await _mode(hass, app, upstream, oauth_mode="bogus", **credentials) == expected
    )
