"""Core's loaded update entities, for ``ha_mcp_tools/update_entities``.

An update entity's state carries ``supported_features`` as a number whose bits
only Core's ``UpdateEntityFeature`` names, and no state or REST answer says
which integration owns the entity. The update component holds both, so this
bridge returns each loaded entity's state with its integration and the names
of its features, read from Core's enum.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.components.update import UpdateEntity
    from homeassistant.core import HomeAssistant

WS_UPDATE_ENTITIES = "ha_mcp_tools/update_entities"
# Advertised by websocket_api's info; without it ha_manage_updates reads the
# states and the entity registry and cannot say which updates can be installed.
CAPABILITY = "update_entities"


def _row(hass: HomeAssistant, entity: UpdateEntity) -> dict[str, Any] | None:
    from homeassistant.components.update import UpdateEntityFeature

    state = hass.states.get(entity.entity_id)
    if state is None:
        return None
    supported = UpdateEntityFeature(entity.supported_features)
    return {
        "entity_id": entity.entity_id,
        "state": state.state,
        "attributes": dict(state.attributes),
        "platform": entity.platform.platform_name,
        "features": [
            feature.name.lower()
            for feature in UpdateEntityFeature
            if feature.name and feature in supported
        ],
    }


def update_entities(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """Every loaded update entity, or the one ``entity_id`` names (none when
    it is not a loaded update entity)."""
    from homeassistant.components.update import DATA_COMPONENT

    component = hass.data.get(DATA_COMPONENT)
    entities = list(component.entities) if component is not None else []
    if (entity_id := msg.get("entity_id")) is not None:
        entities = [e for e in entities if e.entity_id == entity_id]
    return {
        "updates": [
            row for entity in entities if (row := _row(hass, entity)) is not None
        ]
    }


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) row websocket_api registers."""
    return [
        (
            {
                vol.Required("type"): WS_UPDATE_ENTITIES,
                vol.Optional("entity_id"): str,
            },
            update_entities,
            None,
        )
    ]
