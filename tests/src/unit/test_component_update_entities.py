"""``ha_mcp_tools/update_entities``: the update entities of a real ``hass``,
with the integration that owns each, its feature names and, for Core's own
Supervisor updates, their kind."""

from __future__ import annotations

from homeassistant.components.update import UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.typing import MockHAClientWebSocket

from ._component_core import ws_result
from ._hassio_updates_core import KINDS, hassio_entities
from ._hassio_updates_core import core_versions as core_versions
from ._hassio_updates_core import hassio_updates as hassio_updates
from ._tools_addons_core import Supervisor
from ._tools_addons_core import supervisor as supervisor
from ._tools_addons_core import target_app as target_app
from ._tools_updates_core import SoftwareUpdate, restore_update, set_up_updates

_COMMAND = "ha_mcp_tools/update_entities"


async def test_each_update_comes_with_its_state_integration_and_features(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """Core's state numbers the features and does not name the integration."""
    await set_up_updates(
        hass,
        SoftwareUpdate(
            "update.thing",
            features=UpdateEntityFeature.INSTALL | UpdateEntityFeature.BACKUP,
        ),
        integration="marketplace",
    )

    result = await ws_result(component_ws, _COMMAND)

    state = hass.states.get("update.thing")
    assert state is not None
    assert result == {
        "updates": [
            {
                "entity_id": "update.thing",
                "state": "on",
                "attributes": dict(state.attributes),
                "platform": "marketplace",
                "features": ["install", "backup"],
            }
        ]
    }


async def test_supervisor_updates_carry_the_kind_of_their_core_entity(
    hass: HomeAssistant,
    hassio_updates: Supervisor,
    component_ws: MockHAClientWebSocket,
) -> None:
    """An app may carry the Core update's title; only the entity class tells
    the Core update from it."""
    result = await ws_result(component_ws, _COMMAND)

    kinds = {
        row["entity_id"]: row["hassio_kind"]
        for row in result["updates"]
        if row["platform"] == "hassio"
    }
    assert kinds == {
        entity.entity_id: kind
        for entity in hassio_entities(hass)
        for cls, kind in KINDS
        if isinstance(entity, cls)
    }


async def test_a_restored_update_is_listed_with_its_integration_and_features(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
) -> None:
    """Its integration did not set it up again, so Core holds only its
    registry entry and the placeholder state it restored."""
    await set_up_updates(hass)
    entity_id = restore_update(hass, entity_registry)

    [row] = (await ws_result(component_ws, _COMMAND))["updates"]

    assert (row["entity_id"], row["state"]) == (entity_id, "unavailable")
    assert row["attributes"]["restored"] is True
    assert (row["platform"], row["features"]) == ("test", ["install"])


async def test_one_update_is_read_by_its_entity_id(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await set_up_updates(
        hass, SoftwareUpdate("update.one"), SoftwareUpdate("update.two")
    )

    result = await ws_result(component_ws, _COMMAND, entity_id="update.two")

    assert [row["entity_id"] for row in result["updates"]] == ["update.two"]


async def test_an_entity_that_is_no_update_has_no_row(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await set_up_updates(hass, SoftwareUpdate("update.one"))

    result = await ws_result(component_ws, _COMMAND, entity_id="update.missing")

    assert result == {"updates": []}


async def test_no_updates_without_core_update_integration(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    result = await ws_result(component_ws, _COMMAND)

    assert result == {"updates": []}
