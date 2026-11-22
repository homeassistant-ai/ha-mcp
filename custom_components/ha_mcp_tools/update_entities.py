"""Core's update entities, for ``ha_mcp_tools/update_entities``.

An update entity's state carries ``supported_features`` as a number whose bits
only Core's ``UpdateEntityFeature`` names, and no state or REST answer says
which integration owns the entity or, for the Supervisor's updates, which kind
it is. This bridge returns every update state, restored ones included, with its
integration, the names of its features read from Core's enum and, for a loaded
hassio entity, the kind Core's own hassio entity class gives it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.components.update import UpdateEntity
    from homeassistant.core import HomeAssistant, State

WS_UPDATE_ENTITIES = "ha_mcp_tools/update_entities"
# Advertised by websocket_api's info; without it ha_manage_updates reads the
# states and the entity registry and cannot say which updates can be installed.
CAPABILITY = "update_entities"


def _hassio_kind(entity: UpdateEntity) -> str | None:
    from homeassistant.components.hassio import entity as hassio

    kinds = (
        (hassio.HassioCoreEntity, "core"),
        (hassio.HassioOSEntity, "os"),
        (hassio.HassioSupervisorEntity, "supervisor"),
        (hassio.HassioAddonEntity, "addons"),
    )
    return next((kind for cls, kind in kinds if isinstance(entity, cls)), None)


def _row(
    hass: HomeAssistant, state: State, entity: UpdateEntity | None
) -> dict[str, Any]:
    from homeassistant.components.update import UpdateEntityFeature
    from homeassistant.const import ATTR_SUPPORTED_FEATURES
    from homeassistant.helpers import entity_registry as er

    if entity is not None:
        platform: str | None = entity.platform.platform_name
        bits = entity.supported_features
    else:
        entry = er.async_get(hass).async_get(state.entity_id)
        platform = entry.platform if entry is not None else None
        bits = state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
    supported = UpdateEntityFeature(bits)
    row: dict[str, Any] = {
        "entity_id": state.entity_id,
        "state": state.state,
        "attributes": dict(state.attributes),
        "platform": platform,
        "features": [
            feature.name.lower()
            for feature in UpdateEntityFeature
            if feature.name and feature in supported
        ],
    }
    if entity is not None and platform == "hassio":
        row["hassio_kind"] = _hassio_kind(entity)
    return row


def update_entities(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """Every update state, or the one ``entity_id`` names (none when it is not
    an update entity's)."""
    from homeassistant.components.update import DATA_COMPONENT, DOMAIN

    component = hass.data.get(DATA_COMPONENT)
    states = hass.states.async_all(DOMAIN)
    if (entity_id := msg.get("entity_id")) is not None:
        states = [s for s in states if s.entity_id == entity_id]
    return {
        "updates": [
            _row(
                hass,
                state,
                component.get_entity(state.entity_id) if component else None,
            )
            for state in states
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
