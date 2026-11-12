"""The app's check for OAuth-enforcing component code, against the real Core.

With OAuth enabled, the app's ``main()`` asks Core for the component's
authorization-server document before it serves. A missing document tears a
working webhook down and asks for a Core restart, so the check must see the
document whenever Core serves it, and must not give up on one miss.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import ModuleType

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator, WebSocketGenerator

from ._proxy import Upstream
from ._start import App, CoreLink, connect_core, make_app, notifications, run_main
from ._variants import Variant


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


async def _start_with_oauth(hass: HomeAssistant, app: App, upstream: Upstream) -> None:
    app.set_options(mcp_server_url=upstream.target_url(), enable_oauth=True)
    assert await run_main(hass, app) == 0


def _document_requests(core: CoreLink, variant: Variant) -> int:
    """How often the app asked Core for the component's OAuth document."""
    document = ("GET", f"/{variant.domain}/oauth/authorization-server")
    return core.requests.count(document)


async def _webhook_taken_down(
    hass_ws_client: WebSocketGenerator, app: App, variant: Variant
) -> bool:
    """The app read the component as stale code: it took the webhook down
    and waited for the user to restart Core."""
    stale = f"{variant.domain}_oauth_stale" in await notifications(hass_ws_client)
    return stale and app.restarts_awaited == 1


async def test_check_sees_the_oauth_the_running_component_serves(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
) -> None:
    """The app asks Core at the domain in its bundled manifest, and the first
    answer settles it; any other URL would read a working install as stale."""
    waits: list[float] = []
    app.clock.during_wait = waits.append

    await _start_with_oauth(hass, app, upstream)

    assert _document_requests(core, variant) == 1
    assert waits == []
    assert app.restarts_awaited == 0


async def test_check_reports_missing_oauth_after_bounded_retries(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_access_token: str,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
    socket_enabled: None,
    app: App,
    upstream: Upstream,
    variant: Variant,
) -> None:
    """Core serves nothing of the component (it has not loaded it): after a
    few attempts (a single hiccup must not tear the webhook down) the app
    takes the webhook down and waits for a restart."""
    core = await connect_core(app, hass, hass_client, hass_access_token, monkeypatch)

    await _start_with_oauth(hass, app, upstream)

    assert _document_requests(core, variant) == 3
    assert await _webhook_taken_down(hass_ws_client, app, variant)


async def test_check_outlasts_the_down_verdict_left_by_a_stopped_app(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
) -> None:
    """A client that calls before the starting app renews the heartbeat (a
    clean stop deleted it) leaves the component caching "app down" for a
    moment. The app's check must outwait that cache, or it tears down a
    healthy webhook (#2219)."""
    app.start.HEARTBEAT_FILE.unlink()
    document = f"{variant.oauth_base}/authorization-server"
    early_calls: list[int] = []

    def client_calls_once(method: str, path: str) -> None:
        if path == "/services/persistent_notification/dismiss" and not early_calls:
            early_calls.append(core.get(document))

    core.on_request = client_calls_once
    # The check's wait between attempts really passes.
    app.clock.during_wait = time.sleep

    await _start_with_oauth(hass, app, upstream)

    assert early_calls == [404]
    assert _document_requests(core, variant) == 2
    assert app.restarts_awaited == 0


async def test_check_without_the_bundled_manifest_reports_missing_oauth(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_access_token: str,
    hass_ws_client: WebSocketGenerator,
    socket_enabled: None,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream: Upstream,
) -> None:
    """Without the manifest the app cannot tell which document to ask for, so
    it fails closed rather than serve a webhook that may be unauthenticated."""
    app = make_app(variant, fs_root, monkeypatch, ships_component=False, installed=True)
    core = await connect_core(app, hass, hass_client, hass_access_token, monkeypatch)

    await _start_with_oauth(hass, app, upstream)

    assert _document_requests(core, variant) == 0
    assert await _webhook_taken_down(hass_ws_client, app, variant)
    assert "Could not read integration manifest" in app.err()
