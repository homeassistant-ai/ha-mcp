"""ha_set_entity reports an exposure that changed nothing.

Core answers ``homeassistant/expose_entity`` with the entity ids it actually
changed (core#188809).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_entities import EntityTools

from ._entity_registry_core import core_client as core_client
from ._entity_registry_core import lamp as lamp
from ._entity_registry_core import sent_types


@pytest.mark.parametrize(
    ("other_write", "registry_reads"),
    [
        # Nothing read the entry yet: the tool reads it to return it.
        ({}, 1),
        # The registry update's reply is the entry; no second read.
        ({"name": "Desk"}, 0),
    ],
)
async def test_an_empty_expose_map_returns_the_entry_read_once(
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    lamp: str,
    other_write: dict[str, Any],
    registry_reads: int,
) -> None:
    result = await EntityTools(core_client).ha_set_entity(
        entity_id=lamp, expose_to={}, **other_write
    )

    assert result["entity_entry"]["entity_id"] == lamp
    assert sent_types(sent).count("config/entity_registry/get") == registry_reads
    assert "homeassistant/expose_entity" not in sent_types(sent)


async def test_bulk_exposure_counts_what_either_write_changed(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    lamp: str,
) -> None:
    """Exposing changes the lamp, hiding changes the desk, the porch is
    already as asked: only the porch is reported as already set."""
    registry = er.async_get(hass)
    desk = registry.async_get_or_create("light", "demo", "desk").entity_id
    porch = registry.async_get_or_create("light", "demo", "porch").entity_id
    async_expose_entity(hass, "conversation", lamp, False)
    async_expose_entity(hass, "conversation", desk, True)
    async_expose_entity(hass, "cloud.alexa", desk, True)
    async_expose_entity(hass, "conversation", porch, True)

    result = await EntityTools(core_client).ha_set_entity(
        entity_id=[lamp, desk, porch],
        expose_to={"conversation": True, "cloud.alexa": False},
    )

    updates = {r["entity_id"]: " ".join(r["updates"]) for r in result["succeeded"]}
    assert "already set" not in updates[lamp]
    assert "already set" not in updates[desk]
    assert "already set" in updates[porch]
