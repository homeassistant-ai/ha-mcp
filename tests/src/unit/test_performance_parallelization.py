"""The system overview and the config search on real Core (#258).

The overview reads the states, then services and the three registries; a slice
that gets no answer thins the overview instead of failing it. ``deep_search``
reads the states once and every ``input_*`` helper list at the same time, and
one config type it cannot read does not cost the others their results. Each
test runs the tools on the server's own client against a real ``hass``.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.smart_search import SmartSearchTools

from ._command_faults import arrival
from ._core_setup import add_test_entry, set_up_domain
from ._deep_search_core import (
    AUTOMATIONS_FILE,
    automation,
    load_automations,
    load_scripts,
    script,
    write_secret_reference,
)
from ._held_command import held_command

INPUT_HELPERS = (
    "input_boolean",
    "input_number",
    "input_select",
    "input_text",
    "input_datetime",
    "input_button",
)
# A wait the test only sits through when the helper lists are read one by one.
CONCURRENCY_GUARD_S = 10


@pytest.fixture
def tools(core_client: HomeAssistantClient) -> SmartSearchTools:
    return SmartSearchTools(client=core_client)


@pytest.fixture
async def registries(hass: HomeAssistant, core_client: HomeAssistantClient) -> None:
    """Core's ``config`` commands, which answer the registry reads."""
    assert await async_setup_component(hass, "config", {})


def _set_states(
    hass: HomeAssistant, domain: str, count: int, *, distinct: bool = False
) -> None:
    """``count`` entities of ``domain``; ``distinct`` gives each its own state."""
    for i in range(count):
        hass.states.async_set(
            f"{domain}.{domain[0]}{i}",
            f"state_{i}" if distinct else "on",
            {"friendly_name": f"{domain.title()} {i}"},
        )


def _desk_and_fan(hass: HomeAssistant) -> None:
    hass.states.async_set("light.desk", "on", {"friendly_name": "Desk"})
    hass.states.async_set("switch.fan", "off", {"friendly_name": "Fan"})


def _register_ping(hass: HomeAssistant) -> None:
    async def ping(call: ServiceCall) -> None:
        """A service Core lists under ``test``."""

    hass.services.async_register("test", "ping", ping)


def _sensors_of(response: dict[str, Any]) -> dict[str, Any]:
    sensors: dict[str, Any] = response["domain_stats"]["sensor"]
    return sensors


# ---------------------------------------------------------------------------
# get_system_overview: what a missing slice costs
# ---------------------------------------------------------------------------


async def test_answered_reads_fill_every_total(
    hass: HomeAssistant,
    registries: None,
    area_registry: ar.AreaRegistry,
    tools: SmartSearchTools,
) -> None:
    """With every read answered the totals count Core's entities, areas and
    services, and the overview is complete."""
    _desk_and_fan(hass)
    _register_ping(hass)
    area_registry.async_create("Kitchen")
    area_registry.async_create("Bedroom")

    result = await tools.get_system_overview(detail_level="standard")

    summary = result["system_summary"]
    assert summary["total_entities"] == 2
    assert summary["total_areas"] == 2
    assert summary["total_services"] >= 1
    assert "partial" not in result


async def test_unanswered_registry_reads_keep_the_entity_counts(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    registries: None,
    area_registry: ar.AreaRegistry,
    tools: SmartSearchTools,
) -> None:
    """A WebSocket that never answers costs the overview its areas, not the
    entities read over REST."""
    _desk_and_fan(hass)
    area_registry.async_create("Kitchen")

    with patch.object(
        core_client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantConnectionError("ws gone")),
    ):
        result = await tools.get_system_overview(detail_level="minimal")

    assert result["system_summary"]["total_entities"] == 2
    assert result["system_summary"]["total_areas"] == 0


async def test_unanswered_services_read_keeps_the_entity_counts(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools: SmartSearchTools,
) -> None:
    """A services read that never answers zeroes the service total only."""
    _desk_and_fan(hass)
    _register_ping(hass)

    with patch.object(
        core_client,
        "get_services",
        AsyncMock(side_effect=HomeAssistantConnectionError("services gone")),
    ):
        result = await tools.get_system_overview(detail_level="minimal")

    assert result["system_summary"]["total_entities"] == 2
    assert result["system_summary"]["total_services"] == 0


async def test_entity_without_an_area_is_counted_in_its_devices_area(
    hass: HomeAssistant,
    registries: None,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    tools: SmartSearchTools,
) -> None:
    """A light with no area of its own belongs to its device's area, as in
    Core's own area pages."""
    kitchen = area_registry.async_create("Kitchen")
    area_registry.async_create("Bedroom")
    entry = add_test_entry(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "lamp")}
    )
    device_registry.async_update_device(device.id, area_id=kitchen.id)
    entity_registry.async_get_or_create(
        "light", "test", "lamp", suggested_object_id="lamp", device_id=device.id
    )
    hass.states.async_set("light.lamp", "on")

    result = await tools.get_system_overview(detail_level="full")

    area = result["area_analysis"][kitchen.id]
    assert area["count"] == 1
    assert area["domains"] == {"light": 1}


# ---------------------------------------------------------------------------
# get_system_overview: what each detail level and filter shows
# ---------------------------------------------------------------------------


async def test_domains_filter_narrows_the_listing_but_not_the_totals(
    hass: HomeAssistant, tools: SmartSearchTools
) -> None:
    _desk_and_fan(hass)

    result = await tools.get_system_overview(
        detail_level="standard", domains_filter=["light"]
    )

    assert set(result["domain_stats"]) == {"light"}
    summary = result["system_summary"]
    assert summary["total_entities"] == 2
    assert summary["total_domains"] == 2
    assert summary["filtered_domains"] == ["light"]


async def test_minimal_overview_folds_rare_states_into_other(
    hass: HomeAssistant, tools: SmartSearchTools
) -> None:
    """Twenty sensors in twenty states: minimal keeps a short summary that
    still adds up to every sensor."""
    _set_states(hass, "sensor", 20, distinct=True)

    result = await tools.get_system_overview(detail_level="minimal")

    sensors = _sensors_of(result)
    assert sensors["count"] == 20
    assert "_other" in sensors["states_summary"]
    assert len(sensors["states_summary"]) < 20
    assert sum(sensors["states_summary"].values()) == 20


async def test_full_overview_lists_every_state(
    hass: HomeAssistant, tools: SmartSearchTools
) -> None:
    _set_states(hass, "sensor", 20, distinct=True)

    result = await tools.get_system_overview(detail_level="full")

    states = _sensors_of(result)["states_summary"]
    assert len(states) == 20
    assert "_other" not in states


async def test_standard_overview_lists_every_entity_of_a_domain(
    hass: HomeAssistant, tools: SmartSearchTools
) -> None:
    _set_states(hass, "sensor", 100)

    result = await tools.get_system_overview(detail_level="standard")

    sensors = _sensors_of(result)
    assert sensors["count"] == 100
    assert len(sensors["entities"]) == 100
    assert sensors["truncated"] is False


async def test_max_entities_per_domain_caps_the_standard_listing(
    hass: HomeAssistant, tools: SmartSearchTools
) -> None:
    _set_states(hass, "sensor", 100)

    result = await tools.get_system_overview(
        detail_level="standard", max_entities_per_domain=25
    )

    sensors = _sensors_of(result)
    assert sensors["count"] == 100
    assert len(sensors["entities"]) == 25
    assert sensors["truncated"] is True


async def test_max_entities_per_domain_zero_lifts_entity_and_state_caps(
    hass: HomeAssistant, tools: SmartSearchTools
) -> None:
    _set_states(hass, "sensor", 100, distinct=True)

    result = await tools.get_system_overview(
        detail_level="standard", max_entities_per_domain=0
    )

    sensors = _sensors_of(result)
    assert len(sensors["entities"]) == 100
    assert sensors["truncated"] is False
    assert len(sensors["states_summary"]) == 100
    assert "_other" not in sensors["states_summary"]


# ---------------------------------------------------------------------------
# get_system_overview: pagination
# ---------------------------------------------------------------------------


async def test_standard_overview_pages_a_large_install(
    hass: HomeAssistant, tools: SmartSearchTools
) -> None:
    """Without a limit, a standard overview of 300 entities returns one page
    of them and says where the next starts; the domain count stays whole."""
    _set_states(hass, "sensor", 300)

    result = await tools.get_system_overview(detail_level="standard")

    sensors = _sensors_of(result)
    page = result["pagination"]
    assert sensors["count"] == 300
    assert sensors["truncated"] is True
    assert len(sensors["entities"]) == page["entities_returned"] == page["limit"]
    assert page["limit"] < 300
    assert page["total_entity_results"] == 300
    assert page["offset"] == 0
    assert page["has_more"] is True
    assert page["next_offset"] == page["limit"]


async def test_offset_reaches_the_last_page(
    hass: HomeAssistant, tools: SmartSearchTools
) -> None:
    _set_states(hass, "sensor", 300)

    result = await tools.get_system_overview(
        detail_level="standard", limit=100, offset=200
    )

    page = result["pagination"]
    assert _sensors_of(result)["count"] == 300
    assert len(_sensors_of(result)["entities"]) == 100
    assert page["offset"] == 200
    assert page["entities_returned"] == 100
    assert page["has_more"] is False
    assert page["next_offset"] is None


async def test_minimal_overview_has_no_pagination(
    hass: HomeAssistant, tools: SmartSearchTools
) -> None:
    """Minimal samples each domain instead of paging the whole install."""
    _set_states(hass, "sensor", 50)

    result = await tools.get_system_overview(detail_level="minimal")

    assert len(_sensors_of(result)["entities"]) < 50
    assert "pagination" not in result


async def test_a_page_takes_entities_from_every_domain(
    hass: HomeAssistant, tools: SmartSearchTools
) -> None:
    """A page of a lopsided install still shows each domain, not only the
    biggest one."""
    _set_states(hass, "sensor", 150)
    _set_states(hass, "light", 50)
    _set_states(hass, "switch", 50)

    result = await tools.get_system_overview(detail_level="standard", limit=100)

    stats = result["domain_stats"]
    assert [stats[d]["count"] for d in ("sensor", "light", "switch")] == [150, 50, 50]
    assert all(len(stats[d]["entities"]) >= 3 for d in ("sensor", "light", "switch"))
    assert sum(len(ds["entities"]) for ds in stats.values()) <= 100
    assert result["pagination"]["has_more"] is True


async def test_following_next_offset_lists_every_entity_once(
    hass: HomeAssistant, tools: SmartSearchTools
) -> None:
    """Paging through three domains by ``next_offset`` returns each entity
    exactly once, one of each domain per page."""
    for domain in ("sensor", "light", "switch"):
        _set_states(hass, domain, 3)

    pages = []
    offset: int | None = 0
    while offset is not None:
        page = await tools.get_system_overview(
            detail_level="standard", limit=3, offset=offset
        )
        pages.append(page)
        offset = page["pagination"]["next_offset"]

    names = [
        entity["friendly_name"]
        for page in pages
        for domain in page["domain_stats"].values()
        for entity in domain["entities"]
    ]
    assert len(pages) == 3
    assert all(page["pagination"]["entities_returned"] == 3 for page in pages)
    assert all(
        len(domain["entities"]) == 1
        for page in pages
        for domain in page["domain_stats"].values()
    )
    assert sorted(names) == sorted(
        state.attributes["friendly_name"] for state in hass.states.async_all()
    )


async def test_an_explicit_limit_sets_the_page_size(
    hass: HomeAssistant, tools: SmartSearchTools
) -> None:
    _set_states(hass, "sensor", 300)

    result = await tools.get_system_overview(detail_level="standard", limit=50)

    assert len(_sensors_of(result)["entities"]) == 50
    assert result["pagination"]["limit"] == 50
    assert result["pagination"]["has_more"] is True


async def test_an_install_that_fits_one_page_says_nothing_more(
    hass: HomeAssistant, tools: SmartSearchTools
) -> None:
    _set_states(hass, "sensor", 50)

    result = await tools.get_system_overview(detail_level="standard")

    assert len(_sensors_of(result)["entities"]) == 50
    assert result["pagination"]["has_more"] is False
    assert result["pagination"]["next_offset"] is None


# ---------------------------------------------------------------------------
# deep_search
# ---------------------------------------------------------------------------


async def _ten_automations(hass: HomeAssistant) -> list[str]:
    await load_automations(
        hass, [automation(f"uid_{i}", f"Test Automation {i}") for i in range(10)]
    )
    return [f"automation.test_automation_{i}" for i in range(10)]


async def _five_scripts(hass: HomeAssistant) -> list[str]:
    await load_scripts(
        hass, {f"test_{i}": script(f"Test Script {i}") for i in range(5)}
    )
    return [f"script.test_{i}" for i in range(5)]


async def _test_toggle(hass: HomeAssistant, client: HomeAssistantClient) -> str:
    """A stored input_boolean named ``Test toggle <fresh suffix>``."""
    await set_up_domain(hass, "input_boolean", {})
    created = await client.send_websocket_message(
        {"type": "input_boolean/create", "name": f"Test toggle {uuid.uuid4().hex[:8]}"}
    )
    assert created["success"], created
    await hass.async_block_till_done()
    return f"input_boolean.{created['result']['id']}"


def _ids(items: list[dict[str, Any]]) -> list[str]:
    return sorted(item["entity_id"] for item in items)


async def test_every_automation_whose_name_matches_is_listed(
    hass: HomeAssistant, tools: SmartSearchTools
) -> None:
    expected = await _ten_automations(hass)

    result = await tools.deep_search(
        query="test", search_types=["automation"], limit=20
    )

    assert _ids(result["automations"]) == sorted(expected)


async def test_every_script_whose_name_matches_is_listed(
    hass: HomeAssistant, tools: SmartSearchTools
) -> None:
    expected = await _five_scripts(hass)

    result = await tools.deep_search(query="test", search_types=["script"], limit=20)

    assert _ids(result["scripts"]) == expected


async def test_a_stored_helper_whose_name_matches_is_listed(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: SmartSearchTools
) -> None:
    toggle = await _test_toggle(hass, core_client)

    result = await tools.deep_search(query="test", search_types=["helper"], limit=20)

    assert _ids(result["helpers"]) == [toggle]


async def test_one_search_returns_automations_scripts_and_helpers_together(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: SmartSearchTools
) -> None:
    automations = await _ten_automations(hass)
    scripts = await _five_scripts(hass)
    toggle = await _test_toggle(hass, core_client)

    result = await tools.deep_search(
        query="test", search_types=["automation", "script", "helper"], limit=50
    )

    assert _ids(result["automations"]) == sorted(automations)
    assert _ids(result["scripts"]) == scripts
    assert _ids(result["helpers"]) == [toggle]


async def test_a_search_of_every_type_reads_the_states_once(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: SmartSearchTools
) -> None:
    await _ten_automations(hass)
    await _five_scripts(hass)

    with patch.object(
        core_client, "get_states", AsyncMock(wraps=core_client.get_states)
    ) as states:
        await tools.deep_search(
            query="test", search_types=["automation", "script", "helper"]
        )

    states.assert_awaited_once()


async def test_helper_lists_are_requested_together(
    hass: HomeAssistant, tools: SmartSearchTools
) -> None:
    """The last helper list reaches Core while the first is still unanswered,
    so a slow helper backend does not hold up the others."""
    for domain in INPUT_HELPERS:
        await set_up_domain(hass, domain, {})

    async with held_command(hass, "input_boolean/list") as first:
        with arrival(hass, "input_button/list") as last:
            search = asyncio.create_task(
                tools.deep_search(query="test", search_types=["helper"])
            )
            async with asyncio.timeout(CONCURRENCY_GUARD_S):
                await first.arrived.wait()
                await last.wait()
        first.release.set()
        result = await search

    assert result["success"] is True


async def test_name_matches_survive_when_no_config_can_be_read(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    unreachable_client: HomeAssistantClient,
) -> None:
    """Home Assistant became unreachable after the states were read: the
    automations are still found by name."""
    expected = await _ten_automations(hass)
    states = await core_client.get_states()

    result = await SmartSearchTools(client=unreachable_client).deep_search(
        query="test", search_types=["automation"], limit=20, prefetched_states=states
    )

    assert _ids(result["automations"]) == sorted(expected)
    assert all(not item["match_in_config"] for item in result["automations"])


async def test_unreadable_automations_do_not_cost_the_scripts_their_results(
    hass: HomeAssistant, tools: SmartSearchTools
) -> None:
    """Core answers every automation config read with an error; the scripts
    are still searched in full."""
    await _ten_automations(hass)
    await write_secret_reference(hass, AUTOMATIONS_FILE)
    scripts = await _five_scripts(hass)

    result = await tools.deep_search(
        query="test", search_types=["automation", "script"], limit=20
    )

    assert "10 automation(s) not scanned" in result["partial_reason"]
    assert _ids(result["scripts"]) == scripts
