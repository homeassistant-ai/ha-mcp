"""ha_mcp_tools entity_entry / entity_restore: an entity backup written in process.

The commands run on a real ``hass`` through Core's WebSocket API: the entity,
device and config entry are Core's registry rows, a removed entry is one Core
removed, and every refusal is Core's.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.homeassistant.exposed_entities import (
    async_listen_entity_updates,
)
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.json import json_dumps
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, async_capture_events
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools.entity_restore import (
    WS_ENTITY_ENTRY,
    WS_ENTITY_RESTORE,
)
from ha_mcp.backup_entities import restore_entity
from ha_mcp.client.rest_client import HomeAssistantClient

from ._component_core import ws_call, ws_result

SETTINGS = {
    "name": None,
    "icon": "mdi:lamp",
    "area_id": "office",
    "device_class": "outlet",
    "aliases": [None, "lamp"],
    "labels": ["work"],
    "categories": {"automation": "cat-a"},
    "hidden_by": "integration",
}


@pytest.fixture
async def desk(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
) -> er.RegistryEntry:
    """light.desk on a device of a config entry, with settings of its own.

    The integration names it, so an area of its own needs no name of its own.
    """
    config_entry = MockConfigEntry(domain="hue")
    config_entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("hue", "desk")}
    )
    entry = entity_registry.async_get_or_create(
        "light",
        "hue",
        "desk",
        suggested_object_id="desk",
        config_entry=config_entry,
        device_id=device.id,
        original_name="Desk lamp",
    )
    area_registry.async_create("Office")
    kitchen = area_registry.async_create("Kitchen")
    entity_registry.async_update_entity_options(
        entry.entity_id, "conversation", {"should_expose": True}
    )
    return entity_registry.async_update_entity(
        entry.entity_id,
        name="Desk",
        area_id=kitchen.id,
        labels={"x"},
        categories={"automation": "cat-b", "script": "cat-s"},
        hidden_by=er.RegistryEntryHider.USER,
    )


async def _restore(
    ws: MockHAClientWebSocket, desk: er.RegistryEntry, **msg: Any
) -> dict[str, Any]:
    result: dict[str, Any] = await ws_result(
        ws,
        WS_ENTITY_RESTORE,
        **{
            "entry_id": desk.id,
            "entity_id": desk.entity_id,
            "settings": SETTINGS,
            **msg,
        },
    )
    return result


async def test_every_saved_field_goes_in_one_registry_update(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    desk: er.RegistryEntry,
) -> None:
    """One update carries the rename and every field, so a refused value
    leaves the whole entry as it was."""
    updates = async_capture_events(hass, er.EVENT_ENTITY_REGISTRY_UPDATED)

    result = await _restore(component_ws, desk, new_entity_id="light.lamp")

    entry = entity_registry.async_get("light.lamp")
    assert entry is not None
    assert entry.id == desk.id
    assert result == {
        "success": True,
        "entity_entry": json.loads(json_dumps(entry.extended_dict)),
        "warnings": [],
    }
    assert len(updates) == 1
    assert entry.categories == {"automation": "cat-a"}
    assert entry.hidden_by is er.RegistryEntryHider.INTEGRATION
    assert entry.aliases == [er.COMPUTED_NAME, "lamp"]
    assert (entry.name, entry.icon, entry.area_id, entry.device_class) == (
        None,
        "mdi:lamp",
        "office",
        "outlet",
    )
    assert entry.labels == {"work"}


async def test_a_hidden_entity_is_shown_again(
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    desk: er.RegistryEntry,
) -> None:
    result = await _restore(
        component_ws, desk, settings={**SETTINGS, "hidden_by": None}
    )

    assert result["success"] is True
    entry = entity_registry.async_get(desk.id)
    assert entry is not None
    assert entry.hidden_by is None


async def test_an_entity_id_in_entry_id_finds_no_entry(
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    desk: er.RegistryEntry,
) -> None:
    """Core's registry also finds an entry by entity_id; the backup's entry id
    must not pick whatever entity holds that entity_id now."""
    read = await ws_result(component_ws, WS_ENTITY_ENTRY, entry_id=desk.entity_id)
    write = await _restore(component_ws, desk, entry_id=desk.entity_id)

    assert read == {"entry": None, "deleted": None}
    assert write["error"]["code"] == "entry_deleted"
    assert entity_registry.async_get(desk.id) == desk


async def test_a_renamed_entry_is_not_written(
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    desk: er.RegistryEntry,
) -> None:
    result = await _restore(component_ws, desk, entity_id="light.other")

    assert result["error"]["code"] == "entity_identity_mismatch"
    assert entity_registry.async_get(desk.id) == desk


async def test_cores_refusal_writes_nothing(
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    desk: er.RegistryEntry,
) -> None:
    entity_registry.async_get_or_create(
        "light", "hue", "lamp", suggested_object_id="lamp"
    )

    result = await _restore(component_ws, desk, new_entity_id="light.lamp")

    assert result == {
        "success": False,
        "error": {
            "code": "invalid",
            "message": "Entity with this ID is already registered",
        },
    }
    assert entity_registry.async_get(desk.id) == desk


async def test_an_unknown_hider_value_is_invalid(
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    desk: er.RegistryEntry,
) -> None:
    result = await _restore(
        component_ws, desk, settings={**SETTINGS, "hidden_by": "nobody"}
    )

    assert result["error"]["code"] == "invalid"
    assert entity_registry.async_get(desk.id) == desk


@pytest.mark.parametrize(
    ("saved", "disabled_parent", "written"),
    [
        ("user", None, er.RegistryEntryDisabler.USER),
        ("hass", None, er.RegistryEntryDisabler.HASS),
        ("device", "device", er.RegistryEntryDisabler.DEVICE),
        ("config_entry", "config_entry", er.RegistryEntryDisabler.CONFIG_ENTRY),
        ("device", None, None),
        ("config_entry", None, None),
    ],
)
async def test_a_disabler_core_sets_from_a_parent_needs_that_parent_disabled(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    desk: er.RegistryEntry,
    saved: str,
    disabled_parent: str | None,
    written: er.RegistryEntryDisabler | None,
) -> None:
    """A device or config_entry value is written only while that parent is
    disabled: Core resets it on the parent's next update otherwise."""
    assert desk.device_id is not None
    assert desk.config_entry_id is not None
    if disabled_parent == "device":
        device_registry.async_update_device(
            desk.device_id, disabled_by=dr.DeviceEntryDisabler.USER
        )
    elif disabled_parent == "config_entry":
        await hass.config_entries.async_set_disabled_by(
            desk.config_entry_id, ConfigEntryDisabler.USER
        )
    await hass.async_block_till_done()
    if disabled_parent is not None:
        # Core disabled the entity for its parent; the user has disabled it since.
        entity_registry.async_update_entity(
            desk.entity_id, disabled_by=er.RegistryEntryDisabler.USER
        )

    result = await _restore(
        component_ws, desk, settings={**SETTINGS, "disabled_by": saved}
    )

    entry = entity_registry.async_get(desk.id)
    assert entry is not None
    assert entry.disabled_by is written
    if written is None:
        [warning] = result["warnings"]
        assert f"the backup holds {saved!r}" in warning
    else:
        assert result["warnings"] == []


async def test_the_disabled_device_must_be_the_entitys_own(
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    desk: er.RegistryEntry,
) -> None:
    assert desk.config_entry_id is not None
    other = device_registry.async_get_or_create(
        config_entry_id=desk.config_entry_id, identifiers={("hue", "other")}
    )
    device_registry.async_update_device(
        other.id, disabled_by=dr.DeviceEntryDisabler.USER
    )

    result = await _restore(
        component_ws, desk, settings={**SETTINGS, "disabled_by": "device"}
    )

    entry = entity_registry.async_get(desk.id)
    assert entry is not None
    assert entry.disabled_by is None
    [warning] = result["warnings"]
    assert "the device is not disabled now" in warning


async def test_the_assistant_hears_of_an_exposure_change(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    desk: er.RegistryEntry,
) -> None:
    """Core tells the assistant only when its expose call is the one that
    changes the value, so the exposure goes ahead of the options write."""
    assert await async_setup_component(hass, "homeassistant", {})
    told: list[None] = []
    async_listen_entity_updates(hass, "conversation", lambda: told.append(None))

    await _restore(
        component_ws,
        desk,
        options={
            "conversation": {"should_expose": False},
            "light": None,
            "sensor": {"display_precision": 2},
        },
    )

    entry = entity_registry.async_get(desk.id)
    assert entry is not None
    assert entry.options == {
        "conversation": {"should_expose": False},
        "sensor": {"display_precision": 2},
    }
    assert len(told) == 1


async def test_a_removed_entry_is_kept_while_its_config_entry_exists(
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    desk: er.RegistryEntry,
) -> None:
    """Another removed entry is passed over: only this entry's id counts."""
    assert desk.config_entry_id is not None
    other = entity_registry.async_get_or_create("light", "hue", "other")
    entity_registry.async_remove(other.entity_id)
    entity_registry.async_remove(desk.entity_id)
    kept = {
        "entity_id": "light.desk",
        "config_entry_id": desk.config_entry_id,
        "kept_until": None,
    }

    read = await ws_result(component_ws, WS_ENTITY_ENTRY, entry_id=desk.id)
    write = await _restore(component_ws, desk)

    assert read == {"entry": None, "deleted": kept}
    assert write["error"]["code"] == "entry_deleted"
    assert write["error"]["deleted"] == kept


async def test_an_orphaned_entry_is_kept_until_the_time_it_reports(
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    desk: er.RegistryEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """``kept_until`` is the last moment Core still keeps the entry; once Core
    purges it, nothing is kept, and another removed entry is no answer."""
    freezer.move_to(datetime(2026, 11, 13, 10, 0, tzinfo=UTC))
    lamp = entity_registry.async_get_or_create(
        "light", "demo", "lamp", suggested_object_id="lamp"
    )
    entity_registry.async_remove(lamp.entity_id)
    entity_registry.async_remove(desk.entity_id)

    found = await ws_result(component_ws, WS_ENTITY_ENTRY, entry_id=lamp.id)
    deleted = found["deleted"]
    assert found["entry"] is None
    assert (deleted["entity_id"], deleted["config_entry_id"]) == ("light.lamp", None)
    kept_until = datetime.fromisoformat(deleted["kept_until"])

    freezer.move_to(kept_until)
    entity_registry.async_purge_expired_orphaned_entities()
    still = await ws_result(component_ws, WS_ENTITY_ENTRY, entry_id=lamp.id)
    freezer.move_to(kept_until + timedelta(seconds=1))
    entity_registry.async_purge_expired_orphaned_entities()
    gone = await ws_result(component_ws, WS_ENTITY_ENTRY, entry_id=lamp.id)
    write = await _restore(
        component_ws, lamp, entry_id=lamp.id, entity_id=lamp.entity_id
    )

    assert still == found
    assert gone == {"entry": None, "deleted": None}
    assert write["error"]["code"] == "entry_deleted"
    assert write["error"]["deleted"] is None


async def test_a_live_entry_reads_its_current_entity_id(
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    desk: er.RegistryEntry,
) -> None:
    entity_registry.async_update_entity(desk.entity_id, new_entity_id="light.lamp")

    found = await ws_result(component_ws, WS_ENTITY_ENTRY, entry_id=desk.id)

    assert found == {"entry": {"id": desk.id, "entity_id": "light.lamp"}}


@pytest.mark.parametrize(
    ("command", "restore"), [(WS_ENTITY_ENTRY, False), (WS_ENTITY_RESTORE, True)]
)
async def test_commands_are_admin_gated(
    component_ws_read_only: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    desk: er.RegistryEntry,
    command: str,
    restore: bool,
) -> None:
    extra = {"entity_id": desk.entity_id, "settings": SETTINGS} if restore else {}

    reply = await ws_call(component_ws_read_only, command, entry_id=desk.id, **extra)

    assert reply["success"] is False
    assert reply["error"]["code"] == "unauthorized"
    assert entity_registry.async_get(desk.id) == desk


async def test_the_servers_restore_is_written_by_the_component(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    label_registry: lr.LabelRegistry,
    category_registry: cr.CategoryRegistry,
    desk: er.RegistryEntry,
) -> None:
    """The server's restore message passes the component's schema, a removed
    options domain and a renamed entity included, and the component writes
    values Core's own update command refuses (``integration``)."""
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "homeassistant", {})
    work = label_registry.async_create("Work")
    lights = category_registry.async_create(scope="automation", name="Lights")
    snapshot = {
        "id": desk.id,
        "entity_id": "light.desk",
        "name": None,
        "icon": None,
        "area_id": "office",
        "device_class": None,
        "aliases": [None, "desk lamp"],
        "labels": [work.label_id],
        "categories": {"automation": lights.category_id},
        "hidden_by": "integration",
        "disabled_by": "user",
        "options": {"conversation": {"should_expose": False}},
    }
    entity_registry.async_update_entity_options(desk.entity_id, "light", {"x": 1})
    entity_registry.async_update_entity(desk.entity_id, new_entity_id="light.lamp")

    await restore_entity(component_core_client, "light.desk", snapshot)

    entry = entity_registry.async_get(desk.id)
    assert entry is not None
    assert entry.entity_id == "light.desk"
    assert entry.hidden_by is er.RegistryEntryHider.INTEGRATION
    assert entry.disabled_by is er.RegistryEntryDisabler.USER
    assert entry.options == {"conversation": {"should_expose": False}}
    assert entry.labels == {work.label_id}
