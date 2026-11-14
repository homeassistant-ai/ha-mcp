"""ha_get_entity's bulk read of ids without a registry entry, on a real ``hass``."""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import entity_exposure
from ha_mcp.tools.tools_entities import EntityTools


async def test_a_long_bulk_of_ids_without_registry_entry_bounds_its_state_reads(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
) -> None:
    # ``config`` serves the registry reads, ``homeassistant`` the exposure list.
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "homeassistant", {})
    ids = [f"light.loose_{n}" for n in range(3 * entity_exposure.STATE_READS_AT_ONCE)]
    for entity_id in ids:
        hass.states.async_set(entity_id, "on")
    read_state = entity_exposure.get_entity_state_or_none
    in_flight = peak = 0

    async def counted(client: Any, entity_id: str) -> Any:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            return await read_state(client, entity_id)
        finally:
            in_flight -= 1

    monkeypatch.setattr(entity_exposure, "get_entity_state_or_none", counted)

    result = await EntityTools(core_client).ha_get_entity(entity_id=ids)

    assert [e["entity_id"] for e in result["unregistered_entities"]] == ids
    assert peak == entity_exposure.STATE_READS_AT_ONCE
