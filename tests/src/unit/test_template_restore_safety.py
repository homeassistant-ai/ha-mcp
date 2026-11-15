"""Recovery references and observed options survive refusal and concurrency.

The helper is a Template sensor (or button) that Core's own config flow made;
the manager captures and restores it over the server's own client, and the
restore goes through Core's options flow.
"""

from __future__ import annotations

import asyncio
import errno
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import label_registry as lr

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import config_entry_flow

from ._backup_core import SnapshotClock, add_label, snapshot_of
from ._backup_core import backup_settings as backup_settings
from ._backup_core import snapshot_clock as snapshot_clock
from ._backup_core import snapshot_manager as snapshot_manager
from ._core_client import refused_url
from ._template_backup_core import legacy_snapshot, real_snapshot
from ._template_backup_core import manager as manager
from ._template_backup_core import settings as settings
from ._template_backup_core import template_entry as template_entry
from ._template_helper_core import set_template_state, state_template
from ._template_recovery_core import (
    DOMAIN,
    DeletedTemplate,
    create_template_button,
    hold_restore,
    recreated_entry,
    stored_state,
)
from ._template_recovery_core import deleted_template as deleted_template


@pytest.fixture
def options_flow(component_core_client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """A spy on opening an options flow; Core still answers it."""
    with patch.object(
        component_core_client,
        "start_options_flow",
        wraps=component_core_client.start_options_flow,
    ) as spy:
        yield spy


def _others(manager: bm.BackupManager, *names: str) -> list[str]:
    return [row["name"] for row in manager.list_snapshots() if row["name"] not in names]


def _then_unreachable(
    monkeypatch: pytest.MonkeyPatch, client: HomeAssistantClient
) -> None:
    """Home Assistant stops answering once the options flow has run."""
    native = config_entry_flow.restore_config_entry_options
    unreachable = refused_url()

    async def restore_then_go_away(*args: Any, **kwargs: Any) -> Any:
        try:
            return await native(*args, **kwargs)
        finally:
            client.base_url = unreachable

    monkeypatch.setattr(
        config_entry_flow, "restore_config_entry_options", restore_then_go_away
    )


async def test_existing_template_cannot_opt_out_of_required_safety(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
    options_flow: AsyncMock,
) -> None:
    source = await real_snapshot(manager, template_entry)
    await set_template_state(hass, template_entry, "before")
    monkeypatch.setattr(settings, "enable_auto_backup", False)
    held = hold_restore(manager)

    task = asyncio.create_task(
        manager.restore_snapshot(source.name, take_safety_backup=False)
    )
    try:
        await asyncio.wait_for(held.entered.wait(), 2)
        (safety,) = _others(manager, source.name)
        assert stored_state(manager, safety) == state_template("before"), (
            "the required safety snapshot must exist before apply"
        )
    finally:
        held.release.set()
        result = await task

    assert result["safety_backup"] == safety
    assert result["apply_status"] == "applied"
    options_flow.assert_awaited_once()
    assert template_entry.options["state"] == state_template("saved")


async def test_template_safety_opt_out_still_refuses_failed_capture(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    options_flow: AsyncMock,
) -> None:
    source = await real_snapshot(manager, template_entry)
    await set_template_state(hass, template_entry, "before")
    native_mkstemp = tempfile.mkstemp

    def disk_full(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("dir") == manager.backup_dir:
            raise OSError(errno.ENOSPC, "No space left on device")
        return native_mkstemp(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", disk_full)

    with pytest.raises(bm.BackupRestoreError) as caught:
        await manager.restore_snapshot(source.name, take_safety_backup=False)

    assert caught.value.outcome["reason"] == "backup_capture_failed"
    assert caught.value.outcome["apply_status"] == "not_applied"
    assert caught.value.outcome["safety_backup"] is None
    assert source.exists()
    options_flow.assert_not_awaited()
    assert template_entry.options["state"] == state_template("before")


async def test_absent_template_opt_out_recreates_without_safety(
    hass: HomeAssistant, deleted_template: DeletedTemplate
) -> None:
    manager = deleted_template.manager

    result = await manager.restore_snapshot(
        deleted_template.name, take_safety_backup=False
    )

    assert result["restore_mode"] == "recreated"
    assert result["entity_id"] == recreated_entry(hass).entry_id
    assert result["safety_backup"] is None
    assert result["verification_status"] == "matched"
    assert _others(manager) == [deleted_template.name]


async def test_non_template_restore_keeps_safety_opt_out(
    snapshot_manager: bm.BackupManager, label_registry: lr.LabelRegistry
) -> None:
    kitchen = add_label(label_registry, "Kitchen")
    source = await snapshot_of(snapshot_manager, kitchen)
    label_registry.async_update(kitchen, name="Renamed")

    result = await snapshot_manager.restore_snapshot(
        source.name, take_safety_backup=False
    )

    assert result["safety_backup"] is None
    assert _others(snapshot_manager) == [source.name]
    restored = label_registry.async_get_label(kitchen)
    assert restored is not None and restored.name == "Kitchen"


async def test_refusal_keeps_selected_source_at_retention_one(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    settings: config.Settings,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auto_backup_retain_per_entity", 1)
    source = await real_snapshot(manager, template_entry)
    # The sensor's options form offers state, not name: a changed name refuses.
    hass.config_entries.async_update_entry(
        template_entry, options={**template_entry.options, "name": "Changed"}
    )
    await hass.async_block_till_done()

    with (
        patch.object(
            component_core_client,
            "submit_options_flow_step",
            wraps=component_core_client.submit_options_flow_step,
        ) as submitted,
        pytest.raises(bm.BackupRestoreError, match="no restore form offers") as caught,
    ):
        await manager.restore_snapshot(source.name)

    assert source.exists()
    assert caught.value.outcome["apply_status"] == "not_applied"
    assert caught.value.outcome["fields"] == ["name"]
    submitted.assert_not_awaited()
    assert template_entry.options["name"] == "Changed"


async def test_active_restore_pins_source_and_safety_during_other_capture(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auto_backup_retain_per_entity", 1)
    source = await real_snapshot(manager, template_entry)
    await set_template_state(hass, template_entry, "before")
    held = hold_restore(manager)
    task = asyncio.create_task(manager.restore_snapshot(source.name))
    try:
        await asyncio.wait_for(held.entered.wait(), 2)
        (safety,) = _others(manager, source.name)
        await real_snapshot(manager, template_entry)
        assert source.exists()
        assert stored_state(manager, safety) == state_template("before")
    finally:
        held.release.set()
        await task


async def test_serialized_restores_capture_immediate_predecessor(
    hass: HomeAssistant, manager: bm.BackupManager, template_entry: ConfigEntry
) -> None:
    await set_template_state(hass, template_entry, "first")
    first = await real_snapshot(manager, template_entry)
    await set_template_state(hass, template_entry, "second")
    second = await real_snapshot(manager, template_entry)
    await set_template_state(hass, template_entry, "initial")
    held = hold_restore(manager)

    one = asyncio.create_task(manager.restore_snapshot(first.name))
    await asyncio.wait_for(held.entered.wait(), 2)
    two = asyncio.create_task(manager.restore_snapshot(second.name))
    # The second restore runs as far as it can before it waits for the first.
    for _ in range(20):
        await asyncio.sleep(0)
    held.release.set()
    _, result = await asyncio.wait_for(asyncio.gather(one, two), 5)

    assert stored_state(manager, result["safety_backup"]) == state_template("first")
    assert template_entry.options["state"] == state_template("second")


def _press(value: object) -> list[dict[str, Any]]:
    return [{"action": "input_boolean.turn_on", "data": {"value": value}}]


async def test_verification_rejects_type_distinct_options(
    hass: HomeAssistant, manager: bm.BackupManager
) -> None:
    """A button's press data saved as False while Core holds 0: Core takes the
    two as equal and keeps 0, so the restore must report the mismatch rather
    than a verified match."""
    entry = await create_template_button(hass, _press(False))
    source = await real_snapshot(manager, entry)
    for value in (5, 0):
        hass.config_entries.async_update_entry(
            entry, options={**entry.options, "press": _press(value)}
        )
    await hass.async_block_till_done()

    with pytest.raises(bm.BackupRestoreError) as caught:
        await manager.restore_snapshot(source.name)
    await hass.async_block_till_done()

    assert caught.value.outcome["apply_status"] == "applied"
    assert caught.value.outcome["verification_status"] == "mismatched"
    (step,) = entry.options["press"]
    assert type(step["data"]["value"]) is int, "Core kept 0"


@pytest.mark.parametrize("matched", [False, True])
async def test_lost_reply_reports_uncertainty_with_observed_readback(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    matched: bool,
) -> None:
    source = await real_snapshot(manager, template_entry)
    if not matched:
        await set_template_state(hass, template_entry, "different")
    monkeypatch.setattr(
        component_core_client,
        "submit_options_flow_step",
        AsyncMock(side_effect=HomeAssistantConnectionError("reply lost")),
    )

    with pytest.raises(bm.BackupRestoreError) as caught:
        await manager.restore_snapshot(source.name)

    assert caught.value.outcome["apply_status"] == "unknown"
    assert caught.value.outcome["verification_status"] == (
        "matched" if matched else "mismatched"
    )


async def test_safety_snapshot_can_recover_predecessor_at_retention_one(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auto_backup_retain_per_entity", 1)
    await set_template_state(hass, template_entry, "historical")
    source = await real_snapshot(manager, template_entry)
    await set_template_state(hass, template_entry, "before")

    applied = await manager.restore_snapshot(source.name)
    assert source.exists()
    assert template_entry.options["state"] == state_template("historical")
    recovered = await manager.restore_snapshot(applied["safety_backup"])

    assert template_entry.options["state"] == state_template("before")
    assert stored_state(manager, recovered["safety_backup"]) == state_template(
        "historical"
    )
    # Protection ends with the restores; later captures keep normal retention.
    await real_snapshot(manager, template_entry)
    assert len(manager.list_snapshots(domain=DOMAIN)) == 1


@pytest.mark.parametrize("clock_rollback", [False, True])
async def test_safety_exists_before_apply_despite_older_filename_order(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    settings: config.Settings,
    snapshot_clock: SnapshotClock,
    monkeypatch: pytest.MonkeyPatch,
    clock_rollback: bool,
) -> None:
    monkeypatch.setattr(settings, "auto_backup_retain_per_entity", 1)
    source = await real_snapshot(manager, template_entry)
    if clock_rollback:
        snapshot_clock.set("2026-09-02T12:00:00")
    else:
        source = await real_snapshot(manager, template_entry)
        assert source.name.endswith("_01.yaml")
    await set_template_state(hass, template_entry, "before")
    held = hold_restore(manager)

    task = asyncio.create_task(manager.restore_snapshot(source.name))
    try:
        await asyncio.wait_for(held.entered.wait(), 2)
        (safety,) = _others(manager, source.name)
        assert stored_state(manager, safety) == state_template("before"), (
            "restore must have a retained safety snapshot before applying"
        )
    finally:
        held.release.set()
        outcome = await task

    assert source.exists()
    assert outcome["safety_backup"] == safety


@pytest.mark.parametrize("bulk", [False, True])
async def test_active_restore_protects_source_and_safety_from_deletion(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    bulk: bool,
) -> None:
    source = await real_snapshot(manager, template_entry)
    await set_template_state(hass, template_entry, "other history")
    unprotected = await real_snapshot(manager, template_entry)
    held = hold_restore(manager)
    task = asyncio.create_task(manager.restore_snapshot(source.name))
    try:
        await asyncio.wait_for(held.entered.wait(), 2)
        protected = set(_others(manager, unprotected.name))
        assert len(protected) == 2
        if bulk:
            result = manager.delete_bulk(domain=DOMAIN)
            assert set(result["failed"]) == protected
            assert result["deleted"] == [unprotected.name]
        else:
            for name in protected:
                with pytest.raises(ValueError, match="active restore"):
                    manager.delete_snapshot(name)
            manager.delete_snapshot(unprotected.name)
        assert set(_others(manager)) == protected
    finally:
        held.release.set()
        await task
    assert set(manager.delete_bulk(domain=DOMAIN)["deleted"]) == protected


async def test_safety_capture_is_pinned_while_rotation_runs(
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = await real_snapshot(manager, template_entry)
    rotating, release = threading.Event(), threading.Event()
    native_glob = Path.glob

    def glob(self: Path, pattern: str, *args: Any, **kwargs: Any) -> Any:
        # Rotation lists the helper's history in an executor worker.
        if pattern.startswith(f"{DOMAIN}.") and not rotating.is_set():
            rotating.set()
            if not release.wait(5):
                raise TimeoutError("the test did not release the rotation")
        return native_glob(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", glob)
    task = asyncio.create_task(manager.restore_snapshot(source.name))
    try:
        assert await asyncio.to_thread(rotating.wait, 2)
        (safety,) = _others(manager, source.name)
        with pytest.raises(ValueError, match="active restore"):
            manager.delete_snapshot(safety)
    finally:
        release.set()
        outcome = await task
    assert outcome["safety_backup"] == safety


async def test_missing_safety_at_pin_handoff_refuses_apply(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    options_flow: AsyncMock,
) -> None:
    source = await real_snapshot(manager, template_entry)
    await set_template_state(hass, template_entry, "before")
    native_capture = manager.maybe_snapshot

    async def capture_then_lose_it(*args: Any, **kwargs: Any) -> Path | None:
        # The safety capture has finished and released its file; it is
        # deleted before the restore pins it.
        path = await native_capture(*args, **kwargs)
        if path is not None:
            manager.delete_snapshot(path.name)
        return path

    monkeypatch.setattr(manager, "maybe_snapshot", capture_then_lose_it)
    with pytest.raises(bm.BackupRestoreError) as caught:
        await manager.restore_snapshot(source.name)

    assert caught.value.outcome["apply_status"] == "not_applied"
    assert caught.value.outcome["safety_backup"] is None
    options_flow.assert_not_awaited()
    assert source.exists()
    assert template_entry.options["state"] == state_template("before")


async def test_cancellation_releases_entry_for_next_restore_and_keeps_recovery(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auto_backup_retain_per_entity", 1)
    source = await real_snapshot(manager, template_entry)
    await set_template_state(hass, template_entry, "before")
    held = hold_restore(manager)
    task = asyncio.create_task(manager.restore_snapshot(source.name))
    await asyncio.wait_for(held.entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert source.exists()
    assert len(manager.list_snapshots(domain=DOMAIN)) == 2
    held.release.set()
    result = await asyncio.wait_for(manager.restore_snapshot(source.name), 5)
    assert stored_state(manager, result["safety_backup"]) == state_template("before")
    assert template_entry.options["state"] == state_template("saved")


async def test_alias_and_stable_id_restores_share_serialization(
    hass: HomeAssistant, manager: bm.BackupManager, template_entry: ConfigEntry
) -> None:
    first = await real_snapshot(manager, template_entry)
    alias = legacy_snapshot(
        manager, await real_snapshot(manager, template_entry), "sensor.reused"
    )
    held = hold_restore(manager)

    one = asyncio.create_task(manager.restore_snapshot(first.name))
    await asyncio.wait_for(held.entered.wait(), 2)
    two = asyncio.create_task(manager.restore_snapshot(alias.name))
    for _ in range(20):
        await asyncio.sleep(0)
    assert held.targets == [template_entry.entry_id]
    held.release.set()
    await asyncio.wait_for(asyncio.gather(one, two), 5)

    assert held.targets == [template_entry.entry_id, template_entry.entry_id]


@pytest.mark.parametrize("submit", ["answered", "no-answer"])
async def test_unavailable_readback_retains_apply_knowledge(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    submit: str,
) -> None:
    """Home Assistant stops answering after the options flow: the outcome keeps
    what is known about the apply, and no remote text."""
    source = await real_snapshot(manager, template_entry)
    await set_template_state(hass, template_entry, "before")
    if submit == "no-answer":
        monkeypatch.setattr(
            component_core_client,
            "submit_options_flow_step",
            AsyncMock(side_effect=HomeAssistantConnectionError("private upstream")),
        )
    _then_unreachable(monkeypatch, component_core_client)

    with pytest.raises(bm.BackupRestoreError) as caught:
        await manager.restore_snapshot(source.name)

    assert caught.value.outcome["apply_status"] == (
        "applied" if submit == "answered" else "unknown"
    )
    assert caught.value.outcome["verification_status"] == "unavailable"
    assert "private upstream" not in str(caught.value)
