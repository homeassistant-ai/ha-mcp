"""OAuth enabled while Core still runs component code that does not enforce
it (Core was not restarted after an app update): the app takes the webhook
down until Core restarts, then brings it back.

Which component code Core has loaded cannot be staged in a unit test, so the
app's probe for it answers as scripted; everything it does about the answer
goes through the real Core. Restarting Core cannot be staged either:
``App.wait_for_ha_restart`` stands in for the user's restart.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.typing import ClientSessionGenerator, WebSocketGenerator

from ._proxy import Upstream
from ._start import (
    App,
    CoreLink,
    await_in_core,
    connect_core,
    entry_states,
    in_core,
    make_app,
    notifications,
    run_main,
)
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


def _probe_answers(app: App, monkeypatch: pytest.MonkeyPatch, *answers: bool) -> None:
    remaining: Iterator[bool] = iter(answers)
    monkeypatch.setattr(app.start, "_probe_oauth_active", lambda: next(remaining))


def _what_the_user_sees_while_restarting(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    issue_registry: ir.IssueRegistry,
    app: App,
    seen: dict[str, Any],
    core: CoreLink,
    upstream: Upstream,
) -> None:
    variant = app.variant
    relayed_before = len(upstream.requests)
    core.post(f"/api/webhook/{app.proxy_config()['webhook_id']}", b"{}")
    seen["relayed"] = len(upstream.requests) - relayed_before
    seen["entries"] = in_core(hass, lambda: entry_states(hass, variant))
    seen["restart_marker"] = app.path(variant.oauth_marker).exists()
    seen["repair"] = in_core(
        hass,
        lambda: (
            issue_registry.async_get_issue(variant.domain, "oauth_restart_required")
            is not None
        ),
    )
    seen["notifications"] = set(await_in_core(hass, notifications(hass_ws_client)))


async def test_stale_component_code_takes_the_webhook_down(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    issue_registry: ir.IssueRegistry,
    monkeypatch: pytest.MonkeyPatch,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
) -> None:
    """While Core waits for its restart the webhook is gone (it would serve
    unauthenticated) and a click-to-restart Repair is up; when the restarted
    Core still does not enforce OAuth, the webhook stays down and the user is
    told the automatic recovery failed."""
    _probe_answers(app, monkeypatch, False, False)
    app.set_options(mcp_server_url=upstream.target_url(), enable_oauth=True)
    app.deploy_component()
    seen: dict[str, Any] = {}
    app.on_restart = lambda: _what_the_user_sees_while_restarting(
        hass, hass_ws_client, issue_registry, app, seen, core, upstream
    )

    assert await run_main(hass, app) == 0

    assert app.restarts_awaited == 1
    assert seen["entries"] == []
    assert seen["relayed"] == 0
    assert seen["restart_marker"] is True
    assert seen["repair"] is True
    assert f"{variant.domain}_oauth_stale" in seen["notifications"]
    assert f"{variant.domain}_oauth_stuck" in await notifications(hass_ws_client)


async def test_webhook_comes_back_once_core_runs_the_new_code(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    issue_registry: ir.IssueRegistry,
    monkeypatch: pytest.MonkeyPatch,
    app: App,
    core: CoreLink,
    upstream: Upstream,
    variant: Variant,
) -> None:
    """After the restart the webhook is set up again, and the "restart
    required" notification and marker do not outlive the problem."""
    _probe_answers(app, monkeypatch, False, True)
    app.set_options(mcp_server_url=upstream.target_url(), enable_oauth=True)
    app.deploy_component()
    seen: dict[str, Any] = {}
    app.on_restart = lambda: _what_the_user_sees_while_restarting(
        hass, hass_ws_client, issue_registry, app, seen, core, upstream
    )
    running: list[list[str]] = []
    app.clock.while_running = lambda: running.append(
        in_core(hass, lambda: entry_states(hass, variant))
    )

    assert await run_main(hass, app) == 0

    assert f"{variant.domain}_oauth_stale" in seen["notifications"]
    assert running == [["loaded"]]
    assert f"{variant.domain}_oauth_stale" not in await notifications(hass_ws_client)
    assert not app.path(variant.oauth_marker).exists()
    assert "webhook re-enabled" in app.out()
