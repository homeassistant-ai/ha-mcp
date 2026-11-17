"""Selectors refused before any operation exists, on real Core registries.

Each empty-result cause gets its own message, so the user is sent to the
actual mistake (a domain typo, the wrong area, an exclusion, a hidden entity)
rather than to a part of the selector that is fine.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.bulk_selector import BulkSelectorValidationError

from ._bulk_selector_core import add_entity, hide_with_filter, resolve
from ._bulk_selector_core import salon as salon


async def test_unknown_domain_fails_closed_with_a_clear_message(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """A domain typo ('lights') names the domain, not the user's (correct)
    area or exclusions."""
    add_entity(hass, "light.one", area_id=salon)

    with pytest.raises(
        BulkSelectorValidationError, match="No entities of domain 'lights'"
    ):
        await resolve(core_client, {"domain": "lights", "area_ids": [salon]})


async def test_invalid_action_for_a_real_domain_names_valid_actions(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    add_entity(hass, "light.one", area_id=salon)

    with pytest.raises(
        BulkSelectorValidationError, match="Invalid action 'explode' for domain 'light'"
    ):
        await resolve(
            core_client, {"domain": "light", "area_ids": [salon]}, action="explode"
        )


async def test_valid_domain_absent_from_selected_area_names_the_area_not_exclusions(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    salon: str,
    area_registry: ar.AreaRegistry,
) -> None:
    """A real domain with no entity in the selected area says so."""
    add_entity(
        hass, "light.elsewhere", area_id=area_registry.async_create("Bedroom").id
    )

    with pytest.raises(
        BulkSelectorValidationError,
        match=r"No entities of domain 'light' exist in the selected area\(s\)$",
    ):
        await resolve(core_client, {"domain": "light", "area_ids": [salon]})


async def test_cross_domain_aggregate_with_no_matching_leaves_names_the_domain(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    salon: str,
    area_registry: ar.AreaRegistry,
) -> None:
    """A ``group.*`` in the area whose members are all switches is not
    reported as "excluded or hidden": nothing was excluded or hidden."""
    add_entity(
        hass, "group.living_room", area_id=salon, members=["switch.a", "switch.b"]
    )
    add_entity(hass, "switch.a")
    add_entity(hass, "switch.b")
    # Elsewhere, so 'light' is a real domain of this home.
    add_entity(
        hass, "light.elsewhere", area_id=area_registry.async_create("Bedroom").id
    )

    with pytest.raises(
        BulkSelectorValidationError,
        match="members are all a different domain",
    ):
        await resolve(core_client, {"domain": "light", "area_ids": [salon]})


async def test_empty_aggregate_reports_no_members_not_excluded_or_hidden(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    salon: str,
    area_registry: ar.AreaRegistry,
) -> None:
    """A matched aggregate with an empty member list says it has no members."""
    add_entity(hass, "group.empty_lights", area_id=salon, members=[])
    add_entity(
        hass, "light.elsewhere", area_id=area_registry.async_create("Cuisine").id
    )

    with pytest.raises(BulkSelectorValidationError, match="have no members"):
        await resolve(core_client, {"domain": "light", "area_ids": [salon]})


async def test_all_roots_hidden_reports_hidden_not_domain_missing(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """The only light of the area is hidden: the user is told it is hidden,
    not that the area has no light."""
    add_entity(hass, "light.hidden_by_user", area_id=salon, hidden_by_user=True)

    with pytest.raises(
        BulkSelectorValidationError, match=r"excluded or hidden \(1 hidden\)"
    ):
        await resolve(core_client, {"domain": "light", "area_ids": [salon]})


async def test_wrong_domain_aggregate_does_not_mask_a_separately_hidden_match(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """A visible switch group in the area does not hide the fact that the
    area's light is hidden."""
    add_entity(hass, "light.hidden_one", area_id=salon, hidden_by_user=True)
    add_entity(hass, "group.switches", area_id=salon, members=["switch.one"])
    add_entity(hass, "switch.one")

    with pytest.raises(
        BulkSelectorValidationError, match=r"excluded or hidden \(1 hidden\)"
    ):
        await resolve(core_client, {"domain": "light", "area_ids": [salon]})


async def test_all_matches_excluded_names_exclusion_not_area(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """Every match excluded by the caller: the message counts the exclusion."""
    add_entity(hass, "light.only", area_id=salon)

    with pytest.raises(
        BulkSelectorValidationError, match=r"excluded or hidden \(1 excluded\)"
    ):
        await resolve(
            core_client,
            {
                "domain": "light",
                "area_ids": [salon],
                "exclude_entity_ids": ["light.only"],
            },
        )


async def test_all_matches_excluded_and_hidden_reports_both_counts(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    hide_with_filter("light.hidden_one")
    add_entity(hass, "light.excluded_one", area_id=salon)
    add_entity(hass, "light.hidden_one", area_id=salon)

    with pytest.raises(
        BulkSelectorValidationError,
        match=r"excluded or hidden \(1 excluded, 1 hidden\)",
    ):
        await resolve(
            core_client,
            {
                "domain": "light",
                "area_ids": [salon],
                "exclude_entity_ids": ["light.excluded_one"],
            },
        )


def _room_group(hass: HomeAssistant, area_id: str, size: int) -> None:
    lights = [f"light.entity_{index}" for index in range(size)]
    add_entity(hass, "light.room", area_id=area_id, members=lights)
    for light in lights:
        hass.states.async_set(light, "on")


async def test_selector_entity_limit_fails_closed(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """More lights than the selector limit are refused before any dispatch."""
    _room_group(hass, salon, 101)

    with pytest.raises(BulkSelectorValidationError, match="maximum is 100"):
        await resolve(core_client, {"domain": "light", "area_ids": [salon]})


async def test_selector_entity_limit_boundary_allows_exactly_max(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    _room_group(hass, salon, 100)

    result = await resolve(core_client, {"domain": "light", "area_ids": [salon]})

    assert len(result.resolved_entity_ids) == 100


@pytest.mark.parametrize(
    ("selector", "message"),
    [
        ({"domain": "light", "floor_ids": ["unknown"]}, "unknown floor_ids"),
        ({"domain": "light"}, "at least one"),
        (
            {"domain": "light", "area_ids": ["salon"], "name": "Salon"},
            "unsupported fields: name",
        ),
    ],
)
@pytest.mark.usefixtures("salon")
async def test_invalid_structural_ids_fail_closed(
    core_client: HomeAssistantClient, selector: dict[str, Any], message: str
) -> None:
    """Invalid exact topology never degrades to a broader match."""
    with pytest.raises(BulkSelectorValidationError, match=message):
        await resolve(core_client, selector)


@pytest.mark.usefixtures("salon")
async def test_unknown_area_id_message_points_to_the_lookup_tool(
    core_client: HomeAssistantClient,
) -> None:
    """A display name passed as an area id is refused with the way to the
    exact id; without it a caller retried the same name over and over."""
    with pytest.raises(
        BulkSelectorValidationError,
        match=r"unknown area_ids: Couloir Sous-Sol\..*"
        r"exact Home Assistant registry IDs.*ha_list_floors_areas",
    ):
        await resolve(
            core_client, {"domain": "light", "area_ids": ["Couloir Sous-Sol"]}
        )
