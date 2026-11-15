"""A backup diff of an entity or a device, on real Core.

Core keeps the labels of an entity and of a device, and a device's identifiers
and connections, in sets, and lists a set in hash order. Which lists those are
is the component's answer, read from Core's registry entry: with the component
their order is no difference, and without it the diff reports the order and
says so. The snapshot is taken from Core's registry through the server's own
client. An order test then edits the snapshot file to hold a list in the
reverse of the order Core lists it in, so it does not depend on the hash seed.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import orjson
import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from starlette.requests import Request

from ha_mcp.backup_diff import DiffResponse
from ha_mcp.backup_manager import BackupManager
from ha_mcp.backup_registry_items import compared_row
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.settings_ui import build_settings_handlers

from ._backup_restore_helpers import backup_tool, use_backup_manager


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
    hass: HomeAssistant,
    backup_manager: BackupManager,
    component_core_client: HomeAssistantClient,
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
    hass: HomeAssistant,
    backup_manager: BackupManager,
    component_core_client: HomeAssistantClient,
) -> None:
    """Core keeps an entity's aliases in the order they were given, and a restore
    puts that order back, so another order is a difference. The component says
    they are no set, so the diff has nothing to add."""
    entities = er.async_get(hass)
    entity = entities.async_get_or_create("light", "test", "1")
    entities.async_update_entity(entity.entity_id, aliases=["first", "second"])
    snapshot = await _snapshot_reversing(
        backup_manager, "entity", entity.entity_id, "aliases"
    )

    diff = await backup_manager.diff_snapshot(snapshot)

    assert diff["unchanged"] is False, diff
    assert "warnings" not in diff


async def test_a_device_diff_ignores_the_order_core_lists_its_sets_in(
    hass: HomeAssistant,
    backup_manager: BackupManager,
    component_core_client: HomeAssistantClient,
) -> None:
    """A device's labels, identifiers and connections are sets in Core, so the
    order the snapshot holds them in is no difference."""
    device = _device(hass)
    snapshot = await _snapshot_reversing(
        backup_manager, "device", device.id, "labels", "identifiers", "connections"
    )

    diff = await backup_manager.diff_snapshot(snapshot)

    assert diff["unchanged"] is True, diff


async def test_a_diff_without_the_component_reports_the_order_and_says_so(
    hass: HomeAssistant, backup_manager: BackupManager
) -> None:
    """Only the component can say which lists Core keeps in a set. Without it
    the diff does not guess: the order is a difference, with a warning that
    names the lists and the component."""
    device = _device(hass)
    snapshot = await _snapshot_reversing(
        backup_manager, "device", device.id, "labels", "identifiers", "connections"
    )

    diff = cast(DiffResponse, await backup_manager.diff_snapshot(snapshot))

    assert {op["path"].split("/")[1] for op in diff["patch"]} == {
        "labels",
        "identifiers",
        "connections",
    }, diff
    (warning,) = diff["warnings"]
    assert "connections, identifiers, labels" in warning
    assert "ha_mcp_tools component" in warning


async def test_a_question_the_component_does_not_answer_fails_the_comparison(
    hass: HomeAssistant,
    backup_manager: BackupManager,
    component_core_client: HomeAssistantClient,
) -> None:
    """A question that was sent and got no answer is not a missing component:
    the comparison fails rather than report an order the component may rule
    out. The first comparison is answered, so the component is known by then."""
    device = _device(hass)
    snapshot = await _snapshot_reversing(backup_manager, "device", device.id, "labels")
    data, current = await backup_manager.snapshot_comparison(snapshot)
    reordered = {**current, "labels": current["labels"][::-1]}
    component_core_client.send_websocket_message = AsyncMock(
        side_effect=HomeAssistantConnectionError("no answer")
    )

    with pytest.raises(HomeAssistantConnectionError, match="no answer"):
        await compared_row(
            component_core_client, "device", device.id, data["config"], reordered
        )


async def test_the_backup_tool_puts_the_diff_warning_beside_the_diff(
    hass: HomeAssistant,
    backup_manager: BackupManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool's warnings are a top-level list, never part of its data."""
    device = _device(hass)
    snapshot = await _snapshot_reversing(backup_manager, "device", device.id, "labels")
    tool = backup_tool(monkeypatch, backup_manager)

    result = await tool(scope="edits", action="diff", backup_name=snapshot)

    (warning,) = result["warnings"]
    assert "labels" in warning
    assert "warnings" not in result["data"]


async def test_the_settings_diff_carries_the_diff_warning(
    hass: HomeAssistant,
    backup_manager: BackupManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The settings page shows the same comparison, so it gets the warning too."""
    device = _device(hass)
    snapshot = await _snapshot_reversing(backup_manager, "device", device.id, "labels")
    use_backup_manager(monkeypatch, backup_manager)
    handler = build_settings_handlers(None)["diff_backup"]

    response = await handler(
        Request({"type": "http", "path_params": {"name": snapshot}})
    )

    (warning,) = orjson.loads(response.body)["warnings"]
    assert "labels" in warning


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
    """Only a list Core keeps in a set is compared without regard to its order:
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


async def test_a_warning_stored_in_the_snapshot_file_is_not_the_diffs(
    hass: HomeAssistant, core_client: HomeAssistantClient, backup_manager: BackupManager
) -> None:
    """The diff's warnings are what the comparison found, never a key the
    snapshot file carries."""
    assert await async_setup_component(hass, "input_select", {})
    created = await core_client.send_websocket_message(
        {"type": "input_select/create", "name": "Mode", "options": ["first", "second"]}
    )
    path = await backup_manager.maybe_snapshot(
        "helper_input_select", created["result"]["id"], force=True
    )
    assert path is not None
    stored = yaml.safe_load(path.read_text())
    path.write_text(yaml.safe_dump({**stored, "warnings": ["from the file"]}))

    diff = await backup_manager.diff_snapshot(path.name)

    assert "warnings" not in diff
