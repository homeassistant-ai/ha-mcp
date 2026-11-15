"""A todo list Core has not loaded, as the Settings routes and the backup tool report it.

A list Home Assistant has not loaded is ENTITY_UNAVAILABLE; one it no longer
has is ENTITY_NOT_FOUND. A list is unloaded (its state reads unavailable),
disabled (no state, but its registry entry is kept) or deleted.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp.client.rest_client import HomeAssistantClient

from ._backup_core import backup_settings as backup_settings
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_todo_core import (
    LIST,
    add_item,
    mcp_call,
    settings_call,
    take_offline,
    watch_services,
)
from ._backup_todo_core import tasks as tasks

CONSUMERS = ["settings", "mcp"]


async def _snapshot_of_pay(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
    auto_backup: bool,
) -> str:
    """The name of a forced snapshot of the item ``Pay``, with auto-backup as given."""
    uid = await add_item(hass, "Pay", completed=True)
    monkeypatch.setattr(settings, "enable_auto_backup", auto_backup)
    path = await manager.maybe_snapshot("todo_item", f"{LIST}::{uid}", force=True)
    assert path is not None
    return path.name


async def _refusal(
    client: HomeAssistantClient, consumer: str, action: str, name: str, status: int
) -> dict[str, Any]:
    """The error body the consumer answers ``action`` with, after checking the
    Settings route's status (the tool raises, so it has none)."""
    if consumer == "mcp":
        return await mcp_call(client, action, name)
    code, payload = await settings_call(client, f"{action}_backup", name)
    assert code == status
    return payload


@pytest.mark.parametrize("consumer", CONSUMERS)
@pytest.mark.parametrize("how", ["unavailable", "disabled"])
@pytest.mark.parametrize("auto_backup", [True, False])
async def test_restoring_to_a_list_core_has_not_loaded_writes_nothing(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    backup_settings: config.Settings,
    tasks: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
    how: str,
    auto_backup: bool,
) -> None:
    name = await _snapshot_of_pay(
        hass, snapshot_manager, backup_settings, monkeypatch, auto_backup
    )
    await take_offline(hass, entity_registry, tasks, how)
    sent = watch_services(hass)

    payload = await _refusal(core_client, consumer, "restore", name, 409)

    assert payload["error"]["code"] == "ENTITY_UNAVAILABLE"
    assert payload["data"]["apply_status"] == "not_applied"
    assert sent() == []


@pytest.mark.parametrize("consumer", CONSUMERS)
@pytest.mark.parametrize("how", ["unavailable", "disabled"])
async def test_comparing_with_a_list_core_has_not_loaded_is_unavailable(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    backup_settings: config.Settings,
    tasks: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
    how: str,
) -> None:
    name = await _snapshot_of_pay(
        hass, snapshot_manager, backup_settings, monkeypatch, True
    )
    await take_offline(hass, entity_registry, tasks, how)

    payload = await _refusal(core_client, consumer, "diff", name, 409)

    assert payload["error"]["code"] == "ENTITY_UNAVAILABLE"
    assert "has not loaded the todo list" in payload["error"]["message"]
    if consumer == "mcp":
        assert payload["backup_name"] == name


async def test_comparing_with_a_deleted_list_finds_the_item_missing(
    hass: HomeAssistant,
    snapshot_manager: bm.BackupManager,
    backup_settings: config.Settings,
    tasks: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    name = await _snapshot_of_pay(
        hass, snapshot_manager, backup_settings, monkeypatch, True
    )
    await take_offline(hass, entity_registry, tasks, "deleted")

    result = await snapshot_manager.diff_snapshot(name)

    assert result["entity_missing"] is True


@pytest.mark.parametrize("consumer", CONSUMERS)
@pytest.mark.parametrize("auto_backup", [True, False])
async def test_restoring_to_a_deleted_list_is_not_found(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    backup_settings: config.Settings,
    tasks: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
    consumer: str,
    auto_backup: bool,
) -> None:
    """With auto-backup the safety backup reads the list first; both ways a
    deleted list is not found."""
    name = await _snapshot_of_pay(
        hass, snapshot_manager, backup_settings, monkeypatch, auto_backup
    )
    await take_offline(hass, entity_registry, tasks, "deleted")
    sent = watch_services(hass)

    payload = await _refusal(core_client, consumer, "restore", name, 404)

    assert payload["error"]["code"] == "ENTITY_NOT_FOUND"
    assert "no longer exists" in payload["error"]["message"]
    assert sent() == []
