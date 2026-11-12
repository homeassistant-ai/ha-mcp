"""Cancelled backup I/O retains serialization until its executor worker stops.

The helper is a Template sensor that Core's own config flow created; the
manager captures and restores it through the server's own client. The first
restore's safety capture is held in its executor worker, at the point where it
has chosen its file name and not yet written the file.
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient

from ._template_helper_core import (
    create_template_sensor,
    set_template_state,
    state_template,
)

DOMAIN = "helper_template"


def _hold_first_write(
    monkeypatch: pytest.MonkeyPatch, directory: Path, *, fails: bool
) -> tuple[asyncio.Event, asyncio.Event, threading.Event]:
    """Hold the first snapshot write into ``directory`` in its executor worker.

    ``tempfile.mkstemp`` runs once the snapshot's file name is chosen, before
    the file is written. Returns the events set when the first and a second
    write reach that point, and the event that releases the first; with
    ``fails`` the first write then fails.
    """
    loop = asyncio.get_running_loop()
    first, second = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    mkstemp = tempfile.mkstemp

    def reserve(*args: Any, **kwargs: Any) -> Any:
        if Path(kwargs.get("dir", "")) == directory:
            if first.is_set() or release.is_set():
                loop.call_soon_threadsafe(second.set)
            else:
                loop.call_soon_threadsafe(first.set)
                if not release.wait(5):
                    raise TimeoutError("test did not release the writer")
                if fails:
                    raise OSError("controlled writer failure after cancellation")
        return mkstemp(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", reserve)
    return first, second, release


@pytest.fixture
async def loop_errors() -> AsyncIterator[list[dict[str, Any]]]:
    """What reaches the event loop's exception handler during the test."""
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    yield errors
    loop.set_exception_handler(previous)


@pytest.mark.parametrize(
    "worker_fails",
    [
        False,
        pytest.param(
            True,
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "BUG: a writer that fails after its capture was cancelled "
                    "reaches the event loop as an unhandled shielded-future error"
                ),
            ),
        ),
    ],
)
async def test_cancelled_capture_waits_for_writer_before_next_safety_capture(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loop_errors: list[dict[str, Any]],
    worker_fails: bool,
) -> None:
    manager = bm.get_backup_manager(
        component_core_client,
        SimpleNamespace(
            enable_auto_backup=True,
            auto_backup_throttle_minutes=0,
            auto_backup_retain_per_entity=5,
            auto_backup_dir=str(tmp_path / "backups"),
        ),
    )
    entry = await create_template_sensor(hass, "first")
    source = await manager.maybe_snapshot(DOMAIN, entry.entry_id, force=True)
    assert source is not None
    handler = manager.handler_for(DOMAIN)
    assert handler is not None
    restore = AsyncMock(wraps=handler.restore)
    manager.register(bm.DomainHandler(DOMAIN, handler.fetch, restore))
    first_reserved, second_reserved, release_writer = _hold_first_write(
        monkeypatch, manager.backup_dir, fails=worker_fails
    )
    first = asyncio.create_task(manager.restore_snapshot(source.name))
    second = None
    try:
        await asyncio.wait_for(first_reserved.wait(), 2)
        first.cancel("first cancellation")
        await asyncio.sleep(0)
        first.cancel("second cancellation")
        await asyncio.sleep(0)
        await set_template_state(hass, entry, "second")
        second = asyncio.create_task(manager.restore_snapshot(source.name))
        # A released lock would let the second safety capture reserve a name
        # while the first writer still owns it.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(second_reserved.wait(), 0.5)
    finally:
        release_writer.set()
        results = await asyncio.gather(
            first, *([second] if second else []), return_exceptions=True
        )
    assert isinstance(results[0], asyncio.CancelledError)
    assert len(results) == 2
    outcome = results[1]
    assert isinstance(outcome, dict), outcome
    safety = manager.read_snapshot(outcome["safety_backup"])
    assert safety["config"]["options"]["state"] == state_template("second")
    assert source.exists()
    restore.assert_awaited_once()
    assert entry.options["state"] == state_template("first")
    # Nothing is left holding the capture lock.
    later = await asyncio.wait_for(
        manager.maybe_snapshot(DOMAIN, entry.entry_id, force=True), 5
    )
    assert later is not None
    assert loop_errors == []
