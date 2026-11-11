"""Search and the overview name an entity the way Home Assistant shows it.

Since Home Assistant 2026.11 the name on screen is composed by Core from the
parent device, device and entity names; the ``friendly_name`` state attribute
is the legacy name and can differ from it (#2965). The entity here sits on a
child device, where the two differ on any install: both names are Core's own,
read from a real ``hass``, and both tools are served by the component's
commands on it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools

from ._search_tool import _setup_visibility_disabled


@pytest.fixture
def search_tools(
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SearchTools:
    """``ha_search`` and ``ha_get_overview`` served by the component on ``hass``."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    return SearchTools(
        component_core_client, SmartSearchTools(client=component_core_client)
    )


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
    hass: HomeAssistant, search_tools: SearchTools
) -> None:
    entity_id, composed, friendly = _outlet_power_sensor(hass)

    found = await search_tools.ha_search(
        query=composed,
        result_fields=["entity_id", "name", "friendly_name"],
    )

    assert [row["entity_id"] for row in found["entities"]] == [entity_id]
    row = found["entities"][0]
    assert row["name"] == composed
    assert row["friendly_name"] == friendly


async def test_overview_lists_an_entity_under_the_name_home_assistant_shows(
    hass: HomeAssistant, search_tools: SearchTools
) -> None:
    entity_id, composed, friendly = _outlet_power_sensor(hass)

    overview = await search_tools.ha_get_overview(detail_level="full")

    # The legacy path knows no composed name; it must not have answered.
    assert not any("legacy path" in w for w in overview.get("warnings", []))
    rows = overview["domain_stats"]["sensor"]["entities"]
    row = next(row for row in rows if row["entity_id"] == entity_id)
    assert row["name"] == composed
    assert row["friendly_name"] == friendly


async def test_overview_does_not_repeat_a_name_home_assistant_shows_unchanged(
    hass: HomeAssistant, search_tools: SearchTools
) -> None:
    entry = er.async_get(hass).async_get_or_create(
        "sensor", "test", "plain", original_name="Plain"
    )
    hass.states.async_set(
        entry.entity_id,
        "1",
        {"friendly_name": er.async_get_legacy_friendly_name(hass, entry)},
    )

    overview = await search_tools.ha_get_overview(detail_level="full")

    # The legacy path knows no composed name; it must not have answered.
    assert not any("legacy path" in w for w in overview.get("warnings", []))
    rows = overview["domain_stats"]["sensor"]["entities"]
    row = next(row for row in rows if row["entity_id"] == entry.entity_id)
    assert "name" not in row
