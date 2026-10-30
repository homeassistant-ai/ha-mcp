"""Stops that race the worker thread's startup (#2801).

The worker creates its loop and stop event itself, a few milliseconds after
``Thread.start()``; a stop in that gap used to find no event, signal nothing,
and leave the server to start serving with access already switched off. A
stop during the cold import that follows did find the event, but its queued
set had not run when the listener started. Reuses the stubs and manager
factory of test_embedded_server.py; the listener fakes are local.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools.const import OPT_SERVER_URL  # noqa: E402

from .test_embedded_server import (  # noqa: E402
    _manager,
    _no_manifest_pin,  # noqa: F401 - autouse fixture
    _stub_ha_mcp_surface,
    es,
)


def _clear_worker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The worker writes these into os.environ. setenv first so monkeypatch
    # records the prior state and undoes the worker's writes; a bare delenv of
    # an unset key records nothing, and HA_MCP_EMBEDDED=1 would leak into later
    # tests (every app-mode check reads it).
    for key in ("HA_MCP_CONFIG_DIR", "HA_MCP_EMBEDDED"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


async def test_stop_before_the_worker_publishes_its_event_stops_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_worker_env(monkeypatch)
    mgr, _hass, _entry = _manager(tmp_path)
    mgr._thread = threading.Thread(target=lambda: None)
    mgr._thread.start()
    seen: list[bool] = []

    async def _fake_serve(
        _token: str, stop_event: asyncio.Event, _stop_requested: threading.Event
    ) -> None:
        seen.append(stop_event.is_set())

    monkeypatch.setattr(mgr, "_serve", _fake_serve)

    await mgr.async_stop()  # no stop event published yet
    worker = threading.Thread(target=mgr._thread_main, args=("tok",))
    worker.start()  # the worker only gets going now
    worker.join(timeout=5)

    assert seen == [True]


def test_worker_checks_the_flag_only_after_publishing_its_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Checking first would miss a stop that looks for the event in between.
    _clear_worker_env(monkeypatch)
    mgr, _hass, _entry = _manager(tmp_path)
    published_at_check: list[bool] = []

    class _Flag(threading.Event):
        def is_set(self) -> bool:
            published_at_check.append(mgr._stop_event is not None)
            return super().is_set()

    mgr._stop_requested = _Flag()

    async def _fake_serve(
        _token: str, _stop_event: asyncio.Event, _stop_requested: threading.Event
    ) -> None:
        return None

    monkeypatch.setattr(mgr, "_serve", _fake_serve)

    mgr._thread_main("tok")

    assert published_at_check == [True]


async def test_stop_sets_the_flag_before_looking_for_the_event(tmp_path: Path) -> None:
    # A worker that publishes its event between the two steps must still be
    # signalled; looking first would find nothing and skip it.
    mgr, _hass, _entry = _manager(tmp_path)
    loop = MagicMock()
    loop.is_closed.return_value = False
    event = MagicMock()

    class _Flag(threading.Event):
        def set(self) -> None:
            super().set()
            mgr._loop = loop
            mgr._stop_event = event

    mgr._stop_requested = _Flag()
    mgr._thread = threading.Thread(target=lambda: None)
    mgr._thread.start()

    await mgr.async_stop()

    loop.call_soon_threadsafe.assert_called_once_with(event.set)


async def test_each_start_hands_its_worker_a_clear_stop_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A flag left set by the previous stop would stop the new worker at once.
    mgr, _hass, _entry = _manager(tmp_path)
    monkeypatch.setattr(mgr, "_async_ensure_package", AsyncMock())
    monkeypatch.setattr(mgr, "_async_access_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(mgr, "_async_wait_until_ready", AsyncMock())
    monkeypatch.setattr(mgr, "_prepare_config_dir", lambda: None)
    monkeypatch.setattr(es, "_purge_ha_mcp_modules", lambda: None)
    flags: list[bool] = []
    monkeypatch.setattr(
        mgr, "_thread_main", lambda _tok: flags.append(mgr._stop_requested.is_set())
    )
    stale = threading.Event()
    stale.set()
    mgr._stop_requested = stale

    await mgr.async_start()
    mgr._thread.join(timeout=2)

    assert flags == [False]


@pytest.mark.parametrize("stop_during", ["server build", "lifespan entry"])
def test_a_stop_before_the_listener_never_opens_the_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_during: str
) -> None:
    # The stop's event-set is still queued when the worker reaches the
    # listener, so only the flag shows it.
    _clear_worker_env(monkeypatch)
    mgr, _hass, _entry = _manager(
        tmp_path, options={OPT_SERVER_URL: "http://ha.local:8123"}
    )
    # Already finished: the stop's join returns while the worker still starts,
    # as it does after a cold import outlasts the join.
    mgr._thread = threading.Thread(target=lambda: None)
    mgr._thread.start()

    def _stop() -> None:
        stopper = threading.Thread(target=asyncio.run, args=(mgr.async_stop(),))
        stopper.start()
        stopper.join(timeout=5)
        assert not stopper.is_alive()
        # The next start hands the manager a fresh flag.
        mgr._stop_requested = threading.Event()

    @asynccontextmanager
    async def _lifespan() -> AsyncIterator[None]:
        if stop_during == "lifespan entry":
            _stop()
            await asyncio.sleep(0)
        yield

    class _FakeMcp:
        _lifespan_manager = staticmethod(_lifespan)

        def http_app(self, path: str, stateless_http: bool) -> object:
            return object()

    server = SimpleNamespace(mcp=_FakeMcp(), close=AsyncMock())

    def _build_server() -> SimpleNamespace:
        if stop_during == "server build":
            _stop()
        return server

    served: list[bool] = []

    class _UvServer:
        def __init__(self, config: Any) -> None:
            self.should_exit = False

        async def serve(self) -> None:
            served.append(True)

    _stub_ha_mcp_surface(monkeypatch, mcp=server.mcp)
    monkeypatch.setattr(es, "_installed_dist_version", lambda dist: None)
    server_mod = sys.modules["ha_mcp.server"]
    monkeypatch.setattr(server_mod, "HomeAssistantSmartMCPServer", _build_server)
    monkeypatch.setattr(sys.modules["uvicorn"], "Server", _UvServer)

    mgr._thread_main("tok")

    assert served == []
    server.close.assert_awaited_once()
    assert mgr._thread_exc is None
