"""Exact-match ``ha_search`` on an install without the component (issue #214).

``exact_match=True`` (the default) matches the query as a substring of the
entity_id or friendly name. Without the ha_mcp_tools component the server
answers it itself from Core's states and entity registry, so every state and
registry row here is Core's own, read through the server's client.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools


def _hidden(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, object_id: str
) -> None:
    """Register ``light.<object_id>`` as hidden by the user and give it a state."""
    entry = entity_registry.async_get_or_create(
        "light",
        "test",
        object_id,
        suggested_object_id=object_id,
        hidden_by=er.RegistryEntryHider.USER,
    )
    assert entry.entity_id == f"light.{object_id}"
    hass.states.async_set(
        entry.entity_id, "on", {"friendly_name": f"Hidden {object_id}"}
    )


@pytest.fixture
async def tools(hass: HomeAssistant, core_client: HomeAssistantClient) -> SearchTools:
    """The search tools on Core holding a few lights, a switch and a sensor."""
    # ``config`` serves the entity-registry reads.
    assert await async_setup_component(hass, "config", {})
    hass.states.async_set(
        "light.living_room", "on", {"friendly_name": "Living Room Light"}
    )
    hass.states.async_set("light.bedroom", "off", {"friendly_name": "Bedroom Light"})
    hass.states.async_set("switch.kitchen", "on", {"friendly_name": "Kitchen Switch"})
    hass.states.async_set(
        "sensor.temperature", "22.5", {"friendly_name": "Temperature Sensor"}
    )
    return SearchTools(core_client, SmartSearchTools(client=core_client))


def _ids(result: dict[str, Any]) -> list[str]:
    return [row["entity_id"] for row in result["entities"]]


async def test_entity_id_fragment_finds_the_entity(tools: SearchTools) -> None:
    result = await tools.ha_search(query="living")

    assert result["success"] is True
    assert _ids(result) == ["light.living_room"]
    assert result["entities"][0]["match_type"] == "exact_match"


async def test_friendly_name_fragment_finds_the_entity(
    hass: HomeAssistant, tools: SearchTools
) -> None:
    """A fragment found only in the friendly name still matches."""
    hass.states.async_set("light.lamp_1", "on", {"friendly_name": "Reading Lamp"})

    result = await tools.ha_search(query="reading")

    assert _ids(result) == ["light.lamp_1"]


async def test_match_ignores_case(tools: SearchTools) -> None:
    result = await tools.ha_search(query="LIVING")

    assert _ids(result) == ["light.living_room"]


async def test_domain_filter_drops_other_domains(
    hass: HomeAssistant, tools: SearchTools
) -> None:
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen Light"})

    unfiltered = await tools.ha_search(query="kitchen")
    filtered = await tools.ha_search(query="kitchen", domain_filter="switch")

    assert sorted(_ids(unfiltered)) == ["light.kitchen", "switch.kitchen"]
    assert _ids(filtered) == ["switch.kitchen"]
    assert filtered["entities"][0]["domain"] == "switch"


async def test_no_match_is_an_empty_successful_answer(tools: SearchTools) -> None:
    result = await tools.ha_search(query="nonexistent")

    assert result["success"] is True
    assert result["entities"] == []
    assert result["entity_total_matches"] == 0


async def test_limit_caps_the_page_not_the_total(tools: SearchTools) -> None:
    result = await tools.ha_search(query="light", limit=1)

    assert len(result["entities"]) == 1
    assert result["entity_total_matches"] == 2


async def test_whole_name_match_ranks_above_a_fragment_match(
    hass: HomeAssistant, tools: SearchTools
) -> None:
    """A query equal to a friendly name outranks one that only contains it,
    even where the entity_id order says otherwise."""
    hass.states.async_set("light.a_desk_lamp", "on", {"friendly_name": "Desk Lamp"})
    hass.states.async_set("light.z_lamp", "on", {"friendly_name": "Lamp"})

    result = await tools.ha_search(query="lamp")

    assert _ids(result) == ["light.z_lamp", "light.a_desk_lamp"]
    first, second = result["entities"]
    assert first["score"] > second["score"]


async def test_membership_is_reported_only_on_request(
    hass: HomeAssistant, tools: SearchTools
) -> None:
    """A group's members come back, sorted, only when ``result_fields`` asks."""
    hass.states.async_set(
        "light.living_room",
        "on",
        {
            "friendly_name": "Living Room Light",
            "entity_id": ["light.member_two", "light.member_one"],
        },
    )

    default = await tools.ha_search(query="living")
    requested = await tools.ha_search(
        query="living",
        result_fields=["entity_id", "is_group", "member_entity_ids"],
    )

    assert "is_group" not in default["entities"][0]
    assert requested["entities"] == [
        {
            "entity_id": "light.living_room",
            "is_group": True,
            "member_entity_ids": ["light.member_one", "light.member_two"],
        }
    ]


async def test_hidden_member_is_withheld_when_hidden_entities_are_excluded(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, tools: SearchTools
) -> None:
    """``include_hidden=False`` also covers ids nested in a group's members:
    the list is withheld, the group signal stays."""
    _hidden(hass, entity_registry, "hidden_member")
    hass.states.async_set(
        "light.group",
        "on",
        {
            "friendly_name": "Group",
            "group_entities": ["light.hidden_member", "light.visible_member"],
        },
    )
    fields = ["entity_id", "is_group", "member_entity_ids"]

    shown = await tools.ha_search(query="group", result_fields=fields)
    withheld = await tools.ha_search(
        query="group", include_hidden=False, result_fields=fields
    )

    assert shown["entities"] == [
        {
            "entity_id": "light.group",
            "is_group": True,
            "member_entity_ids": ["light.hidden_member", "light.visible_member"],
        }
    ]
    assert withheld["entities"] == [{"entity_id": "light.group", "is_group": True}]


async def test_hidden_entity_is_kept_but_ranked_below_visible_ones(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, tools: SearchTools
) -> None:
    """By default a hidden entity stays in the results with a score penalty,
    so an equally good visible match sorts above it (#1170 finding 9)."""
    _hidden(hass, entity_registry, "guest_room")
    hass.states.async_set(
        "light.guest_room_lamp", "on", {"friendly_name": "Guest Room Lamp"}
    )

    result = await tools.ha_search(query="guest_room")

    assert _ids(result) == ["light.guest_room_lamp", "light.guest_room"]
    visible, hidden = result["entities"]
    assert hidden["score"] < visible["score"]


async def test_hidden_entity_is_dropped_when_hidden_entities_are_excluded(
    hass: HomeAssistant, entity_registry: er.EntityRegistry, tools: SearchTools
) -> None:
    _hidden(hass, entity_registry, "guest_room")
    hass.states.async_set(
        "light.guest_room_lamp", "on", {"friendly_name": "Guest Room Lamp"}
    )

    result = await tools.ha_search(query="guest_room", include_hidden=False)

    assert _ids(result) == ["light.guest_room_lamp"]
