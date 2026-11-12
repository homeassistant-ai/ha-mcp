"""Restore protection across queued retention and pre-submit failures.

The helper is a Template sensor that Core's own config flow created; the
manager captures and restores it through the server's own client, and the
restore goes through Core's options flow.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import config_entry_flow

from ._template_helper_core import (
    create_template_sensor,
    set_template_state,
    state_template,
)

DOMAIN = "helper_template"


@pytest.fixture
async def entry(hass: HomeAssistant) -> ConfigEntry:
    """A Template sensor helper made by Core's config flow, in state ``saved``."""
    return await create_template_sensor(hass, "saved")


@pytest.fixture
def manager(
    component_core_client: HomeAssistantClient, tmp_path: Path
) -> bm.BackupManager:
    settings = SimpleNamespace(
        auto_backup_dir=str(tmp_path / "backups"),
        enable_auto_backup=True,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=1,
    )
    return bm.get_backup_manager(component_core_client, settings)


async def _capture(manager: bm.BackupManager, entry: ConfigEntry) -> Path:
    path = await manager.maybe_snapshot(DOMAIN, entry.entry_id, force=True)
    assert path is not None
    return path


def _state(manager: bm.BackupManager, name: str) -> str:
    options: dict[str, str] = manager.read_snapshot(name)["config"]["options"]
    return options["state"]


@pytest.fixture
def options_flow() -> Iterator[AsyncMock]:
    """A spy on the options-flow restore; Core still runs the flow."""
    with patch.object(
        config_entry_flow,
        "restore_config_entry_options",
        wraps=config_entry_flow.restore_config_entry_options,
    ) as spy:
        yield spy


def _hold_first_rotation(
    monkeypatch: pytest.MonkeyPatch, manager: bm.BackupManager
) -> tuple[threading.Event, threading.Event]:
    """Stop the first rotation of a helper history in its executor worker.

    Rotation lists the history before it checks pins, so the worker stops
    after its caller froze the pins it knew of when it was dispatched.
    """
    entered, release = threading.Event(), threading.Event()
    glob: Callable[..., Iterator[Path]] = Path.glob

    def held_glob(self: Path, pattern: str, *args: Any, **kwargs: Any) -> Any:
        if (
            self == manager.backup_dir
            and pattern.startswith(f"{DOMAIN}.")
            and not entered.is_set()
        ):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("the test did not release the rotation")
        return glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", held_glob)
    return entered, release


async def test_rotation_dispatched_before_restore_must_honor_later_pin(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    entry: ConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    options_flow: AsyncMock,
) -> None:
    source = await _capture(manager, entry)
    await set_template_state(hass, entry, "current")
    entered, release = _hold_first_rotation(monkeypatch, manager)
    capture = asyncio.create_task(manager.maybe_snapshot(DOMAIN, entry.entry_id))
    restore: asyncio.Task[dict[str, Any]] | None = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # The capture keeps one snapshot and is rotating out ``source``; the
        # restore pins ``source`` only now.
        restore = asyncio.create_task(manager.restore_snapshot(source.name))
        await asyncio.sleep(0)
        with pytest.raises(bm.SnapshotInUseError):
            manager.delete_snapshot(source.name)
        release.set()
        await capture
        result = await restore
    finally:
        release.set()
        await asyncio.gather(
            capture, *([restore] if restore else []), return_exceptions=True
        )

    assert _state(manager, result["safety_backup"]) == state_template("current")
    assert source.exists(), "earlier executor rotation ignored the active source pin"
    assert _state(manager, source.name) == state_template("saved"), (
        "pruned source name was reused for the safety backup"
    )
    options_flow.assert_awaited_once()
    assert entry.options["state"] == state_template("saved")


async def test_failure_before_options_flow_keeps_not_applied_knowledge(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    entry: ConfigEntry,
    component_core_client: HomeAssistantClient,
    options_flow: AsyncMock,
) -> None:
    source = await _capture(manager, entry)
    await set_template_state(hass, entry, "current")
    handler = manager.handler_for(DOMAIN)
    assert handler is not None
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        unreachable = f"http://127.0.0.1:{probe.getsockname()[1]}"

    async def restore_offline(client: Any, entity_id: str, config: Any) -> Any:
        # Preflight and the mandatory safety capture are done; Home Assistant
        # stops answering before the handler's pre-submit read.
        client.base_url = unreachable
        return await handler.restore(client, entity_id, config)

    manager.register(bm.DomainHandler(DOMAIN, handler.fetch, restore_offline))

    with pytest.raises(bm.BackupRestoreError) as caught:
        await manager.restore_snapshot(source.name)

    options_flow.assert_not_awaited()
    assert caught.value.outcome["safety_backup"] is not None
    assert caught.value.outcome["apply_status"] == "not_applied"
    assert entry.options["state"] == state_template("current")
