"""Template identity validation scales with changed files, including legacy aliases.

Snapshots are captured from a real Template helper that Core's own config flow
created; files an older release or an import left behind are written next to
them from such a capture.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient

DOMAIN = "helper_template"


@pytest.fixture
def settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        enable_auto_backup=True,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=5,
        auto_backup_dir=str(tmp_path),
    )


@pytest.fixture
async def entry_id(hass: HomeAssistant) -> str:
    """The entry id of a Template sensor helper made by Core's config flow."""
    assert await async_setup_component(hass, "config", {})
    result = await hass.config_entries.flow.async_init(
        "template", context={"source": "user"}
    )
    for answer in ({"next_step_id": "sensor"}, {"name": "Example", "state": "{{ 1 }}"}):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], answer
        )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result["result"].entry_id


@pytest.fixture
def manager(
    component_core_client: HomeAssistantClient, settings: SimpleNamespace
) -> bm.BackupManager:
    return bm.get_backup_manager(component_core_client, settings)


async def _capture(manager: bm.BackupManager, entry_id: str) -> Path:
    """Capture the helper; the manager rotates its history right after."""
    path = await manager.maybe_snapshot(DOMAIN, entry_id)
    assert path is not None
    return path


async def _captured_data(manager: bm.BackupManager, entry_id: str) -> dict[str, Any]:
    """What a capture of the helper holds, with that capture removed again."""
    path = await _capture(manager, entry_id)
    data = manager.read_snapshot(path.name)
    manager.delete_snapshot(path.name)
    return data


def _place(manager: bm.BackupManager, entity_id: str, data: dict[str, Any]) -> Path:
    """A snapshot file under ``entity_id`` as an older release or import left it."""
    path = manager.backup_dir / f"{DOMAIN}.{entity_id}.20200101_000000.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.mark.parametrize("unrelated_count", [10, 100])
async def test_rotation_does_not_reparse_unchanged_histories(
    manager: bm.BackupManager,
    settings: SimpleNamespace,
    entry_id: str,
    unrelated_count: int,
) -> None:
    settings.auto_backup_retain_per_entity = 2
    data = await _captured_data(manager, entry_id)
    for number in range(unrelated_count):
        other = f"other-{number}"
        _place(
            manager,
            other,
            {
                **data,
                "entity_id": other,
                "config": {**data["config"], "entry_id": other},
            },
        )
    legacy = _place(
        manager, "sensor.old_name", {**data, "entity_id": "sensor.old_name"}
    )
    with patch.object(manager, "read_snapshot", wraps=manager.read_snapshot) as read:
        current = await _capture(manager, entry_id)
        assert (
            read.call_count == unrelated_count + 2
        )  # Discover and validate all history once.
        read.reset_mock()
        manager.list_snapshots(entity_id=entry_id)
        assert read.call_count == 0, (
            "Unchanged histories should not require full YAML parsing"
        )
        added = await _capture(manager, entry_id)
        assert read.call_count == 1, (
            "Only the newly captured file needs identity validation"
        )
    assert len(list(manager.backup_dir.glob("*.yaml"))) == unrelated_count + 2
    assert added.exists()
    assert len([path for path in (legacy, current) if path.exists()]) == 1


async def test_changed_legacy_identity_is_revalidated_before_filter_or_delete(
    manager: bm.BackupManager, entry_id: str
) -> None:
    data = await _captured_data(manager, entry_id)
    path = _place(manager, "sensor.reused", {**data, "entity_id": "sensor.reused"})
    assert manager.list_snapshots(entity_id=entry_id)
    data = manager.read_snapshot(path.name)
    data["config"]["entry_id"] = "a-different-entry"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert manager.list_snapshots(entity_id=entry_id) == []
    assert manager.delete_bulk(entity_id=entry_id)["deleted"] == []
    assert manager.list_snapshots(entity_id="a-different-entry")[0]["name"] == path.name
    assert manager.delete_bulk(entity_id="a-different-entry")["deleted"] == [path.name]


async def test_invalid_changed_identity_is_not_rotated_or_deleted(
    manager: bm.BackupManager, settings: SimpleNamespace, entry_id: str
) -> None:
    path = await _capture(manager, entry_id)
    assert manager.list_snapshots(entity_id=entry_id)
    data = manager.read_snapshot(path.name)
    data["config"]["entry_id"] = "conflicting-entry"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    settings.auto_backup_retain_per_entity = 1
    await _capture(manager, entry_id)
    manager.delete_bulk(entity_id=entry_id)
    assert path.exists()


async def test_imported_legacy_history_is_discovered_after_rotation(
    manager: bm.BackupManager, settings: SimpleNamespace, entry_id: str
) -> None:
    # Room for the two real captures, so only a discovered import is rotated.
    settings.auto_backup_retain_per_entity = 2
    current = await _capture(manager, entry_id)
    data = manager.read_snapshot(current.name)
    legacy = _place(
        manager, "sensor.old_name", {**data, "entity_id": "sensor.old_name"}
    )
    added = await _capture(manager, entry_id)
    assert current.exists()
    assert not legacy.exists()
    assert added.exists()


@pytest.mark.parametrize("delete_method", ["single", "bulk", "external"])
async def test_reused_filename_does_not_keep_deleted_identity(
    manager: bm.BackupManager, entry_id: str, delete_method: str
) -> None:
    data = await _captured_data(manager, entry_id)
    path = _place(manager, "sensor.reused", {**data, "entity_id": "sensor.reused"})
    assert manager.list_snapshots(entity_id=entry_id)
    data = manager.read_snapshot(path.name)
    if delete_method == "single":
        manager.delete_snapshot(path.name)
    elif delete_method == "bulk":
        manager.delete_bulk(entity_id=entry_id)
    else:
        path.unlink()
    data["config"]["entry_id"] = "a-different-entry"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    assert manager.list_snapshots(entity_id=entry_id) == []
    assert manager.list_snapshots(entity_id="a-different-entry")[0]["name"] == path.name


async def test_identity_changed_during_read_is_not_used_for_deletion(
    manager: bm.BackupManager, entry_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = await _captured_data(manager, entry_id)
    path = _place(manager, "sensor.reused", {**data, "entity_id": "sensor.reused"})
    original_read = manager.read_snapshot

    def read_then_replace(name: str) -> dict[str, Any]:
        read = original_read(name)
        changed = {**read, "config": {**read["config"], "entry_id": "different-entry"}}
        path.write_text(yaml.safe_dump(changed), encoding="utf-8")
        return read

    monkeypatch.setattr(manager, "read_snapshot", read_then_replace)
    assert manager.delete_bulk(entity_id=entry_id)["deleted"] == []
    assert path.exists()
    monkeypatch.setattr(manager, "read_snapshot", original_read)
    assert manager.list_snapshots(entity_id="different-entry")[0]["name"] == path.name
