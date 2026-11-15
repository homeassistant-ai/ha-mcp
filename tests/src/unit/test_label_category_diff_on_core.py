"""The backup diff of a label, category, area or floor, on real Core.

A snapshot is captured from Core's registry through the server's own client; the
item is then changed, deleted or restored in Core, and the diff runs through the
backup manager's public entry point. Every registry row compared is Core's.
"""

from __future__ import annotations

from typing import cast

import orjson
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr

from custom_components.ha_mcp_tools.websocket_api import registries
from ha_mcp.backup_diff import DiffResponse
from ha_mcp.backup_manager import BackupManager
from ha_mcp.backup_registry_items import reordered_lists
from ha_mcp.client.rest_client import HomeAssistantClient

from ._command_faults import received
from ._registry_item_snapshots import (
    _second_restore_with_lists_reversed,
    _snapshot,
    _store_reversed,
)

_FIELDS = "ha_mcp_tools/registry_fields"


async def test_a_diff_after_a_restore_finds_the_item_and_reports_its_new_id(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """A restored category has another id than the snapshot's, and so does a
    renamed label, area or floor. The diff finds them by their name and reports
    the id as the one difference, instead of calling them unchanged; an
    unrelated item of the same registry is not taken for them."""
    labels = lr.async_get(hass)
    labels.async_create("Unrelated")
    label = labels.async_create("Security")
    labels.async_update(label.label_id, name="Alarm")
    categories = cr.async_get(hass)
    categories.async_create(scope="automation", name="Unrelated")
    categories.async_create(scope="script", name="Unrelated")
    category = categories.async_create(scope="automation", name="Lighting")
    other = categories.async_create(scope="script", name="Night")
    areas = ar.async_get(hass)
    areas.async_create("Unrelated")
    area = areas.async_create("Hall", icon="mdi:door")
    areas.async_update(area.id, name="Corridor")
    floors = fr.async_get(hass)
    floors.async_create("Unrelated")
    floor = floors.async_create("Ground", level=0)
    floors.async_update(floor.floor_id, name="Base")
    wanted = {
        await _snapshot(backup_manager, "label", label.label_id): (
            "label_id",
            label.label_id,
        ),
        await _snapshot(
            backup_manager, "category", f"automation:{category.category_id}"
        ): ("category_id", category.category_id),
        await _snapshot(backup_manager, "category", f"script:{other.category_id}"): (
            "category_id",
            other.category_id,
        ),
        await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}"): (
            "area_id",
            area.id,
        ),
        await _snapshot(backup_manager, "area_or_floor", f"floor:{floor.floor_id}"): (
            "floor_id",
            floor.floor_id,
        ),
    }
    labels.async_delete(label.label_id)
    categories.async_delete(scope="automation", category_id=category.category_id)
    categories.async_delete(scope="script", category_id=other.category_id)
    areas.async_delete(area.id)
    floors.async_delete(floor.floor_id)

    for snapshot, (id_key, old_id) in wanted.items():
        await backup_manager.restore_snapshot(snapshot)
        diff = cast(DiffResponse, await backup_manager.diff_snapshot(snapshot))
        assert (diff["entity_missing"], diff["unchanged"]) == (False, False), snapshot
        assert [(op["path"], op["value"]) for op in diff["patch"]] == [
            (f"/{id_key}", old_id)
        ], snapshot


async def _restored_under_the_same_id(
    hass: HomeAssistant, backup_manager: BackupManager
) -> list[str]:
    """The snapshots of a label, an area and a floor that were deleted and
    restored. None was ever renamed, so each came back under its own id, with
    the points in time of its new creation."""
    labels = lr.async_get(hass)
    label = labels.async_create("Security")
    areas = ar.async_get(hass)
    area = areas.async_create("Hall", icon="mdi:door")
    floors = fr.async_get(hass)
    floor = floors.async_create("Ground", level=0)
    snapshots = [
        await _snapshot(backup_manager, "label", label.label_id),
        await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}"),
        await _snapshot(backup_manager, "area_or_floor", f"floor:{floor.floor_id}"),
    ]
    labels.async_delete(label.label_id)
    areas.async_delete(area.id)
    floors.async_delete(floor.floor_id)
    for snapshot in snapshots:
        await backup_manager.restore_snapshot(snapshot)
    return snapshots


async def test_a_diff_after_a_restore_under_the_same_id_ignores_the_timestamps(
    hass: HomeAssistant,
    backup_manager: BackupManager,
    component_core_client: HomeAssistantClient,
) -> None:
    """The points in time Core set on the restored item are no difference the
    snapshot can restore. Which fields those are is the component's answer,
    asked once per diff for the row's own registry."""
    snapshots = await _restored_under_the_same_id(hass, backup_manager)

    with received(hass, _FIELDS) as seen:
        diffs = [await backup_manager.diff_snapshot(name) for name in snapshots]

    for diff in diffs:
        assert (diff["entity_missing"], diff["unchanged"]) == (False, True), diff
        assert "warnings" not in diff
    assert [msg["registry"] for msg in seen[_FIELDS]] == ["label", "area", "floor"]


async def test_a_diff_without_the_component_ignores_the_timestamps_too(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """Without the component the diff of a label, area or floor leaves out
    ``created_at`` and ``modified_at``, as it did before the component named
    them, and has nothing to warn about."""
    snapshots = await _restored_under_the_same_id(hass, backup_manager)

    for snapshot in snapshots:
        diff = await backup_manager.diff_snapshot(snapshot)
        assert (diff["entity_missing"], diff["unchanged"]) == (False, True), diff
        assert "warnings" not in diff


async def test_a_diff_of_an_entry_the_component_does_not_find_ignores_the_timestamps(
    hass: HomeAssistant,
    backup_manager: BackupManager,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core can drop an entry between the diff's read and its question. The
    component then names no point in time, and the diff leaves out the two it
    leaves out without a component."""
    snapshots = await _restored_under_the_same_id(hass, backup_manager)
    monkeypatch.setattr(registries, "_registry_entry", lambda *args: None)

    for snapshot in snapshots:
        diff = await backup_manager.diff_snapshot(snapshot)
        assert (diff["entity_missing"], diff["unchanged"]) == (False, True), diff


async def test_a_diff_after_a_restore_ignores_the_order_core_lists_aliases_in(
    hass: HomeAssistant,
    backup_manager: BackupManager,
    component_core_client: HomeAssistantClient,
) -> None:
    """Core keeps aliases in a set, so the order the snapshot holds them in is
    no difference: the snapshot file is edited to hold them in the reverse of the
    order Core lists the restored area's in."""
    areas = ar.async_get(hass)
    area = areas.async_create("Hall", aliases={"entrance", "lobby"})
    snapshot = await _snapshot(backup_manager, "area_or_floor", f"area:{area.id}")
    areas.async_delete(area.id)
    await backup_manager.restore_snapshot(snapshot)
    (restored,) = areas.async_list_areas()
    _store_reversed(backup_manager, snapshot, restored, "aliases")

    diff = await backup_manager.diff_snapshot(snapshot)

    assert (diff["entity_missing"], diff["unchanged"]) == (False, True), diff


async def test_a_diff_after_a_restore_under_another_id_ignores_the_alias_order(
    hass: HomeAssistant,
    backup_manager: BackupManager,
    component_core_client: HomeAssistantClient,
) -> None:
    """The area comes back under a new id, so the diff finds it by name. Its
    aliases are compared without regard to order: the snapshot file is edited to
    hold them in the reverse of the order Core lists them in."""
    snapshot = await _second_restore_with_lists_reversed(
        hass, backup_manager, "aliases"
    )

    diff = cast(DiffResponse, await backup_manager.diff_snapshot(snapshot))

    assert diff["entity_missing"] is False
    assert not [op for op in diff["patch"] if op["path"].startswith("/aliases")], diff


def test_a_list_keeps_the_type_of_its_elements_when_compared(
    hass: HomeAssistant,
) -> None:
    """A snapshot edited by hand to hold [1, 2] does not hold the aliases "1"
    and "2" Core lists in another order: the list is compared as it is."""
    area = ar.async_get(hass).async_create("Hall", aliases={"1", "2"})
    live = orjson.loads(orjson.dumps(area.json_fragment))
    reverse = list(reversed(live["aliases"]))

    assert reordered_lists(live, {**live, "aliases": reverse}) == ["aliases"]
    assert reordered_lists(live, {**live, "aliases": [int(a) for a in reverse]}) == []


async def test_a_diff_after_a_restore_shows_the_label_core_dropped(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """The restored area is found although it lost a deleted label, and the lost
    label is among the differences the diff reports."""
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
    assert {"op": "add", "path": "/labels/-", "value": gone.label_id} in cast(
        DiffResponse, diff
    )["patch"]


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
