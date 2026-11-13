"""The visibility filter in ``get_system_overview``, on real Core.

The overview reads states, services and the area, entity and device registries
from Core through the server's own client, so the hidden set is resolved from
Core's registry rows. Count coherence: ``total_entities`` and ``domain_stats``
describe the filtered universe.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components import api
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.visibility.model import VisibilityConfig

_OPTIONAL_READS = [
    "config/area_registry/list",
    "config/entity_registry/list",
    "config/device_registry/list",
]


@pytest.fixture
async def overview(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
) -> SmartSearchTools:
    """A light and a diagnostic sensor on a ``hass`` that serves the registries."""
    assert await async_setup_component(hass, "config", {})
    entity_registry.async_get_or_create(
        "light", "test", "keep", suggested_object_id="keep"
    )
    entity_registry.async_get_or_create(
        "sensor",
        "test",
        "drop_batt",
        suggested_object_id="drop_batt",
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    hass.states.async_set("light.keep", "on")
    hass.states.async_set("sensor.drop_batt", "50")
    return SmartSearchTools(client=core_client)


async def test_overview_drops_entity_in_area_inherited_from_its_device(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    overview: SmartSearchTools,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """An entity with no area of its own is hidden by its device's area."""
    set_config(enabled=True, exclude_areas=["garage"])
    garage = area_registry.async_create("Garage")
    assert garage.id == "garage"
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "spot")}
    )
    device_registry.async_update_device(device.id, area_id=garage.id)
    entity_registry.async_get_or_create(
        "light",
        "test",
        "spot",
        suggested_object_id="spot",
        device_id=device.id,
    )
    hass.states.async_set("light.spot", "on")

    res = await overview.get_system_overview(detail_level="full")

    listed = {
        e["entity_id"]
        for stats in res["domain_stats"].values()
        for e in stats["entities"]
    }
    assert "light.spot" not in listed
    assert "light.keep" in listed
    assert res["system_summary"]["total_entities"] == 2


async def test_overview_hidden_category_leaves_totals_and_domains_coherent(
    overview: SmartSearchTools,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """A hidden diagnostic sensor is in neither the totals nor the domain rollup."""
    set_config(enabled=True, exclude_categories=["diagnostic"])

    res = await overview.get_system_overview(detail_level="full")

    assert res["success"] is True
    assert res["system_summary"]["total_entities"] == 1
    assert res["system_summary"]["total_domains"] == 1
    assert "light" in res["domain_stats"]
    assert "sensor" not in res["domain_stats"]


async def test_overview_with_filter_disabled_keeps_every_entity(
    overview: SmartSearchTools,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    set_config(enabled=False, exclude_categories=["diagnostic"])

    res = await overview.get_system_overview(detail_level="full")

    assert res["system_summary"]["total_entities"] == 2
    assert {"light", "sensor"} <= set(res["domain_stats"])


async def test_overview_reads_states_first_then_optional_slices_together(
    core_client: HomeAssistantClient,
    overview: SmartSearchTools,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """States are read first; the services and registry reads are all sent
    before Core serves the services request, so they overlap."""
    set_config(enabled=False)
    order = Mock()
    with (
        patch.object(
            core_client, "get_states", AsyncMock(wraps=core_client.get_states)
        ) as states,
        patch.object(
            core_client, "get_services", AsyncMock(wraps=core_client.get_services)
        ) as services,
        patch.object(
            core_client,
            "send_websocket_message",
            AsyncMock(wraps=core_client.send_websocket_message),
        ) as send,
        patch.object(
            api, "async_services_json", wraps=api.async_services_json
        ) as core_services,
    ):
        order.attach_mock(states, "states")
        order.attach_mock(services, "services")
        order.attach_mock(send, "send")
        order.attach_mock(core_services, "core_services")

        res = await overview.get_system_overview(detail_level="minimal")

    assert res["success"] is True
    names = [call[0] for call in order.mock_calls]
    assert names[0] == "states"
    assert names.index("core_services") > max(
        i for i, name in enumerate(names) if name == "send"
    )
    assert sorted(call.args[0]["type"] for call in send.await_args_list) == sorted(
        _OPTIONAL_READS
    )
    services.assert_awaited_once()


async def test_overview_stops_when_mandatory_states_read_gets_no_answer(
    core_client: HomeAssistantClient,
    overview: SmartSearchTools,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """With no states answer, no optional read is sent and the overview fails."""
    set_config(enabled=False)
    with (
        patch.object(
            core_client,
            "get_states",
            side_effect=HomeAssistantConnectionError("states unavailable"),
        ),
        patch.object(
            core_client, "get_services", AsyncMock(wraps=core_client.get_services)
        ) as services,
        patch.object(
            core_client,
            "send_websocket_message",
            AsyncMock(wraps=core_client.send_websocket_message),
        ) as send,
        pytest.raises(ToolError, match="states unavailable"),
    ):
        await overview.get_system_overview(detail_level="minimal")

    services.assert_not_called()
    send.assert_not_called()


async def test_overview_visibility_warning_does_not_mark_partial(
    overview: SmartSearchTools,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """An unknown category warns on complete data without setting ``partial``."""
    set_config(enabled=True, exclude_categories=["typo"])

    res = await overview.get_system_overview(detail_level="full")

    assert any("unknown exclude_categories" in w for w in res.get("warnings", []))
    assert res.get("partial") is not True


async def test_overview_services_failure_marks_partial_beside_visibility_warning(
    core_client: HomeAssistantClient,
    overview: SmartSearchTools,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """An unanswered services read sets ``partial`` and names itself, and a
    visibility warning still rides along in the same ``warnings`` list."""
    set_config(enabled=True, exclude_categories=["typo"])

    with patch.object(
        core_client,
        "get_services",
        side_effect=HomeAssistantConnectionError("services boom"),
    ):
        res = await overview.get_system_overview(detail_level="full")

    assert res.get("partial") is True
    warnings = res.get("warnings", [])
    assert any("Services unavailable" in w for w in warnings)
    assert any("unknown exclude_categories" in w for w in warnings)


async def test_overview_cancelled_during_states_read_propagates_cancellation(
    core_client: HomeAssistantClient,
    overview: SmartSearchTools,
    set_config: Callable[..., VisibilityConfig],
) -> None:
    """Cancelling the overview mid states read cancels it instead of being
    turned into an error result."""
    set_config(enabled=False)
    with patch.object(
        core_client, "get_states", AsyncMock(wraps=core_client.get_states)
    ) as states:
        task = asyncio.create_task(overview.get_system_overview(detail_level="full"))
        await asyncio.sleep(0)
        states.assert_called_once()
        task.cancel()

        await asyncio.wait({task})
        assert task.cancelled()
