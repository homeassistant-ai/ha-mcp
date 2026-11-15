"""One restore, read through each consumer that reports it.

The Settings page route, the ``ha_manage_backup`` tool and the page's own
script all report a restore outcome. These helpers run a snapshot's restore
through the first two on the server's own manager and client, on a real
``hass``, and hand its reply to the script.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from starlette.requests import Request

from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.settings_ui import build_settings_handlers

from ._backup_core import HEADER, manage_backup
from ._backup_restore_helpers import run_restore, use_backup_manager
from ._template_helper_core import create_template_sensor, set_template_state

__all__ = [
    "Reply",
    "FlowSpies",
    "SavedTemplate",
    "diff_through",
    "fill_the_disk",
    "removed_template",
    "restore_fails_with",
    "restore_through",
    "run_restore",
    "saved_template",
    "unchanged_template",
    "settings_route",
    "watch_options_flow",
]


@dataclass(frozen=True)
class Reply:
    """What a consumer answered: its HTTP status (Settings only) and payload."""

    status: int | None
    payload: dict[str, Any]


async def settings_route(
    route: str,
    manager: bm.BackupManager,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> Reply:
    """Call the Settings page's ``route`` for the snapshot ``name``."""
    use_backup_manager(monkeypatch, manager)
    request = Request({"type": "http", "path_params": {"name": name}})
    response = await build_settings_handlers(None)[route](request)
    return Reply(response.status_code, json.loads(bytes(response.body)))


async def _tool_reply(client: HomeAssistantClient, action: str, name: str) -> Reply:
    """The ``ha_manage_backup`` answer; a refusal comes back as its payload."""
    try:
        reply = await manage_backup(
            client, scope="edits", action=action, backup_name=name
        )
    except ToolError as err:
        return Reply(None, json.loads(str(err)))
    return Reply(None, reply)


async def restore_through(
    consumer: str,
    manager: bm.BackupManager,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> Reply:
    """Restore ``name`` through ``consumer`` (``settings`` or ``mcp``)."""
    if consumer == "settings":
        return await settings_route("restore_backup", manager, monkeypatch, name)
    return await _tool_reply(client, "restore", name)


async def diff_through(
    consumer: str,
    manager: bm.BackupManager,
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> Reply:
    """Preview the restore of ``name`` through ``consumer``."""
    if consumer == "settings":
        return await settings_route("diff_backup", manager, monkeypatch, name)
    return await _tool_reply(client, "diff", name)


def fill_the_disk(manager: bm.BackupManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every write of a snapshot into the backup directory fails, as on a full disk."""
    native_replace = os.replace

    def replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if Path(os.fsdecode(dst)).parent == manager.backup_dir:
            raise OSError("No space left on device")
        native_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)


def restore_fails_with(
    manager: bm.BackupManager, domain: str, error: Exception
) -> None:
    """Make the restore of ``domain`` end in ``error``, once its safety
    snapshot is taken. Its read of Home Assistant stays the real one."""
    real = manager.handler_for(domain)
    assert real is not None

    async def restore(client: Any, entity_id: str, config: Any) -> Any:
        raise error

    manager.register(bm.DomainHandler(domain, real.fetch, restore))


class FlowSpies(NamedTuple):
    """The options-flow calls a restore makes; Home Assistant still answers each."""

    start: AsyncMock
    submit: AsyncMock
    abort: AsyncMock


@contextmanager
def watch_options_flow(client: HomeAssistantClient) -> Iterator[FlowSpies]:
    """Spy on the options flow calls, so a test can count or forbid one."""
    with (
        patch.object(
            client, "start_options_flow", wraps=client.start_options_flow
        ) as start,
        patch.object(
            client, "submit_options_flow_step", wraps=client.submit_options_flow_step
        ) as submit,
        patch.object(
            client, "abort_options_flow", wraps=client.abort_options_flow
        ) as abort,
    ):
        yield FlowSpies(start, submit, abort)


@dataclass(frozen=True)
class SavedTemplate:
    """A Template helper Core holds, a snapshot of it and what has changed since."""

    entry: ConfigEntry
    snapshot: Path


@pytest.fixture
async def unchanged_template(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> SavedTemplate:
    """A Template helper (entity ``sensor.example``) and a snapshot of it as it is."""
    entry = await create_template_sensor(hass, "saved")
    snapshot = await snapshot_manager.maybe_snapshot(
        "helper_template", entry.entry_id, force=True
    )
    assert snapshot is not None
    return SavedTemplate(entry, snapshot)


@pytest.fixture
async def saved_template(
    hass: HomeAssistant, unchanged_template: SavedTemplate
) -> SavedTemplate:
    """The same helper, its state edited after it was snapshotted."""
    await set_template_state(hass, unchanged_template.entry, "edited")
    return unchanged_template


async def removed_template(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    registry: er.EntityRegistry,
    *,
    keep_mapping: bool,
) -> Path:
    """A snapshot of a Template helper whose entity was renamed to
    ``sensor.custom``, taken before the helper was removed. Without
    ``keep_mapping`` it is the older kind, which stored no entity IDs."""
    entry = await create_template_sensor(hass, "saved")
    registry.async_update_entity("sensor.example", new_entity_id="sensor.custom")
    snapshot = await manager.maybe_snapshot(
        "helper_template", entry.entry_id, force=True
    )
    assert snapshot is not None
    if not keep_mapping:
        stored: dict[str, Any] = yaml.safe_load(snapshot.read_text())
        del stored["config"]["entities"]
        snapshot.write_text(HEADER + yaml.safe_dump(stored, sort_keys=False))
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    return snapshot
