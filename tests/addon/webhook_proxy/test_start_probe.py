"""The app's probe for OAuth-enforcing component code, against the real Core.

A False answer tears a working webhook down and asks for a Core restart, so
the probe must see the component's OAuth surface whenever Core serves it.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import ModuleType

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._proxy import Upstream, set_up_proxy, write_proxy_config
from ._start import App, CoreLink, connect_core, make_app
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


async def _set_up_oauth_proxy(
    hass: HomeAssistant, variant: Variant, fs_root: Path, upstream: Upstream
) -> None:
    write_proxy_config(
        fs_root,
        variant,
        {
            "target_url": upstream.target_url(),
            "webhook_id": "mcp_probe_webhook",
            "oauth": {"mode": "ha_auth"},
        },
    )
    await set_up_proxy(hass, variant)


async def _probe(hass: HomeAssistant, app: App) -> bool:
    active: bool = await hass.async_add_executor_job(app.step("_probe_oauth_active"))
    return active


async def test_probe_sees_the_oauth_the_running_component_serves(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
) -> None:
    """The probe asks Core at the domain in the app's bundled manifest; any
    other URL would read a working install as stale code."""
    await _set_up_oauth_proxy(hass, variant, fs_root, upstream)

    assert await _probe(hass, app) is True
    assert app.clock.sleeps == []


async def test_probe_reports_missing_oauth_after_bounded_retries(
    hass: HomeAssistant, app: App, core: CoreLink
) -> None:
    """Nothing serves the component's OAuth documents: after a few spaced
    attempts (a single hiccup must not tear the webhook down) it says so."""
    assert await _probe(hass, app) is False
    assert len(app.clock.sleeps) == 2


async def test_probe_outlasts_the_down_verdict_left_by_a_stopped_app(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    variant: Variant,
    fs_root: Path,
    upstream: Upstream,
) -> None:
    """A client that called while the app was stopped leaves the component
    caching "app down" for a moment. The next start's probe must outwait that
    cache, or it tears down a healthy webhook (#2219)."""
    await _set_up_oauth_proxy(hass, variant, fs_root, upstream)
    app.start.HEARTBEAT_FILE.unlink()
    document = f"{variant.oauth_base}/authorization-server"
    assert await hass.async_add_executor_job(core.get, document) == 404
    app.start.HEARTBEAT_FILE.touch()
    # The probe's wait between attempts really passes.
    app.clock.during_wait = time.sleep

    assert await _probe(hass, app) is True
    assert len(app.clock.sleeps) == 1


def test_probe_without_the_bundled_manifest_reports_missing_oauth(
    variant: Variant,
    fs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    app = make_app(variant, fs_root, monkeypatch, ships_component=False)

    assert app.step("_probe_oauth_active")() is False
    assert len(app.clock.sleeps) == 2
    assert "Could not read integration manifest" in capsys.readouterr().err
