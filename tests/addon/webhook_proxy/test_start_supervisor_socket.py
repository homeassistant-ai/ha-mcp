"""Supervisor deprecates an app's token over TCP: on Supervisor 2026.11.1 and
later the app asks it over the Unix socket Supervisor mounts for it
(``SUPERVISOR_SOCKET``), without the token. Each start is the app's
``main()``; Supervisor's ``/addons`` list decides whether it refuses."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from tests.src.unit._tools_addons_core import app_socket

from ._start import App, CoreLink, Supervisor, connect_core, make_app, run_main
from ._variants import Variant, supervisor_socket_supported


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


@pytest.fixture
async def socket_supervisor(
    monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> AsyncIterator[Supervisor]:
    """A second Supervisor, reached only over the app's Unix socket."""
    found = Supervisor()
    async with app_socket(found.serve, monkeypatch):
        yield found


async def test_the_app_asks_supervisor_over_its_socket_without_its_token(
    hass: HomeAssistant,
    app: App,
    core: CoreLink,
    socket_supervisor: Supervisor,
    variant: Variant,
) -> None:
    # Both list the running sibling, so either route ends in the refusal.
    sibling = [{"slug": f"abc123_{variant.sibling_base}", "state": "started"}]
    socket_supervisor.addons = sibling
    app.supervisor.addons = sibling

    assert await run_main(hass, app) == 1

    asked, unused = (
        (socket_supervisor, app.supervisor)
        if supervisor_socket_supported(variant)
        else (app.supervisor, socket_supervisor)
    )
    assert "/addons" in asked.gets
    assert unused.gets == []
    if supervisor_socket_supported(variant):
        assert set(socket_supervisor.authorizations) == {None}
