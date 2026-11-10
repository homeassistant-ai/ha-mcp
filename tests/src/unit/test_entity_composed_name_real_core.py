"""Search and the overview name an entity the way Home Assistant shows it.

Since Home Assistant 2026.11 the name on screen is composed by Core from the
parent device, device and entity names; the ``friendly_name`` state attribute
is the legacy name and can differ from it (#2965). The entity here sits on a
child device, where the two differ on any install: both names are Core's own,
read from a real ``hass``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools.websocket_api.overview import _do_overview
from custom_components.ha_mcp_tools.websocket_api.search import _do_search
from ha_mcp.tools.search.overview import _build_overview_slices
from ha_mcp.tools.smart_search import SmartSearchTools


def _outlet_power_sensor(hass: HomeAssistant) -> tuple[str, str, str]:
    """Register a sensor on a power strip's outlet.

    Returns its entity_id, the name Core composes for it and its legacy
    ``friendly_name``, which the state carries as an entity's state does.
    """
    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    strip = devices.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "strip")},
        name="Power strip",
    )
    outlet = devices.async_get_or_create_child(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "outlet_1")},
        name="Outlet 1",
        parent_device_id=strip.id,
    )
    entry = er.async_get(hass).async_get_or_create(
        "sensor",
        "test",
        "outlet_1_power",
        config_entry=config_entry,
        device_id=outlet.id,
        has_entity_name=True,
        original_name="Power",
    )
    composed = er.async_get_full_entity_name(hass, entry)
    friendly = er.async_get_legacy_friendly_name(hass, entry)
    assert composed != friendly, "Core no longer tells the two names apart"
    hass.states.async_set(entry.entity_id, "50", {"friendly_name": friendly})
    return entry.entity_id, composed, friendly


async def test_search_finds_an_entity_by_the_name_home_assistant_shows(
    hass: HomeAssistant,
) -> None:
    entity_id, composed, friendly = _outlet_power_sensor(hass)

    found = _do_search(hass, {"query": composed, "search_types": ["entity"]})

    assert [row["entity_id"] for row in found["entities"]] == [entity_id]
    row = found["entities"][0]
    assert row["name"] == composed
    assert row["friendly_name"] == friendly


async def test_overview_lists_an_entity_under_the_name_home_assistant_shows(
    hass: HomeAssistant,
) -> None:
    entity_id, composed, friendly = _outlet_power_sensor(hass)
    slices = _build_overview_slices(_do_overview(hass, {}))
    assert slices is not None

    overview = await SmartSearchTools(client=AsyncMock()).get_system_overview(
        detail_level="full", prefetched_slices=slices.registry_slices
    )

    rows = overview["domain_stats"]["sensor"]["entities"]
    row = next(row for row in rows if row["entity_id"] == entity_id)
    assert row["name"] == composed
    assert row["friendly_name"] == friendly


async def test_overview_does_not_repeat_a_name_home_assistant_shows_unchanged(
    hass: HomeAssistant,
) -> None:
    entry = er.async_get(hass).async_get_or_create(
        "sensor", "test", "plain", original_name="Plain"
    )
    hass.states.async_set(
        entry.entity_id,
        "1",
        {"friendly_name": er.async_get_legacy_friendly_name(hass, entry)},
    )
    slices = _build_overview_slices(_do_overview(hass, {}))
    assert slices is not None

    overview = await SmartSearchTools(client=AsyncMock()).get_system_overview(
        detail_level="full", prefetched_slices=slices.registry_slices
    )

    rows = overview["domain_stats"]["sensor"]["entities"]
    row = next(row for row in rows if row["entity_id"] == entry.entity_id)
    assert "name" not in row
