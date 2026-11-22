"""``ha_mcp_tools/update_entities``: the loaded update entities of a real
``hass``, with the integration that owns each and its feature names."""

from __future__ import annotations

from homeassistant.components.update import UpdateEntityFeature
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import MockHAClientWebSocket

from ._component_core import ws_result
from ._tools_updates_core import SoftwareUpdate, set_up_updates

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


async def test_one_update_is_read_by_its_entity_id(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await set_up_updates(
        hass, SoftwareUpdate("update.one"), SoftwareUpdate("update.two")
    )

    result = await ws_result(component_ws, _COMMAND, entity_id="update.two")

    assert [row["entity_id"] for row in result["updates"]] == ["update.two"]


async def test_an_entity_that_is_no_loaded_update_has_no_row(
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
