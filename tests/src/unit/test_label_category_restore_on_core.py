"""Restoring a label or category from its auto-backup, on real Core.

A snapshot is captured from Core's registry through the server's own client; the
item is then changed or deleted in Core, and the restore runs through the backup
manager's public entry point. Every registry row read back is Core's.
"""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import label_registry as lr

from ha_mcp.backup_manager import BackupManager
from ha_mcp.client.rest_client import HomeAssistantCommandError


async def _snapshot(backup_manager: BackupManager, domain: str, entity_id: str) -> str:
    path = await backup_manager.maybe_snapshot(domain, entity_id, force=True)
    assert path is not None
    return path.name


async def test_a_label_changed_since_the_snapshot_is_updated_in_place(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    registry = lr.async_get(hass)
    label = registry.async_create("Security", color="red")
    snapshot = await _snapshot(backup_manager, "label", label.label_id)
    registry.async_update(label.label_id, name="Alarm", color="blue")

    await backup_manager.restore_snapshot(snapshot)

    restored = registry.async_get_label(label.label_id)
    assert restored is not None
    assert (restored.name, restored.color) == ("Security", "red")
    assert len(registry.labels) == 1


async def test_a_deleted_label_is_created_again(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """Core refuses an update of an id it no longer holds, so the restore
    creates the label from the snapshot."""
    registry = lr.async_get(hass)
    label = registry.async_create(
        "Security", color="red", icon="mdi:shield", description="Locks"
    )
    snapshot = await _snapshot(backup_manager, "label", label.label_id)
    registry.async_delete(label.label_id)

    await backup_manager.restore_snapshot(snapshot)

    restored = registry.async_get_label(label.label_id)
    assert restored is not None
    assert (restored.name, restored.color, restored.icon, restored.description) == (
        "Security",
        "red",
        "mdi:shield",
        "Locks",
    )


async def test_a_restored_label_does_not_carry_what_carried_it(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """Core takes a deleted label off the areas that carried it; the restore
    brings back the label, not those assignments (the delete tool's docstring
    says so)."""
    registry = lr.async_get(hass)
    label = registry.async_create("Security")
    area = ar.async_get(hass).async_create("Hall", labels={label.label_id})
    snapshot = await _snapshot(backup_manager, "label", label.label_id)
    registry.async_delete(label.label_id)

    await backup_manager.restore_snapshot(snapshot)

    assert registry.async_get_label(label.label_id) is not None
    stored = ar.async_get(hass).async_get_area(area.id)
    assert stored is not None
    assert stored.labels == set()


async def test_a_second_restore_of_a_renamed_label_takes_the_first_ones_label(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """Core derives a created label's id from its name, so the id the snapshot
    holds is not the one the restored label gets: a second restore finds the
    first one's label by its content."""
    registry = lr.async_get(hass)
    label = registry.async_create("Alpha", color="red")
    registry.async_update(label.label_id, name="Beta")
    snapshot = await _snapshot(backup_manager, "label", label.label_id)
    registry.async_delete(label.label_id)

    await backup_manager.restore_snapshot(snapshot)
    await backup_manager.restore_snapshot(snapshot)

    assert [(lbl.name, lbl.color) for lbl in registry.labels.values()] == [
        ("Beta", "red")
    ]


async def test_a_taken_label_name_is_refused_not_overwritten(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """A label that merely shares the name is another label: the restore reports
    Core's refusal and leaves it alone."""
    registry = lr.async_get(hass)
    label = registry.async_create("Alpha", color="red")
    registry.async_update(label.label_id, name="Beta")
    snapshot = await _snapshot(backup_manager, "label", label.label_id)
    registry.async_delete(label.label_id)
    other = registry.async_create("Beta", color="green")

    with pytest.raises(HomeAssistantCommandError, match="already in use"):
        await backup_manager.restore_snapshot(snapshot)

    kept = registry.async_get_label(other.label_id)
    assert kept is not None
    assert kept.color == "green"


async def test_a_category_changed_since_the_snapshot_is_updated_in_place(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    registry = cr.async_get(hass)
    category = registry.async_create(scope="automation", name="Lighting")
    snapshot = await _snapshot(
        backup_manager, "category", f"automation:{category.category_id}"
    )
    registry.async_update(
        scope="automation", category_id=category.category_id, name="Lamps"
    )

    await backup_manager.restore_snapshot(snapshot)

    restored = registry.async_get_category(
        scope="automation", category_id=category.category_id
    )
    assert restored is not None
    assert restored.name == "Lighting"


async def test_a_second_restore_of_a_deleted_category_takes_the_first_ones_category(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """Core picks a created category's id, so the restored category has a new
    one; restoring again finds it by its content instead of creating a second."""
    registry = cr.async_get(hass)
    category = registry.async_create(
        scope="automation", name="Lighting", icon="mdi:lamp"
    )
    snapshot = await _snapshot(
        backup_manager, "category", f"automation:{category.category_id}"
    )
    registry.async_delete(scope="automation", category_id=category.category_id)

    await backup_manager.restore_snapshot(snapshot)
    await backup_manager.restore_snapshot(snapshot)

    rows = list(registry.categories["automation"].values())
    assert [(row.name, row.icon) for row in rows] == [("Lighting", "mdi:lamp")]
    assert rows[0].category_id != category.category_id
