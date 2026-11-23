"""Fixtures and helpers the ha_set_entity and ha_remove_entity tests share."""

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_entities import EntityTools


@pytest.fixture
async def tools(hass: HomeAssistant, core_client: HomeAssistantClient) -> EntityTools:
    """The entity tools on a ``hass`` that serves Core's registry commands."""
    assert await async_setup_component(hass, "config", {})
    return EntityTools(core_client)


@pytest.fixture
def labels(label_registry: lr.LabelRegistry) -> list[str]:
    """Labels ``outdoor`` and ``smart`` in Core's label registry."""
    return [label_registry.async_create(name).label_id for name in ("Outdoor", "Smart")]


def registry_entity(
    registry: er.EntityRegistry, entity_id: str, **changes: Any
) -> er.RegistryEntry:
    """A registry entry for ``entity_id`` with ``changes`` applied."""
    domain, object_id = entity_id.split(".")
    entry = registry.async_get_or_create(
        domain, "demo", object_id, suggested_object_id=object_id
    )
    if changes:
        entry = registry.async_update_entity(entry.entity_id, **changes)
    return entry


def stored_entry(registry: er.EntityRegistry, entity_id: str) -> er.RegistryEntry:
    entry = registry.async_get(entity_id)
    assert entry is not None
    return entry
