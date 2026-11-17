"""A home on real Core that the bulk selector tests resolve against."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.bulk_selector import BulkSelectorResolution, resolve_bulk_selector
from ha_mcp.utils.data_paths import get_data_dir
from ha_mcp.visibility.persistence import save_visibility_config

from ._visibility_config import visibility_config


@pytest.fixture
async def salon(hass: HomeAssistant, area_registry: ar.AreaRegistry) -> str:
    """The Salon area's id, on a ``hass`` whose ``config`` answers the
    registry lists the selector reads."""
    assert await async_setup_component(hass, "config", {})
    return area_registry.async_create("Salon").id


def add_entity(
    hass: HomeAssistant,
    entity_id: str,
    *,
    area_id: str | None = None,
    device_id: str | None = None,
    members: list[str] | None = None,
    hidden_by_user: bool = False,
) -> None:
    """Register ``entity_id`` with a state; an aggregate lists ``members``."""
    domain, object_id = entity_id.split(".")
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        domain,
        "test",
        object_id,
        suggested_object_id=object_id,
        device_id=device_id,
        # Core lets a device's entity hold an area of its own only when it
        # has a name of its own.
        original_name=object_id,
        hidden_by=er.RegistryEntryHider.USER if hidden_by_user else None,
    )
    assert entry.entity_id == entity_id
    if area_id is not None:
        registry.async_update_entity(entity_id, area_id=area_id)
    attributes: dict[str, Any] = {} if members is None else {"entity_id": members}
    hass.states.async_set(entity_id, "on", attributes)


def hide_with_filter(*entity_ids: str, **settings: Any) -> None:
    """Turn ha-mcp's own entity visibility filter on, denying ``entity_ids``."""
    save_visibility_config(
        get_data_dir(),
        visibility_config(deny_entity_ids=list(entity_ids), **settings),
    )


async def resolve(
    client: HomeAssistantClient,
    selector: dict[str, Any],
    *,
    action: str = "off",
    parameters: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
) -> BulkSelectorResolution:
    return await resolve_bulk_selector(
        client,
        selector,
        action=action,
        parameters=parameters,
        timeout_seconds=timeout_seconds,
        validate_first=True,
    )
