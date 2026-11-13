"""Regressions for Template capture trust, write coordination and identity."""

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import auto_backup, backup
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._held_command import held_command
from ._template_helper_core import (
    create_template_sensor,
    set_template_state,
    state_template,
)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> config.Settings:
    """The server's real settings, with auto-backup on and its own directory."""
    configured = config.get_global_settings()
    for name, value in {
        "enable_auto_backup": True,
        "enable_snapshot_actions": True,
        "backup_read_only": False,
        "read_only_mode": False,
        "enable_snapshot_delete": False,
        "auto_backup_throttle_minutes": 30,
        "auto_backup_retain_per_entity": 5,
        "auto_backup_dir": str(tmp_path / "backups"),
    }.items():
        monkeypatch.setattr(configured, name, value)
    return configured


@pytest.fixture
async def template_entry(hass: HomeAssistant) -> ConfigEntry:
    return await create_template_sensor(hass, "saved")


@pytest.fixture
def manager(
    component_core_client: HomeAssistantClient, settings: config.Settings
) -> bm.BackupManager:
    return bm.get_backup_manager(component_core_client, settings)


async def _until(predicate: Any) -> None:
    for _ in range(1000):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


def _rename(
    entity_registry: er.EntityRegistry, entry: ConfigEntry, new_entity_id: str
) -> None:
    (row,) = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    entity_registry.async_update_entity(row.entity_id, new_entity_id=new_entity_id)


def _degrade_secret_scrub(hass: HomeAssistant) -> None:
    """A secrets.yaml that is present but cannot be parsed."""
    Path(hass.config.path("secrets.yaml")).write_text("key: [unclosed\n")


async def _real_snapshot(manager: bm.BackupManager, entry: ConfigEntry) -> Path:
    path = await manager.maybe_snapshot("helper_template", entry.entry_id, force=True)
    assert path is not None
    return path


def _legacy(
    manager: bm.BackupManager,
    path: Path,
    entity_id: str | None,
    *,
    entry_id: str | None = None,
    emptied: bool = False,
) -> Path:
    """Rewrite a real snapshot's identity the way an older release stored it."""
    data = manager.read_snapshot(path.name)
    data["entity_id"] = entity_id
    if entry_id is not None:
        data["config"]["entry_id"] = entry_id
    if emptied:
        data["config"] = {}
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.mark.parametrize("mandatory", [False, True])
async def test_degraded_scrub_never_writes_snapshot(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    mandatory: bool,
) -> None:
    _degrade_secret_scrub(hass)
    if mandatory:
        with pytest.raises(bm.MandatoryBackupError, match="secret scrub"):
            await manager.maybe_snapshot(
                "helper_template", template_entry.entry_id, mandatory=True
            )
    else:
        assert (
            await manager.maybe_snapshot("helper_template", template_entry.entry_id)
            is None
        )
    assert not list(manager.backup_dir.glob("*.yaml"))


async def test_degraded_safety_capture_blocks_restore(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
) -> None:
    source = await _real_snapshot(manager, template_entry)
    await set_template_state(hass, template_entry, "changed")
    _degrade_secret_scrub(hass)

    with pytest.raises(bm.BackupRestoreError) as caught:
        await manager.restore_snapshot(source.name)

    assert caught.value.outcome["apply_status"] == "not_applied"
    assert caught.value.outcome["safety_backup"] is None
    assert template_entry.options["state"] == state_template("changed")
    assert list(manager.backup_dir.glob("*.yaml")) == [source]


@pytest.mark.parametrize(
    "first_target,second_target",
    [("alias", "canonical"), ("canonical", "alias")],
)
async def test_alias_capture_uses_stable_header_and_shared_throttle(
    entity_registry: er.EntityRegistry,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    first_target: str,
    second_target: str,
) -> None:
    _rename(entity_registry, template_entry, "sensor.renamed_example")
    targets = {"alias": "sensor.renamed_example", "canonical": template_entry.entry_id}

    first = await manager.maybe_snapshot("helper_template", targets[first_target])

    assert first is not None
    assert manager.read_snapshot(first.name)["entity_id"] == template_entry.entry_id
    assert (
        await manager.maybe_snapshot("helper_template", targets[second_target]) is None
    )
    assert [
        row["name"] for row in manager.list_snapshots(domain="helper_template")
    ] == [first.name]


async def test_alias_capture_refetches_after_canonical_lock(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An alias capture that waited on the canonical lock reads the entry again."""
    _rename(entity_registry, template_entry, "sensor.renamed_example")
    monkeypatch.setattr(settings, "auto_backup_throttle_minutes", 0)
    async with held_command(hass, "ha_mcp_tools/helpers_list", first_only=True) as held:
        # The canonical capture's read is held while it owns the lock.
        canonical = asyncio.create_task(
            manager.maybe_snapshot("helper_template", template_entry.entry_id)
        )
        await asyncio.wait_for(held.arrived.wait(), 2)
        alias = asyncio.create_task(
            manager.maybe_snapshot("helper_template", "sensor.renamed_example")
        )
        # The alias capture's own read is answered; it then waits for the lock.
        await _until(lambda: held.calls >= 2)
        for _ in range(20):
            await asyncio.sleep(0)
        await set_template_state(hass, template_entry, "after-lock")
        held.release.set()
        await asyncio.wait_for(canonical, 2)
        path = await asyncio.wait_for(alias, 2)

    assert path is not None
    assert manager.read_snapshot(path.name)["config"]["options"][
        "state"
    ] == state_template("after-lock")


async def test_capture_response_identity_round_trips_to_history(
    entity_registry: er.EntityRegistry,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
) -> None:
    _rename(entity_registry, template_entry, "sensor.renamed_example")
    mcp = FastMCP("template-backup-review")
    backup.register_backup_tools(mcp, component_core_client)
    tool = await mcp.get_tool("ha_manage_backup")

    result = await tool.fn(
        scope="edits",
        action="create",
        domain="helper_template",
        entity_id="sensor.renamed_example",
    )

    rows = manager.list_snapshots(
        domain="helper_template", entity_id=result["data"]["entity_id"]
    )
    assert [row["name"] for row in rows] == [result["data"]["backup_name"]]


@pytest.mark.parametrize(
    ("identity", "emptied"),
    [(None, False), ("wrong-entry", False), ("sensor.old", True)],
)
async def test_invalid_legacy_identity_is_not_deleted(
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
    identity: str | None,
    emptied: bool,
) -> None:
    path = _legacy(
        manager,
        await _real_snapshot(manager, template_entry),
        identity,
        emptied=emptied,
    )
    monkeypatch.setattr(settings, "auto_backup_retain_per_entity", 1)
    await manager.maybe_snapshot("helper_template", template_entry.entry_id, force=True)

    manager.delete_bulk(domain="helper_template", entity_id=template_entry.entry_id)

    assert path.exists()


async def test_legacy_alias_history_lists_and_deletes_by_stable_identity(
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
) -> None:
    old = _legacy(
        manager, await _real_snapshot(manager, template_entry), "sensor.reused"
    )
    new = await _real_snapshot(manager, template_entry)
    other = _legacy(
        manager,
        await _real_snapshot(manager, template_entry),
        "sensor.reused",
        entry_id="other-entry",
    )

    rows = manager.list_snapshots(
        domain="helper_template", entity_id=template_entry.entry_id
    )

    assert {row["name"] for row in rows} == {old.name, new.name}
    assert {row["entity_id"] for row in rows} == {template_entry.entry_id}
    assert set(
        manager.delete_bulk(
            domain="helper_template", entity_id=template_entry.entry_id
        )["deleted"]
    ) == {old.name, new.name}
    assert other.exists()


async def test_legacy_alias_history_rotates_with_canonical_captures(
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = _legacy(
        manager, await _real_snapshot(manager, template_entry), "sensor.reused"
    )
    other = _legacy(
        manager,
        await _real_snapshot(manager, template_entry),
        "sensor.reused",
        entry_id="other-entry",
    )
    monkeypatch.setattr(settings, "auto_backup_retain_per_entity", 1)

    latest = await manager.maybe_snapshot(
        "helper_template", template_entry.entry_id, force=True
    )

    assert latest is not None and latest.exists()
    assert not old.exists()
    assert other.exists()


@pytest.mark.parametrize("enabled", [False, True])
async def test_alias_removal_uses_core_identity_and_waits_for_restore(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    template_entry: ConfigEntry,
    settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    monkeypatch.setattr(settings, "enable_auto_backup", enabled)
    _rename(entity_registry, template_entry, "sensor.secondary")
    manager = bm.get_backup_manager(core_client, settings)
    monkeypatch.setattr(auto_backup, "get_backup_manager", lambda *_: manager)
    async with manager.config_entry_write_guard(template_entry.entry_id):
        task = asyncio.create_task(
            IntegrationTools(core_client).ha_remove_helpers_integrations(
                target="sensor.secondary",
                helper_type="template",
                confirm=True,
                wait=False,
            )
        )
        done, _ = await asyncio.wait({task}, timeout=0.2)
        assert not done
        assert hass.config_entries.async_get_entry(template_entry.entry_id)
    await task

    assert hass.config_entries.async_get_entry(template_entry.entry_id) is None
    assert not list(manager.backup_dir.glob("*.yaml"))


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("first_operation", ["edit", "restore"])
@pytest.mark.parametrize("edit_domain", ["helper_template", "integration"])
async def test_helper_edit_and_restore_serialize_capture_through_mutation(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    template_entry: ConfigEntry,
    settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    first_operation: str,
    edit_domain: str,
) -> None:
    monkeypatch.setattr(settings, "enable_auto_backup", enabled)
    manager = bm.get_backup_manager(component_core_client, settings)
    monkeypatch.setattr(auto_backup, "get_backup_manager", lambda *_: manager)
    source = await _real_snapshot(manager, template_entry)
    await set_template_state(hass, template_entry, "initial")
    entered, release = asyncio.Event(), asyncio.Event()

    @auto_backup.with_auto_backup(
        domain=edit_domain, id_param="helper_id", client=component_core_client
    )
    async def edit(*, helper_id: str) -> dict[str, Any]:
        if first_operation == "edit":
            entered.set()
            await release.wait()
        await set_template_state(hass, template_entry, "edited")
        return {"success": True}

    async def run(operation: str) -> dict[str, Any]:
        if operation == "edit":
            return await edit(helper_id=template_entry.entry_id)
        return await manager.restore_snapshot(source.name)

    # A restore that runs first has its safety capture's read held in Core,
    # with the write guard taken; the edit never gets that far.
    async with held_command(hass, "ha_mcp_tools/helpers_list", first_only=True) as held:
        if first_operation == "edit":
            held.release.set()
        first = asyncio.create_task(run(first_operation))
        await asyncio.wait_for(
            (entered if first_operation == "edit" else held.arrived).wait(), 2
        )
        second = asyncio.create_task(
            run("restore" if first_operation == "edit" else "edit")
        )
        try:
            for _ in range(20):
                await asyncio.sleep(0)
            assert template_entry.options["state"] == state_template("initial")
        finally:
            release.set()
            held.release.set()
            results = await asyncio.gather(first, second)

    if first_operation == "edit":
        safety = manager.read_snapshot(results[1]["safety_backup"])
        assert safety["config"]["options"]["state"] == state_template("edited")
        assert template_entry.options["state"] == state_template("saved")
    else:
        assert template_entry.options["state"] == state_template("edited")
