"""Stops that race the worker thread's startup (#2801).

The worker creates its loop and stop event itself, a few milliseconds after
``Thread.start()``; a stop in that gap used to find no event, signal nothing,
and leave the server to start serving with access already switched off. A
stop during the cold import that follows did find the event, but its queued
set had not run when the listener started. The worker is the real one: the
gap is held open at its event loop creation, the cold import through
``ImportHook``.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import embedded_server as es
from custom_components.ha_mcp_tools.const import OPT_PIP_SPEC

from ._embedded_server_core import (
    WORKER_THREAD,
    ImportHook,
    new_manager,
    running_server,
    server_options,
    shorten,
    worker_gone,
)
from ._embedded_server_core import embedded_server_env as embedded_server_env
from ._embedded_server_core import import_hook as import_hook

pytestmark = pytest.mark.usefixtures("embedded_server_env", "socket_enabled")

STOPPED_BEFORE_LISTENING = "stopped before its HTTP listener started"


def is_closed(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


async def stop_while_the_worker_is_held(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    entered: threading.Event,
    release: threading.Event,
    caplog: pytest.LogCaptureFixture,
    port_options: dict[str, object],
) -> int:
    """Start a server, stop it while the worker waits, release it; return its port."""
    shorten(monkeypatch, stop_join=0.2, ready_stall=3, ready_cap=3, ready_poll=0.1)
    caplog.set_level(logging.DEBUG, logger=es.__name__)
    mgr, _entry = await new_manager(hass, token=True, options=port_options)
    start = asyncio.ensure_future(mgr.async_start())
    assert await hass.async_add_executor_job(entered.wait, 30)

    await mgr.async_stop()
    release.set()
    await worker_gone()
    # Uncancelled, the start reports a server that never came up; the entry's
    # unload cancels it instead.
    await asyncio.wait([start])
    with pytest.raises(es.EmbeddedServerError, match="did not become reachable"):
        start.result()
    return mgr.port


async def test_a_stop_before_the_worker_is_up_keeps_it_from_serving(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stop that finds no worker event yet must still stop the server (#2801)."""
    entered, release = threading.Event(), threading.Event()
    real_new_event_loop = asyncio.new_event_loop

    def held_new_event_loop() -> asyncio.AbstractEventLoop:
        if threading.current_thread().name == WORKER_THREAD:
            entered.set()
            release.wait(30)
        return real_new_event_loop()

    monkeypatch.setattr(asyncio, "new_event_loop", held_new_event_loop)

    port = await stop_while_the_worker_is_held(
        hass, monkeypatch, entered, release, caplog, server_options()
    )

    assert STOPPED_BEFORE_LISTENING in caplog.text
    assert is_closed(port)


@pytest.mark.parametrize("module", ["ha_mcp.config", "ha_mcp.server"])
async def test_a_stop_during_the_cold_import_keeps_the_port_closed(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    import_hook: ImportHook,
    caplog: pytest.LogCaptureFixture,
    module: str,
) -> None:
    """The queued stop may not have run when the listener starts; the flag shows it."""
    import_hook.block(module)

    port = await stop_while_the_worker_is_held(
        hass,
        monkeypatch,
        import_hook.entered,
        import_hook.release,
        caplog,
        {**server_options(), OPT_PIP_SPEC: "ha-mcp==99.0.0"},
    )

    assert STOPPED_BEFORE_LISTENING in caplog.text
    assert is_closed(port)


async def test_a_restart_after_a_stop_serves(hass: HomeAssistant) -> None:
    """A stop flag left set by the previous stop must not stop the next worker."""
    async with running_server(hass) as (mgr, _entry):
        await mgr.async_stop()
        assert is_closed(mgr.port)

        await mgr.async_start()

        assert mgr.is_running
        assert not is_closed(mgr.port)
