"""Registry icons read back from Core, for the zone backup and helper icon tests."""

from __future__ import annotations

from homeassistant.helpers import entity_registry as er


def icon_of(registry: er.EntityRegistry, entity_id: str) -> str | None:
    """The icon override in ``entity_id``'s registry entry."""
    entry = registry.async_get(entity_id)
    assert entry is not None, entity_id
    return entry.icon
