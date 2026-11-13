"""The visibility hard-exclude in the fuzzy entity search and area listing.

``smart_entity_search`` and ``get_entities_by_area`` read states and the
registries from a real ``hass`` through the server's own client, so the hidden
set comes from Core's registry rows.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.visibility.model import VisibilityConfig


@pytest.fixture
async def search(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
) -> SmartSearchTools:
    """A kitchen light and a diagnostic kitchen sensor, registered with Core."""
    assert await async_setup_component(hass, "config", {})
    kitchen = area_registry.async_create("Kitchen")
    for domain, object_id, category in (
        ("light", "keep", None),
        ("sensor", "drop", EntityCategory.DIAGNOSTIC),
    ):
        entity_registry.async_get_or_create(
            domain,
            "test",
            object_id,
            suggested_object_id=object_id,
            entity_category=category,
        )
        entity_registry.async_update_entity(f"{domain}.{object_id}", area_id=kitchen.id)
        hass.states.async_set(
            f"{domain}.{object_id}",
            "on",
            {"friendly_name": f"Kitchen {object_id.title()}"},
        )
    return SmartSearchTools(client=core_client)


def _hide_diagnostic(set_config: Callable[..., VisibilityConfig]) -> None:
    set_config(enabled=True, exclude_categories=["diagnostic"])


async def test_fuzzy_search_never_returns_hidden_entity(
    search: SmartSearchTools,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """A hidden entity is not a fuzzy match, however well it scores."""
    _hide_diagnostic(set_config)

    res = await search.smart_entity_search("kitchen")

    ids = [match["entity_id"] for match in res["matches"]]
    assert "sensor.drop" not in ids
    assert "light.keep" in ids


async def test_fuzzy_search_with_filter_disabled_returns_both(
    search: SmartSearchTools,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    set_config(enabled=False, exclude_categories=["diagnostic"])

    res = await search.smart_entity_search("kitchen")

    assert {match["entity_id"] for match in res["matches"]} == {
        "light.keep",
        "sensor.drop",
    }


async def test_area_listing_hides_entity_and_counts_only_visible(
    search: SmartSearchTools,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    _hide_diagnostic(set_config)

    res = await search.get_entities_by_area(area_query="Kitchen", include_hidden=True)

    blob = json.dumps(res)
    assert "sensor.drop" not in blob
    assert "light.keep" in blob
    assert res["total_entities"] == 1


async def test_area_listing_with_filter_disabled_lists_both(
    search: SmartSearchTools,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    set_config(enabled=False, exclude_categories=["diagnostic"])

    res = await search.get_entities_by_area(area_query="Kitchen", include_hidden=True)

    blob = json.dumps(res)
    assert "sensor.drop" in blob
    assert res["total_entities"] == 2


async def test_area_listing_fails_when_states_get_no_answer(
    core_client: HomeAssistantClient,
    search: SmartSearchTools,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """With no states answer the area search fails instead of reporting an
    empty area with ``success``."""
    set_config(enabled=False)
    with (
        patch.object(
            core_client,
            "get_states",
            side_effect=HomeAssistantConnectionError("HA unreachable"),
        ),
        pytest.raises(ToolError, match="HA unreachable"),
    ):
        await search.get_entities_by_area(area_query="Kitchen")


async def test_area_listing_cancelled_during_reads_propagates_cancellation(
    hass: HomeAssistant,
    search: SmartSearchTools,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """Cancelling the area search mid registry read cancels it instead of
    degrading to an empty registry."""
    set_config(enabled=False)
    # Core's own handler still answers; the wrapper only marks the read in flight.
    registry_read_received = asyncio.Event()
    core_handler, schema = hass.data[websocket_api.DOMAIN][
        "config/entity_registry/list"
    ]

    @callback
    def note_registry_read(
        core: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        registry_read_received.set()
        core_handler(core, connection, msg)

    websocket_api.async_register_command(
        hass, "config/entity_registry/list", note_registry_read, schema
    )
    task = asyncio.create_task(search.get_entities_by_area(area_query="Kitchen"))
    try:
        async with asyncio.timeout(5):
            await registry_read_received.wait()
    finally:
        task.cancel()

    await asyncio.wait({task})
    assert task.cancelled()
