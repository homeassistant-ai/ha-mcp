"""Supervisor deprecates an app's token over TCP: on Supervisor 2026.11 and
later the app asks it over the Unix socket Supervisor mounts for it
(``SUPERVISOR_SOCKET``), without the token. Each start is the app's
``main()``; Supervisor's ``/addons`` list decides whether it refuses."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import pytest
import pytest_socket
from aiohttp import web
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

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
    # The plugin's allow list refuses Unix sockets; Core's hassio tests reset it.
    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    found = Supervisor()
    server = web.Application()
    server.router.add_route("*", "/{path:.*}", found.serve)
    runner = web.AppRunner(server)
    await runner.setup()
    # Not tmp_path: it can exceed the 104-byte sun_path limit on macOS.
    socket_dir = tempfile.mkdtemp(dir="/tmp")
    socket_path = Path(socket_dir, "supervisor.sock")
    await web.UnixSite(runner, str(socket_path)).start()
    monkeypatch.setenv("SUPERVISOR_SOCKET", str(socket_path))
    try:
        yield found
    finally:
        await runner.cleanup()
        shutil.rmtree(socket_dir)


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
