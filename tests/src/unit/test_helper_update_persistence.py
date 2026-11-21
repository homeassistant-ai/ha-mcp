"""ha_config_set_helper writes what it is given where Home Assistant keeps it.

A storage helper's update goes into its stored item, not only the entity
registry, so it survives a restart (#880). A flow helper runs its config or
options flow (#967), and area, labels and icon then reach every entity of its
config entry. Every reply is Core's: the tool runs on the server's own client
connected to a real ``hass``, and each check reads back what Core stored.
"""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_helpers import HelperConfigTools

from ._command_faults import HELD_WAIT_S, dropped_on, pooled_socket
from ._held_command import held_command
from ._helper_tool_core import create_item, fresh_name, set_up, stored_item
from ._helper_tool_core import tools as tools
from ._receipt_faults import on_receipt
from ._tool_errors import error_of

REGISTRY_UPDATE = "config/entity_registry/update"
TARIFF_METER = {
    "source": "sensor.energy",
    "cycle": "none",
    "tariffs": ["peak", "offpeak"],
}


def _entity_id(
    entity_registry: er.EntityRegistry, helper_type: str, item_id: str
) -> str:
    entity_id = entity_registry.async_get_entity_id(helper_type, helper_type, item_id)
    assert entity_id is not None
    return entity_id


def _entry_entities(
    entity_registry: er.EntityRegistry, entry_id: str
) -> list[er.RegistryEntry]:
    return er.async_entries_for_config_entry(entity_registry, entry_id)


async def _min_max_entry(hass: HomeAssistant) -> ConfigEntry:
    """A min_max helper made by Core's own config flow."""
    await set_up(hass, "min_max")
    flow = await hass.config_entries.flow.async_init(
        "min_max", context={"source": "user"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"],
        {"name": "Average", "entity_ids": ["sensor.a", "sensor.b"], "type": "mean"},
    )
    await hass.async_block_till_done()
    entry: ConfigEntry = result["result"]
    return entry


async def test_an_input_select_update_stores_its_new_options(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    tools: HelperConfigTools,
) -> None:
    """#880: changed options that reached only the entity registry were gone
    after a restart."""
    await set_up(hass, "input_select")
    item_id = await create_item(core_client, "input_select", {"options": ["a", "b"]})

    await tools.ha_config_set_helper(
        helper_type="input_select",
        helper_id=_entity_id(entity_registry, "input_select", item_id),
        config={"options": ["A", "B", "C"], "initial": "B"},
    )

    stored = await stored_item(core_client, "input_select", item_id)
    assert stored["options"] == ["A", "B", "C"]
    assert stored["initial"] == "B"


async def test_without_the_component_an_updated_icon_goes_to_the_entity_registry(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """With no Core schema to read, the icon is not guessed into the item."""
    await set_up(hass, "input_button")
    item_id = await create_item(core_client, "input_button", {})
    entity_id = _entity_id(entity_registry, "input_button", item_id)
    new_name = fresh_name()

    await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="input_button",
        helper_id=entity_id,
        name=new_name,
        icon="mdi:gesture-tap",
    )

    stored = await stored_item(core_client, "input_button", item_id)
    assert stored["name"] == new_name
    assert "icon" not in stored
    entry = entity_registry.async_get(entity_id)
    assert entry is not None
    assert entry.icon == "mdi:gesture-tap"


async def test_an_update_without_a_name_clears_the_area(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    tools: HelperConfigTools,
) -> None:
    """#1012: an update that only clears the area needs no name, and keeps the
    stored one."""
    await set_up(hass, "input_boolean")
    item_id = await create_item(core_client, "input_boolean", {})
    entity_id = _entity_id(entity_registry, "input_boolean", item_id)
    entity_registry.async_update_entity(
        entity_id, area_id=area_registry.async_create("Kitchen").id
    )
    before = await stored_item(core_client, "input_boolean", item_id)

    await tools.ha_config_set_helper(
        helper_type="input_boolean", helper_id=entity_id, area_id=""
    )

    entry = entity_registry.async_get(entity_id)
    assert entry is not None
    assert entry.area_id is None
    assert await stored_item(core_client, "input_boolean", item_id) == before


async def test_an_empty_config_creates_the_helper(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> None:
    """#2479: ``config={}`` means no fields, not an invalid config."""
    await set_up(hass, "input_boolean")
    name = fresh_name()

    result = await tools.ha_config_set_helper(
        helper_type="input_boolean", name=name, config={}
    )

    stored = await stored_item(core_client, "input_boolean", result["data"]["id"])
    assert stored["name"] == name


async def test_area_labels_and_icon_reach_every_entity_of_a_flow_helper(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    label_registry: lr.LabelRegistry,
    entity_registry: er.EntityRegistry,
    tools: HelperConfigTools,
) -> None:
    """#967: a utility_meter with two tariffs has a select and two sensors; a
    setting applied to one of them only leaves the others unplaced."""
    await set_up(hass, "utility_meter")
    area_id = area_registry.async_create("Kitchen").id
    label_id = label_registry.async_create("Metered").label_id

    result = await tools.ha_config_set_helper(
        helper_type="utility_meter",
        name="Daily energy",
        config=dict(TARIFF_METER),
        area_id=area_id,
        labels=[label_id],
        icon="mdi:flash",
    )

    entities = _entry_entities(entity_registry, result["entry_id"])
    assert sorted(result["entity_ids"]) == sorted(e.entity_id for e in entities)
    assert len(entities) == 3
    for entity in entities:
        assert entity.area_id == area_id
        assert entity.labels == {label_id}
        assert entity.icon == "mdi:flash"
    assert result["area_id"] == area_id
    assert result["labels"] == [label_id]
    assert result["icon"] == "mdi:flash"


async def test_a_flow_helper_update_runs_its_options_flow_and_applies_the_icon(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    entry = await _min_max_entry(hass)
    [entity] = _entry_entities(entity_registry, entry.entry_id)

    result = await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="min_max",
        helper_id=entry.entry_id,
        config={"type": "max"},
        icon="mdi:flash",
    )

    assert result["updated"] is True
    assert entry.options["type"] == "max"
    updated = entity_registry.async_get(entity.entity_id)
    assert updated is not None
    assert updated.icon == "mdi:flash"
    assert result["icon"] == "mdi:flash"


async def test_empty_values_clear_a_flow_helpers_area_labels_and_icon(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    label_registry: lr.LabelRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """#1012: '' clears the area and icon and [] the labels, on an update that
    passes no name. ``config={}`` keeps every option and finishes the flow."""
    entry = await _min_max_entry(hass)
    [entity] = _entry_entities(entity_registry, entry.entry_id)
    entity_registry.async_update_entity(
        entity.entity_id,
        area_id=area_registry.async_create("Kitchen").id,
        labels={label_registry.async_create("Metered").label_id},
        icon="mdi:flash",
    )

    result = await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="min_max",
        helper_id=entry.entry_id,
        config={},
        area_id="",
        labels=[],
        icon="",
    )

    cleared = entity_registry.async_get(entity.entity_id)
    assert cleared is not None
    assert (cleared.area_id, cleared.labels, cleared.icon) == (None, set(), None)
    assert (result["area_id"], result["labels"], result["icon"]) == (None, [], None)


async def test_a_refused_registry_update_is_a_warning_naming_its_fields(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The helper exists, so the call succeeds; what did not land is not
    echoed as applied."""
    await set_up(hass, "min_max")
    area_id = area_registry.async_create("Hallway").id

    with on_receipt(
        hass,
        REGISTRY_UPDATE,
        lambda msg: entity_registry.async_remove(msg["entity_id"]),
    ):
        result = await HelperConfigTools(core_client).ha_config_set_helper(
            helper_type="min_max",
            name="Average",
            config={"entity_ids": ["sensor.a"], "type": "mean"},
            area_id=area_id,
            icon="mdi:flash",
        )

    [entity_id] = result["entity_ids"]
    assert any(
        entity_id in w and "area_id" in w and "icon" in w and "Entity not found" in w
        for w in result["warnings"]
    )
    assert "area_id" not in result
    assert "icon" not in result


async def test_an_unanswered_entity_list_is_a_warning_not_a_helper_without_entities(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
) -> None:
    """The entry was created; the caller must learn that its area was not
    applied, rather than read ``entity_ids: []`` as the whole story."""
    await set_up(hass, "min_max")
    area_id = area_registry.async_create("Hallway").id
    socket = await pooled_socket(core_client)

    with dropped_on(hass, "config/entity_registry/list", socket):
        result = await HelperConfigTools(core_client).ha_config_set_helper(
            helper_type="min_max",
            name="Average",
            config={"entity_ids": ["sensor.a"], "type": "mean"},
            area_id=area_id,
            wait=False,
        )

    [entry] = hass.config_entries.async_entries("min_max")
    assert result["entity_ids"] == []
    assert any(
        "entity_registry/list" in w and entry.entry_id in w for w in result["warnings"]
    )


async def test_an_unanswered_registry_update_does_not_stop_the_others(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One entity's update gets no answer: it is named in a warning, and the
    other two entities still get the area."""
    await set_up(hass, "utility_meter")
    area_id = area_registry.async_create("Hallway").id
    monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", HELD_WAIT_S)

    async with held_command(hass, REGISTRY_UPDATE, first_only=True):
        result = await HelperConfigTools(core_client).ha_config_set_helper(
            helper_type="utility_meter",
            name="Daily energy",
            config=dict(TARIFF_METER),
            area_id=area_id,
        )

        [raised] = [w for w in result["warnings"] if "raised" in w]
        others = [e for e in result["entity_ids"] if e not in raised]
        assert len(others) == 2
        for entity_id in others:
            entity = entity_registry.async_get(entity_id)
            assert entity is not None
            assert entity.area_id == area_id
            assert not any(entity_id in w for w in result["warnings"])


async def test_a_blank_name_does_not_name_a_new_flow_helper(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A form asking for a name is not answered with an empty one."""
    await set_up(hass, "min_max")

    with pytest.raises(ToolError) as exc_info:
        await HelperConfigTools(core_client).ha_config_set_helper(
            helper_type="min_max",
            name="",
            config={"entity_ids": ["sensor.x"], "type": "mean"},
        )

    assert "name: required key not provided" in error_of(exc_info)["message"]
    assert hass.config_entries.async_entries("min_max") == []


async def test_an_empty_string_config_opens_the_flow_form(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Agents send "" for an unset config: it answers no form, as the
    storage helpers treat it, instead of failing as invalid JSON."""
    await set_up(hass, "min_max")

    shown = await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="min_max", name="Average", config=""
    )

    assert shown["flow_type"] == "form"
    assert hass.config_entries.async_entries("min_max") == []
