"""Snapshots of a label, category, area or floor for the backup restore and diff tests."""

from __future__ import annotations

from typing import Any

import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import label_registry as lr

from ha_mcp.backup_manager import BackupManager


async def _snapshot(backup_manager: BackupManager, domain: str, entity_id: str) -> str:
    path = await backup_manager.maybe_snapshot(domain, entity_id, force=True)
    assert path is not None
    return path.name


def _store(backup_manager: BackupManager, snapshot: str, **values: Any) -> None:
    """Edit the snapshot to hold ``values`` in its row."""
    path = next(backup_manager.backup_dir.rglob(snapshot))
    stored = yaml.safe_load(path.read_text())
    stored["config"].update(values)
    path.write_text(yaml.safe_dump(stored))


def _store_reversed(
    backup_manager: BackupManager, snapshot: str, area: ar.AreaEntry, *keys: str
) -> None:
    """Edit the snapshot to hold ``keys`` in the reverse of Core's order."""
    _store(
        backup_manager,
        snapshot,
        **{key: list(reversed(list(getattr(area, key)))) for key in keys},
    )


async def _second_restore_with_lists_reversed(
    hass: HomeAssistant, backup_manager: BackupManager, *keys: str
) -> str:
    """Restore a deleted area under another id, then hand back its snapshot,
    edited to hold ``keys`` in the reverse of the order Core lists them in."""
    labels = lr.async_get(hass)
    ids = {labels.async_create(name).label_id for name in ("One", "Two")}
    registry = ar.async_get(hass)
    area = registry.async_create("Hall", aliases={"entrance", "lobby"}, labels=ids)
    registry.async_update(area.id, name="Corridor")
    snapshot = await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}")
    registry.async_delete(area.id)
    await backup_manager.restore_snapshot(snapshot)
    (restored,) = registry.async_list_areas()
    _store_reversed(backup_manager, snapshot, restored, *keys)
    return snapshot
