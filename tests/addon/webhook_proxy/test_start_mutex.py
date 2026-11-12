"""Only one flavor may run: both would own Core's root OAuth /authorize and
/token routes. Each start is the app's ``main()``; the refusal and its
banner go through the real Core."""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

import pytest
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator, WebSocketGenerator

from ._proxy import Upstream
from ._start import App, CoreLink, connect_core, make_app, notifications, run_main
from ._variants import VARIANTS, Variant


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


async def test_refuses_and_tells_the_user_while_the_sibling_runs(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    app: App,
    core: CoreLink,
    variant: Variant,
) -> None:
    # Supervisor hash-prefixes third-party slugs.
    app.supervisor.addons = [
        {"slug": f"abc123_{variant.sibling_base}", "state": "started"}
    ]

    assert await run_main(hass, app) == 1

    assert variant.mutex_id in await notifications(hass_ws_client)
    assert not app.path(variant.config_file).exists()


@pytest.mark.parametrize("sibling", ["stopped", "absent"])
async def test_starts_quietly_without_a_running_sibling(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
    sibling: str,
) -> None:
    if sibling == "stopped":
        app.supervisor.addons = [
            {"slug": f"abc123_{variant.sibling_base}", "state": "stopped"}
        ]
    app.set_options(mcp_server_url=upstream.target_url())

    assert await run_main(hass, app) == 0

    assert await notifications(hass_ws_client) == {}


async def test_starts_loudly_when_the_supervisor_cannot_say(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    app: App,
    core: CoreLink,
    upstream: Upstream,
) -> None:
    """The check retries a few times, spaced out, then starts anyway (the
    integration's route-owner guard is the backstop) but says so in the log."""
    app.supervisor.reachable = False
    app.set_options(mcp_server_url=upstream.target_url())
    waits: list[float] = []
    app.clock.during_wait = waits.append

    assert await run_main(hass, app) == 0

    assert app.supervisor.gets == ["/addons"] * 3
    assert len(waits) == 2
    assert "Could not query the Supervisor /addons list" in app.err()
    assert await notifications(hass_ws_client) == {}


async def test_clean_start_clears_both_flavors_refusal_banners(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    app: App,
    core: CoreLink,
    variant: Variant,
    upstream: Upstream,
) -> None:
    """Once the user resolves the conflict, neither flavor's stale "refused
    to start" banner may linger."""
    sibling = next(v for v in VARIANTS if v.key != variant.key)
    for flavor in (variant, sibling):
        persistent_notification.async_create(
            hass, "refused", notification_id=flavor.mutex_id
        )
    app.set_options(mcp_server_url=upstream.target_url())

    assert await run_main(hass, app) == 0

    remaining = await notifications(hass_ws_client)
    assert variant.mutex_id not in remaining
    assert sibling.mutex_id not in remaining
