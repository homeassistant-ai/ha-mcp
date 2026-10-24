"""A stop that comes before the worker thread publishes its stop event (#2801).

The worker creates its loop and stop event itself, a few milliseconds after
``Thread.start()``; a stop in that gap used to find no event, signal nothing,
and leave the server to start serving with access already switched off.
Reuses the fakes of test_embedded_server.py.
"""

from __future__ import annotations

import threading
from unittest.mock import AsyncMock, MagicMock

from ._embedded_stubs import install

install()

from .test_embedded_server import (  # noqa: E402
    _manager,
    _no_manifest_pin,  # noqa: F401 - autouse fixture
    es,
)


def _clear_worker_env(monkeypatch) -> None:
    # The worker writes these into os.environ. setenv first so monkeypatch
    # records the prior state and undoes the worker's writes; a bare delenv of
    # an unset key records nothing, and HA_MCP_EMBEDDED=1 would leak into later
    # tests (every app-mode check reads it).
    for key in ("HA_MCP_CONFIG_DIR", "HA_MCP_EMBEDDED"):
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


async def test_stop_before_the_worker_publishes_its_event_stops_it(
    tmp_path, monkeypatch
):
    _clear_worker_env(monkeypatch)
    mgr, _hass, _entry = _manager(tmp_path)
    mgr._thread = threading.Thread(target=lambda: None)
    mgr._thread.start()
    seen: list[bool] = []

    async def _fake_serve(_token, stop_event):
        seen.append(stop_event.is_set())

    monkeypatch.setattr(mgr, "_serve", _fake_serve)

    await mgr.async_stop()  # no stop event published yet
    worker = threading.Thread(target=mgr._thread_main, args=("tok",))
    worker.start()  # the worker only gets going now
    worker.join(timeout=5)

    assert seen == [True]


def test_worker_checks_the_flag_only_after_publishing_its_event(tmp_path, monkeypatch):
    # Checking first would miss a stop that looks for the event in between.
    _clear_worker_env(monkeypatch)
    mgr, _hass, _entry = _manager(tmp_path)
    published_at_check: list[bool] = []

    class _Flag(threading.Event):
        def is_set(self) -> bool:
            published_at_check.append(mgr._stop_event is not None)
            return super().is_set()

    mgr._stop_requested = _Flag()

    async def _fake_serve(_token, _stop_event):
        return None

    monkeypatch.setattr(mgr, "_serve", _fake_serve)

    mgr._thread_main("tok")

    assert published_at_check == [True]


async def test_stop_sets_the_flag_before_looking_for_the_event(tmp_path):
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


async def test_each_start_hands_its_worker_a_clear_stop_flag(tmp_path, monkeypatch):
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
