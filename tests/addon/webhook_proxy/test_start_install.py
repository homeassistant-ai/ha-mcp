"""How the app puts its bundled component into /config/custom_components, and
what it reports: a first install needs a Core restart to load, a new version
needs one to take effect, anything else needs none.

Each start is the app's ``main()`` against the real Core: the restart it
waits for, and the notification it posts, are what the user sees.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator, WebSocketGenerator

from ._proxy import Upstream
from ._start import App, CoreLink, connect_core, make_app, notifications, run_main
from ._variants import Variant


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


async def _start(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    app: App,
    upstream: Upstream,
) -> bool:
    """Start the app; whether it told the user the component was updated."""
    app.set_options(mcp_server_url=upstream.target_url())
    assert await run_main(hass, app) == 0
    return f"{app.variant.domain}_update" in await notifications(hass_ws_client)


def _manifest(component_dir: Path) -> Any:
    return json.loads((component_dir / "manifest.json").read_text())


async def test_first_install_copies_the_component_and_awaits_a_restart(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
) -> None:
    """Core loads a new custom integration only at boot."""
    updated = await _start(hass, hass_ws_client, app, upstream)

    assert _manifest(app.deployed_component) == _manifest(variant.component_dir)
    assert app.restarts_awaited == 1
    assert updated is False


async def test_new_version_is_installed_and_reported(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
) -> None:
    app.deploy_component(version="0.0.1")

    updated = await _start(hass, hass_ws_client, app, upstream)

    assert _manifest(app.deployed_component) == _manifest(variant.component_dir)
    assert updated is True
    assert app.restarts_awaited == 0


async def test_same_version_is_left_alone(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    app: App,
    core: CoreLink,
    upstream: Upstream,
) -> None:
    app.deploy_component()
    untouched = app.deployed_component / "left_in_place.txt"
    untouched.write_text("")

    updated = await _start(hass, hass_ws_client, app, upstream)

    assert untouched.exists()
    assert updated is False
    assert app.restarts_awaited == 0


async def test_corrupt_deployed_manifest_is_repaired_without_a_restart_prompt(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
) -> None:
    """An unreadable deployed manifest says nothing about a version change, so
    the files are refreshed without asking the user to restart Core."""
    app.deploy_component()
    (app.deployed_component / "manifest.json").write_text("not json{{{")

    updated = await _start(hass, hass_ws_client, app, upstream)

    assert _manifest(app.deployed_component) == _manifest(variant.component_dir)
    assert updated is False
    assert app.restarts_awaited == 0
