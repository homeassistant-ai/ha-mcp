"""Restoring a label, category, area or floor from its auto-backup, on real Core.

A snapshot is captured from Core's registry through the server's own client; the
item is then changed or deleted in Core, and the restore runs through the backup
manager's public entry point. Every registry row read back is Core's.
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr

from ha_mcp.backup_diff import DiffResponse
from ha_mcp.backup_manager import BackupManager
from ha_mcp.backup_registry_items import restored_item
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantCommandError
from ha_mcp.utils.registry_update_lock import registry_update_lock


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


async def test_a_deleted_area_is_created_again(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """Core refuses an update of an id it no longer holds, so the restore
    creates the area from the snapshot."""
    registry = ar.async_get(hass)
    area = registry.async_create("Hall", icon="mdi:door", aliases={"Entry"})
    snapshot = await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}")
    registry.async_delete(area.id)

    await backup_manager.restore_snapshot(snapshot)

    assert [(a.name, a.icon, a.aliases) for a in registry.areas.values()] == [
        ("Hall", "mdi:door", {"Entry"})
    ]


async def test_a_second_restore_of_a_renamed_area_takes_the_first_ones_area(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """Core derives a created area's id from its name, like a label's."""
    registry = ar.async_get(hass)
    area = registry.async_create("Hall", icon="mdi:door")
    registry.async_update(area.id, name="Corridor")
    snapshot = await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}")
    registry.async_delete(area.id)

    await backup_manager.restore_snapshot(snapshot)
    await backup_manager.restore_snapshot(snapshot)

    assert [a.name for a in registry.areas.values()] == ["Corridor"]


async def test_an_area_changed_since_the_snapshot_is_updated_in_place(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    registry = ar.async_get(hass)
    area = registry.async_create("Hall")
    snapshot = await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}")
    registry.async_update(area.id, name="Corridor")

    await backup_manager.restore_snapshot(snapshot)

    restored = registry.async_get_area(area.id)
    assert restored is not None
    assert restored.name == "Hall"
    assert len(registry.areas) == 1


async def test_a_second_restore_of_a_renamed_floor_takes_the_first_ones_floor(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    registry = fr.async_get(hass)
    floor = registry.async_create("Ground", level=0)
    registry.async_update(floor.floor_id, name="Base")
    snapshot = await _snapshot(
        backup_manager, "area_or_floor", f"floor:{floor.floor_id}"
    )
    registry.async_delete(floor.floor_id)

    await backup_manager.restore_snapshot(snapshot)
    await backup_manager.restore_snapshot(snapshot)

    assert [(f.name, f.level) for f in registry.floors.values()] == [("Base", 0)]


async def test_a_deleted_area_without_an_icon_is_created_again(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """The snapshot row carries ``icon: None``, which Core's create refuses
    (it takes a string or nothing)."""
    registry = ar.async_get(hass)
    area = registry.async_create("Hall")
    snapshot = await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}")
    registry.async_delete(area.id)

    await backup_manager.restore_snapshot(snapshot)

    assert [(a.name, a.icon) for a in registry.areas.values()] == [("Hall", None)]


async def test_a_restored_area_keeps_its_floor(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """``floor_id`` is the id of a floor but a field of an area."""
    floor = fr.async_get(hass).async_create("Ground")
    registry = ar.async_get(hass)
    area = registry.async_create("Hall", floor_id=floor.floor_id)
    snapshot = await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}")
    registry.async_update(area.id, floor_id=None)
    await backup_manager.restore_snapshot(snapshot)
    registry.async_delete(area.id)

    await backup_manager.restore_snapshot(snapshot)

    assert [(a.name, a.floor_id) for a in registry.areas.values()] == [
        ("Hall", floor.floor_id)
    ]


async def test_a_second_restore_matches_list_fields_in_any_order(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """Core keeps aliases in a set and lists them in hash order, so the order the
    snapshot holds is not the one a restored area lists them in."""
    registry = ar.async_get(hass)
    aliases = {f"alias {i}" for i in range(60)}
    area = registry.async_create("Hall", icon="mdi:door", aliases=aliases)
    registry.async_update(area.id, name="Corridor")
    snapshot = await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}")
    registry.async_delete(area.id)

    await backup_manager.restore_snapshot(snapshot)
    await backup_manager.restore_snapshot(snapshot)

    assert [(a.name, a.aliases) for a in registry.areas.values()] == [
        ("Corridor", aliases)
    ]


async def test_a_second_restore_takes_an_area_that_lost_a_deleted_label(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """Core drops the label id it no longer holds from the created area, so the
    area's labels differ from the snapshot's on the second restore."""
    labels = lr.async_get(hass)
    kept = labels.async_create("Kept")
    gone = labels.async_create("Gone")
    registry = ar.async_get(hass)
    area = registry.async_create("Hall", labels={kept.label_id, gone.label_id})
    registry.async_update(area.id, name="Corridor")
    snapshot = await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}")
    registry.async_delete(area.id)
    labels.async_delete(gone.label_id)

    await backup_manager.restore_snapshot(snapshot)
    await backup_manager.restore_snapshot(snapshot)

    assert [(a.name, a.labels) for a in registry.areas.values()] == [
        ("Corridor", {kept.label_id})
    ]


async def test_the_restore_of_a_deleted_item_says_it_no_longer_exists(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """The item is gone, not renamed: the restore creates it again and
    overwrites none of its state, which is what the warning says."""
    labels = lr.async_get(hass)
    label = labels.async_create("Security")
    categories = cr.async_get(hass)
    category = categories.async_create(scope="automation", name="Lighting")
    areas = ar.async_get(hass)
    area = areas.async_create("Hall")
    floors = fr.async_get(hass)
    floor = floors.async_create("Ground")
    ids = {
        "label": label.label_id,
        "category": f"automation:{category.category_id}",
        "area_or_floor": f"area:{area.id}",
    }
    snapshots = {
        domain: await _snapshot(backup_manager, domain, entity_id)
        for domain, entity_id in ids.items()
    }
    snapshots["floor"] = await _snapshot(
        backup_manager, "area_or_floor", f"floor:{floor.floor_id}"
    )
    ids["floor"] = f"floor:{floor.floor_id}"
    labels.async_delete(label.label_id)
    categories.async_delete(scope="automation", category_id=category.category_id)
    areas.async_delete(area.id)
    floors.async_delete(floor.floor_id)

    for domain, snapshot in snapshots.items():
        outcome = await backup_manager.restore_snapshot(snapshot)
        assert outcome["warnings"] == [
            f"No safety backup was taken: {ids[domain]} no longer exists, so this "
            "restore overwrites none of its state."
        ], domain


async def test_a_diff_after_a_restore_finds_the_item_under_its_new_id(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """A restored category has another id than the snapshot's, and so does a
    renamed label, area or floor; the diff finds them by their fields."""
    labels = lr.async_get(hass)
    label = labels.async_create("Security")
    labels.async_update(label.label_id, name="Alarm")
    categories = cr.async_get(hass)
    category = categories.async_create(scope="automation", name="Lighting")
    other = categories.async_create(scope="script", name="Night")
    areas = ar.async_get(hass)
    area = areas.async_create("Hall", icon="mdi:door")
    areas.async_update(area.id, name="Corridor")
    floors = fr.async_get(hass)
    floor = floors.async_create("Ground", level=0)
    floors.async_update(floor.floor_id, name="Base")
    snapshots = [
        await _snapshot(backup_manager, "label", label.label_id),
        await _snapshot(
            backup_manager, "category", f"automation:{category.category_id}"
        ),
        await _snapshot(backup_manager, "category", f"script:{other.category_id}"),
        await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}"),
        await _snapshot(backup_manager, "area_or_floor", f"floor:{floor.floor_id}"),
    ]
    labels.async_delete(label.label_id)
    categories.async_delete(scope="automation", category_id=category.category_id)
    categories.async_delete(scope="script", category_id=other.category_id)
    areas.async_delete(area.id)
    floors.async_delete(floor.floor_id)

    for snapshot in snapshots:
        await backup_manager.restore_snapshot(snapshot)
        diff = await backup_manager.diff_snapshot(snapshot)
        assert (diff["entity_missing"], diff["unchanged"]) == (False, True), snapshot


async def test_a_diff_after_a_restore_shows_the_label_core_dropped(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """The restored area is found although it lost a deleted label; the lost
    label is the one difference the diff reports."""
    labels = lr.async_get(hass)
    gone = labels.async_create("Gone")
    areas = ar.async_get(hass)
    area = areas.async_create("Hall", labels={gone.label_id})
    areas.async_update(area.id, name="Corridor")
    snapshot = await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}")
    areas.async_delete(area.id)
    labels.async_delete(gone.label_id)
    await backup_manager.restore_snapshot(snapshot)

    diff = await backup_manager.diff_snapshot(snapshot)

    assert diff["entity_missing"] is False
    assert cast(DiffResponse, diff)["patch"] == [
        {"op": "add", "path": "/labels/-", "value": gone.label_id}
    ]


async def test_a_diff_of_a_present_item_changed_since_the_snapshot_is_not_missing(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """The item still holds its id, so the lookup by fields stays out of it."""
    labels = lr.async_get(hass)
    label = labels.async_create("Security")
    categories = cr.async_get(hass)
    category = categories.async_create(scope="automation", name="Lighting")
    areas = ar.async_get(hass)
    area = areas.async_create("Hall")
    floors = fr.async_get(hass)
    floor = floors.async_create("Ground")
    snapshots = [
        await _snapshot(backup_manager, "label", label.label_id),
        await _snapshot(
            backup_manager, "category", f"automation:{category.category_id}"
        ),
        await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}"),
        await _snapshot(backup_manager, "area_or_floor", f"floor:{floor.floor_id}"),
    ]
    labels.async_update(label.label_id, color="red")
    categories.async_update(
        scope="automation", category_id=category.category_id, icon="mdi:lamp"
    )
    areas.async_update(area.id, icon="mdi:door")
    floors.async_update(floor.floor_id, icon="mdi:stairs")

    for snapshot in snapshots:
        diff = await backup_manager.diff_snapshot(snapshot)
        assert (diff["entity_missing"], diff["unchanged"]) == (False, False), snapshot


async def test_a_diff_of_a_deleted_item_still_says_it_is_missing(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    labels = lr.async_get(hass)
    label = labels.async_create("Security")
    categories = cr.async_get(hass)
    category = categories.async_create(scope="automation", name="Lighting")
    areas = ar.async_get(hass)
    area = areas.async_create("Hall")
    floors = fr.async_get(hass)
    floor = floors.async_create("Ground")
    snapshots = [
        await _snapshot(backup_manager, "label", label.label_id),
        await _snapshot(
            backup_manager, "category", f"automation:{category.category_id}"
        ),
        await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}"),
        await _snapshot(backup_manager, "area_or_floor", f"floor:{floor.floor_id}"),
    ]
    labels.async_delete(label.label_id)
    categories.async_delete(scope="automation", category_id=category.category_id)
    areas.async_delete(area.id)
    floors.async_delete(floor.floor_id)

    for snapshot in snapshots:
        diff = await backup_manager.diff_snapshot(snapshot)
        assert (diff["entity_missing"], diff["unchanged"]) == (True, False), snapshot


@pytest.mark.usefixtures("backup_manager")  # loads Core's config commands
async def test_a_snapshot_without_a_name_matches_no_live_item(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Every capture holds a name; without one any row would match."""
    labels = lr.async_get(hass)
    labels.async_create("Unrelated")

    found = await restored_item(
        core_client, "label", "security", {"label_id": "security"}
    )

    assert found is None


async def test_a_floor_changed_since_the_snapshot_is_updated_in_place(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    registry = fr.async_get(hass)
    floor = registry.async_create("Ground", level=0)
    snapshot = await _snapshot(
        backup_manager, "area_or_floor", f"floor:{floor.floor_id}"
    )
    registry.async_update(floor.floor_id, name="Base")

    await backup_manager.restore_snapshot(snapshot)

    restored = registry.async_get_floor(floor.floor_id)
    assert restored is not None
    assert restored.name == "Ground"
    assert len(registry.floors) == 1


async def test_an_area_restore_waits_for_the_registry_update_lock(
    hass: HomeAssistant,
    backup_manager: BackupManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restore reads the registry and writes under the lock the area tool
    takes, so a concurrent update cannot land between the two."""
    registry = ar.async_get(hass)
    area = registry.async_create("Hall", icon="mdi:door")
    snapshot = await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}")
    registry.async_delete(area.id)

    asked = asyncio.Event()

    def watched_lock(registry_name: str, resource_id: str) -> asyncio.Lock:
        if (registry_name, resource_id) == ("area", area.id):
            asked.set()
        return registry_update_lock(registry_name, resource_id)

    monkeypatch.setattr("ha_mcp.backup_manager.registry_update_lock", watched_lock)
    async with registry_update_lock("area", area.id):
        restore = asyncio.ensure_future(backup_manager.restore_snapshot(snapshot))
        try:
            # The restore asks for the lock, then waits on it with no await
            # in between: one more turn of the loop finds it blocked.
            await asyncio.wait_for(asked.wait(), 5)
            await asyncio.sleep(0)
            assert not restore.done()
            assert registry.async_get_area(area.id) is None
        except BaseException:
            restore.cancel()
            await asyncio.gather(restore, return_exceptions=True)
            raise
    await asyncio.wait_for(restore, 5)

    assert registry.async_get_area(area.id) is not None


async def test_a_restore_reads_areas_and_categories_through_the_component(
    hass: HomeAssistant,
    backup_manager: BackupManager,
    component_core_client: HomeAssistantClient,
) -> None:
    """With the component installed the registry read takes its route; a
    second restore still finds the first one's item."""
    areas = ar.async_get(hass)
    area = areas.async_create("Hall", icon="mdi:door")
    areas.async_update(area.id, name="Corridor")
    area_snapshot = await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}")
    areas.async_delete(area.id)
    categories = cr.async_get(hass)
    category = categories.async_create(scope="automation", name="Lighting")
    category_snapshot = await _snapshot(
        backup_manager, "category", f"automation:{category.category_id}"
    )
    categories.async_delete(scope="automation", category_id=category.category_id)

    for snapshot in (
        area_snapshot,
        area_snapshot,
        category_snapshot,
        category_snapshot,
    ):
        await backup_manager.restore_snapshot(snapshot)

    assert [a.name for a in areas.areas.values()] == ["Corridor"]
    assert [c.name for c in categories.categories["automation"].values()] == [
        "Lighting"
    ]


async def test_an_area_restored_without_a_deleted_label_says_so(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """Core drops a label id it no longer holds and still answers success, so
    the restore names it."""
    labels = lr.async_get(hass)
    gone = labels.async_create("Gone")
    kept = labels.async_create("Kept")
    registry = ar.async_get(hass)
    area = registry.async_create(
        "Hall", icon="mdi:door", labels={gone.label_id, kept.label_id}
    )
    snapshot = await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}")
    labels.async_delete(gone.label_id)
    registry.async_delete(area.id)

    outcome = await backup_manager.restore_snapshot(snapshot)

    assert outcome["result"]["warnings"] == [
        "The area was restored without the labels gone: they no longer exist."
    ]
    restored = registry.async_get_area(area.id)
    assert restored is not None
    assert restored.labels == {kept.label_id}


async def test_an_area_restored_with_its_labels_has_no_label_warning(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    labels = lr.async_get(hass)
    kept = labels.async_create("Kept")
    registry = ar.async_get(hass)
    area = registry.async_create("Hall", icon="mdi:door", labels={kept.label_id})
    snapshot = await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}")
    registry.async_delete(area.id)

    outcome = await backup_manager.restore_snapshot(snapshot)

    assert "warnings" not in outcome["result"]
