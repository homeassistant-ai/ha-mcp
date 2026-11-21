"""Each handler finds, in Core, the item the id a caller passes names.

A pre-write snapshot that finds nothing is skipped silently, so every handler
has to resolve the id form a realistic caller passes (an entity id after a
rename, a dashboard's internal id, an automation's entity id). Items are set
up through Core and read back over the client by the handlers the manager
registers.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.lovelace.dashboard import LovelaceStorage
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError as CoreHomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools import websocket_api
from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantError

from ._backup_core import backup_settings as backup_settings
from ._backup_core import handler_for as _handler
from ._backup_core import snapshot_manager as snapshot_manager
from ._config_dir_yaml import setup_from_configuration, write_config_files
from ._core_setup import add_test_entry
from ._helper_tool_core import create_item, set_up


async def _renamed_input_boolean(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> str:
    """An ``input_boolean`` whose entity id no longer matches its storage id;
    the storage id."""
    await set_up(hass, "input_boolean")
    item_id = await create_item(client, "input_boolean", {})
    entity_id = entity_registry.async_get_entity_id(
        "input_boolean", "input_boolean", item_id
    )
    assert entity_id is not None
    entity_registry.async_update_entity(
        entity_id, new_entity_id="input_boolean.renamed_slug"
    )
    return item_id


async def test_a_renamed_helper_is_found_through_the_entity_registry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
) -> None:
    """The registry entry of the given entity id holds the storage id; a
    renamed entity's object id no longer equals it."""
    item_id = await _renamed_input_boolean(hass, core_client, entity_registry)

    held = await _handler(snapshot_manager, "helper_input_boolean").fetch(
        core_client, "input_boolean.renamed_slug"
    )

    assert held is not None
    assert held["id"] == item_id


async def test_a_name_that_is_another_helpers_storage_id_never_names_that_helper(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
) -> None:
    """A renamed entity can carry another helper's id as its name, so a bare id
    is a storage id and an entity id is looked up in the registry: the object
    id of the entity is never tried as a storage id."""
    await set_up(hass, "input_boolean")
    first = await create_item(core_client, "input_boolean", {})
    second = await create_item(core_client, "input_boolean", {})
    first_entity = entity_registry.async_get_entity_id(
        "input_boolean", "input_boolean", first
    )
    second_entity = entity_registry.async_get_entity_id(
        "input_boolean", "input_boolean", second
    )
    assert first_entity is not None and second_entity is not None
    # The second helper's entity takes the name that is the first one's storage id.
    entity_registry.async_update_entity(
        first_entity, new_entity_id="input_boolean.moved"
    )
    entity_registry.async_update_entity(
        second_entity, new_entity_id=f"input_boolean.{first}"
    )

    held = await _handler(snapshot_manager, "helper_input_boolean").fetch(
        core_client, f"input_boolean.{first}"
    )

    assert held is not None
    assert held["id"] == second


async def test_a_failed_registry_read_is_raised_not_read_as_no_entry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    backup_settings: config.Settings,
    entity_registry: er.EntityRegistry,
) -> None:
    """Without Core's ``config`` commands the registry read is refused, which
    must propagate (so the capture logs a warning) rather than silently skip
    the snapshot."""
    assert await async_setup_component(hass, "input_boolean", {})
    await create_item(core_client, "input_boolean", {})
    manager = bm.get_backup_manager(core_client, backup_settings)

    with pytest.raises(HomeAssistantError):
        await _handler(manager, "helper_input_boolean").fetch(
            core_client, "input_boolean.renamed_slug"
        )


@pytest.mark.parametrize("component", [False, True], ids=["core_only", "component"])
async def test_a_device_is_read_from_the_registry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    device_registry: dr.DeviceRegistry,
    component: bool,
) -> None:
    if component:
        websocket_api.async_register_commands(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=add_test_entry(hass).entry_id,
        identifiers={("test", "hub")},
        name="Hub",
    )
    device_registry.async_update_device(device.id, name_by_user="My hub")

    held = await _handler(snapshot_manager, "device").fetch(core_client, device.id)

    assert held is not None
    assert held["name_by_user"] == "My hub"


async def test_restoring_a_device_puts_its_user_fields_back(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    """A restore reverts the device to the snapshot, so a field the write
    cleared comes back and one the write set goes."""
    device = device_registry.async_get_or_create(
        config_entry_id=add_test_entry(hass).entry_id,
        identifiers={("test", "hub")},
        name="Hub",
    )
    area = area_registry.async_create("Living room")
    label = label_registry.async_create("Important")
    device_registry.async_update_device(
        device.id, name_by_user="My hub", area_id=area.id, labels={label.label_id}
    )
    handler = _handler(snapshot_manager, "device")
    held = await handler.fetch(core_client, device.id)
    device_registry.async_update_device(
        device.id, name_by_user=None, area_id=None, labels=set()
    )

    await handler.restore(core_client, device.id, held)

    restored = device_registry.async_get(device.id)
    assert restored is not None
    assert restored.name_by_user == "My hub"
    assert restored.area_id == area.id
    assert restored.labels == {label.label_id}


async def _dashboard(
    client: HomeAssistantClient, *, saved: dict[str, Any] | None
) -> None:
    """A storage dashboard at ``my-dash`` (internal id ``my_dash``)."""
    created = await client.send_websocket_message(
        {
            "type": "lovelace/dashboards/create",
            "url_path": "my-dash",
            "title": "My dashboard",
            "mode": "storage",
        }
    )
    assert created["success"], created
    if saved is not None:
        written = await client.send_websocket_message(
            {"type": "lovelace/config/save", "url_path": "my-dash", "config": saved}
        )
        assert written["success"], written


async def test_a_dashboard_is_found_by_its_internal_id(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    """The internal (underscored) id must resolve to the url_path before the
    config read, else Core says "Unknown config" and the snapshot is skipped."""
    assert await async_setup_component(hass, "lovelace", {})
    await _dashboard(core_client, saved={"views": []})

    held = await _handler(snapshot_manager, "dashboard").fetch(core_client, "my_dash")

    assert held is not None
    assert held["config"] == {"views": []}
    assert held["dashboard"]["url_path"] == "my-dash"


async def test_an_unknown_dashboard_has_nothing_to_back_up(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    """Core's "Unknown config" for an unresolved url_path (also the create path
    of a brand-new dashboard) means nothing to snapshot, not a failure."""
    assert await async_setup_component(hass, "lovelace", {})

    assert (
        await _handler(snapshot_manager, "dashboard").fetch(core_client, "x_dash")
        is None
    )


async def test_a_dashboard_without_a_saved_config_keeps_its_settings_row(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    """Core reports "no config" for a dashboard nobody saved yet; its settings
    row is still worth restoring."""
    assert await async_setup_component(hass, "lovelace", {})
    await _dashboard(core_client, saved=None)

    held = await _handler(snapshot_manager, "dashboard").fetch(core_client, "my-dash")

    assert held is not None
    assert held["config"] is None
    assert held["dashboard"]["url_path"] == "my-dash"


async def test_a_dashboard_read_failing_for_another_reason_is_raised(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    """Anything but "not found" must propagate (so the capture logs a warning)
    rather than be misread as "nothing to back up"."""
    assert await async_setup_component(hass, "lovelace", {})
    await _dashboard(core_client, saved={"views": []})

    with (
        patch.object(
            LovelaceStorage,
            "async_json",
            AsyncMock(side_effect=CoreHomeAssistantError("Internal Server Error")),
        ),
        pytest.raises((ToolError, HomeAssistantError)),
    ):
        await _handler(snapshot_manager, "dashboard").fetch(core_client, "my_dash")


_AUTOMATIONS = """\
- id: "1781613420568"
  alias: Kitchen lights
  trigger:
    - platform: event
      event_type: kitchen_pressed
  action: []
"""


async def _kitchen_automation(hass: HomeAssistant) -> None:
    await write_config_files(
        hass,
        {
            "configuration.yaml": "automation: !include automations.yaml\n",
            "automations.yaml": _AUTOMATIONS,
        },
    )
    await setup_from_configuration(hass, "automation")


@pytest.mark.parametrize("target", ["automation.kitchen_lights", "1781613420568"])
async def test_an_automation_is_snapshotted_as_stored_by_entity_id_or_storage_id(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    target: str,
) -> None:
    """The entity id form reaches the client unstripped, so it resolves to the
    storage id; a bare slug would 404 and the snapshot would be skipped. A
    rewritten snapshot restored a different config than Core held."""
    await _kitchen_automation(hass)

    held = await _handler(snapshot_manager, "automation").fetch(core_client, target)

    assert held is not None
    assert held["id"] == "1781613420568"
    assert held["alias"] == "Kitchen lights"
    assert held["trigger"] == [{"platform": "event", "event_type": "kitchen_pressed"}]


async def test_an_automation_core_does_not_hold_has_nothing_to_back_up(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    await _kitchen_automation(hass)

    assert (
        await _handler(snapshot_manager, "automation").fetch(
            core_client, "automation.gone"
        )
        is None
    )
