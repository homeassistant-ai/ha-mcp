"""A backup diff of an entity or a device, on real Core.

Core keeps the labels of an entity and of a device, and a device's identifiers
and connections, in sets, and lists a set in hash order. The snapshot is taken
from Core's registry through the server's own client. An order test then edits
the snapshot file to hold a list in the reverse of the order Core lists it in,
so it does not depend on the hash seed.
"""

from __future__ import annotations

from typing import cast

import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.backup_diff import DiffResponse
from ha_mcp.backup_manager import BackupManager
from ha_mcp.client.rest_client import HomeAssistantClient


async def _snapshot_reversing(
    backup_manager: BackupManager, domain: str, entity_id: str, *keys: str
) -> str:
    """Snapshot the item, then store each of ``keys`` in the reverse of Core's order."""
    path = await backup_manager.maybe_snapshot(domain, entity_id, force=True)
    assert path is not None
    stored = yaml.safe_load(path.read_text())
    for key in keys:
        assert len(stored["config"][key]) > 1
        stored["config"][key].reverse()
    path.write_text(yaml.safe_dump(stored))
    return path.name


def _device(hass: HomeAssistant) -> dr.DeviceEntry:
    """A device with two identifiers, two connections and two labels."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("test", "1"), ("test", "2")},
        connections={
            (dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:00:00:01"),
            (dr.CONNECTION_NETWORK_MAC, "aa:bb:cc:00:00:02"),
        },
    )
    updated = devices.async_update_device(device.id, labels={"first", "second"})
    assert updated is not None
    return updated


async def test_an_entity_diff_ignores_the_order_core_lists_labels_in(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """An entity's labels are a set in Core, so the order the snapshot holds
    them in is no difference."""
    entities = er.async_get(hass)
    entity = entities.async_get_or_create("light", "test", "1")
    entities.async_update_entity(entity.entity_id, labels={"first", "second"})
    snapshot = await _snapshot_reversing(
        backup_manager, "entity", entity.entity_id, "labels"
    )

    diff = await backup_manager.diff_snapshot(snapshot)

    assert diff["unchanged"] is True, diff


async def test_an_entity_diff_reports_aliases_in_another_order(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """Core keeps an entity's aliases in the order they were given, and a restore
    puts that order back, so another order is a difference."""
    entities = er.async_get(hass)
    entity = entities.async_get_or_create("light", "test", "1")
    entities.async_update_entity(entity.entity_id, aliases=["first", "second"])
    snapshot = await _snapshot_reversing(
        backup_manager, "entity", entity.entity_id, "aliases"
    )

    diff = await backup_manager.diff_snapshot(snapshot)

    assert diff["unchanged"] is False, diff


async def test_a_device_diff_ignores_the_order_core_lists_its_sets_in(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """A device's labels, identifiers and connections are sets in Core, so the
    order the snapshot holds them in is no difference."""
    device = _device(hass)
    snapshot = await _snapshot_reversing(
        backup_manager, "device", device.id, "labels", "identifiers", "connections"
    )

    diff = await backup_manager.diff_snapshot(snapshot)

    assert diff["unchanged"] is True, diff


async def test_a_device_diff_names_the_changed_field_and_not_modified_at(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """Core gives a device a new ``modified_at`` whenever a write changes a field
    it stores; no restore can set that back, so the diff names only the field
    that changed."""
    device = _device(hass)
    path = await backup_manager.maybe_snapshot("device", device.id, force=True)
    assert path is not None
    dr.async_get(hass).async_update_device(device.id, name_by_user="Renamed")

    diff = cast(DiffResponse, await backup_manager.diff_snapshot(path.name))

    assert [op["path"] for op in diff["patch"]] == ["/name_by_user"], diff


async def test_a_device_diff_names_a_list_the_snapshot_does_not_hold(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """A snapshot taken before Core's row had a list is compared as it is: the
    diff names the list instead of failing on the missing key."""
    device = _device(hass)
    path = await backup_manager.maybe_snapshot("device", device.id, force=True)
    assert path is not None
    stored = yaml.safe_load(path.read_text())
    del stored["config"]["labels"]
    path.write_text(yaml.safe_dump(stored))

    diff = cast(DiffResponse, await backup_manager.diff_snapshot(path.name))

    assert "/labels" in [op["path"] for op in diff["patch"]], diff


async def test_a_device_diff_answers_for_a_snapshot_whose_config_is_no_mapping(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """A snapshot file edited by hand can hold a config that is no mapping; the
    diff reports it as a difference instead of failing on it."""
    device = _device(hass)
    path = await backup_manager.maybe_snapshot("device", device.id, force=True)
    assert path is not None
    stored = yaml.safe_load(path.read_text())
    stored["config"] = "text"
    path.write_text(yaml.safe_dump(stored))

    diff = await backup_manager.diff_snapshot(path.name)

    assert diff["unchanged"] is False, diff


async def test_a_helper_diff_reports_options_in_another_order(
    hass: HomeAssistant, core_client: HomeAssistantClient, backup_manager: BackupManager
) -> None:
    """Only a registry row is compared without regard to the order of its lists:
    an input_select keeps its options in the order given, so another order is a
    difference."""
    assert await async_setup_component(hass, "input_select", {})
    created = await core_client.send_websocket_message(
        {"type": "input_select/create", "name": "Mode", "options": ["first", "second"]}
    )
    item_id = created["result"]["id"]
    path = await backup_manager.maybe_snapshot(
        "helper_input_select", item_id, force=True
    )
    assert path is not None
    await core_client.send_websocket_message(
        {
            "type": "input_select/update",
            "input_select_id": item_id,
            "name": "Mode",
            "options": ["second", "first"],
        }
    )

    diff = await backup_manager.diff_snapshot(path.name)

    assert diff["unchanged"] is False, diff
