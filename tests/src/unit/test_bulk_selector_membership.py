"""Which leaves a structural bulk selector resolves to, on real Core registries."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import floor_registry as fr

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.bulk_selector import BulkSelectorValidationError

from ._bulk_selector_core import add_entity, hide_with_filter, resolve
from ._bulk_selector_core import salon as salon
from ._core_setup import add_test_entry


async def test_excluded_leaf_inside_nested_aggregate_is_never_an_operation(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """Exclusion subtraction happens after recursive membership expansion, and
    no aggregate is ever an operation itself."""
    add_entity(
        hass,
        "light.salon_group",
        area_id=salon,
        members=["light.nested", "light.table"],
    )
    add_entity(hass, "light.nested", members=["light.vitrine", "light.sofa"])
    for leaf in ("light.vitrine", "light.sofa", "light.table"):
        add_entity(hass, leaf)

    result = await resolve(
        core_client,
        {
            "domain": "light",
            "area_ids": [salon],
            "exclude_entity_ids": ["light.vitrine"],
        },
    )

    assert result.resolved_entity_ids == ("light.sofa", "light.table")
    assert result.excluded_entity_ids == ("light.vitrine",)
    assert {row["entity_id"] for row in result.operations} == {
        "light.sofa",
        "light.table",
    }


async def test_floor_and_device_area_inheritance_resolve_exactly(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    salon: str,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Floor expansion includes entities inheriting an area from their device."""
    ground = floor_registry.async_create("Ground").floor_id
    upper = floor_registry.async_create("Upper").floor_id
    area_registry.async_update(salon, floor_id=ground)
    other = area_registry.async_create("Other", floor_id=upper).id
    device = device_registry.async_get_or_create(
        config_entry_id=add_test_entry(hass).entry_id, identifiers={("test", "dev1")}
    )
    device_registry.async_update_device(device.id, area_id=salon)
    add_entity(hass, "light.inherited", device_id=device.id)
    add_entity(hass, "light.other", area_id=other)

    result = await resolve(
        core_client, {"domain": "light", "floor_ids": [ground]}, timeout_seconds=5
    )

    assert result.selected_area_ids == (salon,)
    assert result.operations == [
        {
            "entity_id": "light.inherited",
            "action": "off",
            "validate_first": True,
            "timeout_seconds": 5,
        }
    ]


async def test_child_device_inherits_parent_area_for_action_selection(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    salon: str,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """An entity of a child device is selected through its parent's area."""
    entry_id = add_test_entry(hass).entry_id
    parent = device_registry.async_get_or_create(
        config_entry_id=entry_id, identifiers={("test", "parent")}
    )
    device_registry.async_update_device(parent.id, area_id=salon)
    child = device_registry.async_get_or_create_child(
        config_entry_id=entry_id,
        identifiers={("test", "child")},
        parent_device_id=parent.id,
    )
    elsewhere = device_registry.async_get_or_create(
        config_entry_id=entry_id, identifiers={("test", "other")}
    )
    device_registry.async_update_device(
        elsewhere.id, area_id=area_registry.async_create("Other").id
    )
    add_entity(hass, "switch.child_outlet", device_id=child.id)
    add_entity(hass, "switch.other", device_id=elsewhere.id)

    result = await resolve(core_client, {"domain": "switch", "area_ids": [salon]})

    assert result.resolved_entity_ids == ("switch.child_outlet",)


async def test_membership_cycle_fails_before_returning_operations(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """A cyclic aggregate graph is an all-or-nothing preflight failure."""
    add_entity(hass, "light.one", area_id=salon, members=["light.two"])
    add_entity(hass, "light.two", members=["light.one"])

    with pytest.raises(BulkSelectorValidationError, match="cycle"):
        await resolve(core_client, {"domain": "light", "area_ids": [salon]})


async def test_dangling_member_error_names_the_referencing_aggregate(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """A stale member ID names WHICH aggregate's member list is stale, so the
    user can find the misconfigured aggregate."""
    add_entity(hass, "light.group", area_id=salon, members=["light.gone"])

    with pytest.raises(
        BulkSelectorValidationError,
        match=r"'light\.gone' does not exist.*referenced by 'light\.group'",
    ):
        await resolve(core_client, {"domain": "light", "area_ids": [salon]})


async def test_cross_domain_aggregate_root_expands_into_target_domain_leaves(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """A ``group.*`` in the area still contributes its ``light.*`` members
    that have no area of their own."""
    add_entity(
        hass,
        "group.living_room_lights",
        area_id=salon,
        members=["light.ceiling", "light.lamp"],
    )
    add_entity(hass, "light.ceiling")
    add_entity(hass, "light.lamp")

    result = await resolve(core_client, {"domain": "light", "area_ids": [salon]})

    assert result.resolved_entity_ids == ("light.ceiling", "light.lamp")


async def test_scene_in_selected_area_does_not_contribute_leaves(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    salon: str,
    area_registry: ar.AreaRegistry,
) -> None:
    """A scene's ``entity_id`` attribute names the entities it configures,
    which live elsewhere: a scene in the area must not pull them in."""
    bedroom = area_registry.async_create("Bedroom").id
    add_entity(hass, "scene.movie_night", area_id=salon, members=["light.bedroom_lamp"])
    add_entity(hass, "light.salon_ceiling", area_id=salon)
    add_entity(hass, "light.bedroom_lamp", area_id=bedroom)

    result = await resolve(core_client, {"domain": "light", "area_ids": [salon]})

    assert result.resolved_entity_ids == ("light.salon_ceiling",)


async def test_excluding_an_aggregate_excludes_every_expanded_member(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """Excluding a GROUP turns off none of its members."""
    add_entity(
        hass,
        "light.spare_group",
        area_id=salon,
        members=["light.member_one", "light.member_two"],
    )
    add_entity(hass, "light.member_one")
    add_entity(hass, "light.member_two")
    add_entity(hass, "light.unrelated", area_id=salon)

    result = await resolve(
        core_client,
        {
            "domain": "light",
            "area_ids": [salon],
            "exclude_entity_ids": ["light.spare_group"],
        },
    )

    assert result.resolved_entity_ids == ("light.unrelated",)


async def test_hue_room_group_excludes_one_member_leaf_not_the_group(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    salon: str,
) -> None:
    """The live "turn off the basement except the staircase light" case: one
    leaf of a room group is excluded, the rest are operations, and the group
    itself never is."""
    basement = area_registry.async_create("Sous-sol").id
    members = [
        "light.couloir_sous_sol_spot_01",
        "light.couloir_sous_sol_spot_02",
        "light.couloir_sous_sol_escalier",
    ]
    add_entity(hass, "light.couloir_sous_sol_hue", area_id=basement, members=members)
    for member in members:
        add_entity(hass, member)

    result = await resolve(
        core_client,
        {
            "domain": "light",
            "area_ids": [basement],
            "exclude_entity_ids": ["light.couloir_sous_sol_escalier"],
        },
    )

    assert [row["entity_id"] for row in result.operations] == [
        "light.couloir_sous_sol_spot_01",
        "light.couloir_sous_sol_spot_02",
    ]
    assert result.excluded_entity_ids == ("light.couloir_sous_sol_escalier",)


async def test_excluded_aggregate_not_reported_in_expanded_group_ids(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """``expanded_group_ids`` reports the groups that fed the selection, not a
    group named only in ``exclude_entity_ids``.

    ``light.excluded_group`` has no area: in the selected area it would also
    be a selection root, a second reason to be reported.
    """
    add_entity(hass, "light.selected_group", area_id=salon, members=["light.a"])
    add_entity(hass, "light.a")
    add_entity(hass, "light.excluded_group", members=["light.b"])
    add_entity(hass, "light.b")

    result = await resolve(
        core_client,
        {
            "domain": "light",
            "area_ids": [salon],
            "exclude_entity_ids": ["light.excluded_group"],
        },
    )

    assert result.expanded_group_ids == ("light.selected_group",)


async def test_area_ids_strips_whitespace_padding(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """A padded area_id (e.g. from copy-paste) still selects its area."""
    add_entity(hass, "light.one", area_id=salon)

    result = await resolve(core_client, {"domain": "light", "area_ids": [f" {salon} "]})

    assert result.resolved_entity_ids == ("light.one",)


async def test_visibility_filters_positive_leaf_but_keeps_exclusion_private(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """Hidden leaves are skipped and counted, and a hidden leaf the caller
    also excluded is not named in the report."""
    hide_with_filter("light.hidden", "light.secret")
    add_entity(
        hass,
        "light.group",
        area_id=salon,
        members=["light.visible", "light.hidden", "light.secret"],
    )
    for leaf in ("light.visible", "light.hidden", "light.secret"):
        add_entity(hass, leaf)

    result = await resolve(
        core_client,
        {
            "domain": "light",
            "area_ids": [salon],
            "exclude_entity_ids": ["light.secret"],
        },
    )

    assert result.resolved_entity_ids == ("light.visible",)
    assert result.excluded_entity_ids == ()
    assert result.hidden_entity_count == 2


async def test_directly_hidden_matching_root_is_counted_and_warned(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """A hidden entity assigned directly to the area is counted, and the
    result warns that something was skipped."""
    hide_with_filter("light.hidden")
    add_entity(hass, "light.visible", area_id=salon)
    add_entity(hass, "light.hidden", area_id=salon)

    result = await resolve(core_client, {"domain": "light", "area_ids": [salon]})

    assert result.resolved_entity_ids == ("light.visible",)
    assert result.hidden_entity_count == 1
    assert any("hidden by the entity visibility filter" in w for w in result.warnings)


async def test_visibility_config_warnings_reach_the_resolution(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """A visibility-config note (an unknown ``exclude_categories`` entry)
    reaches the result's warnings even when nothing is hidden."""
    hide_with_filter(exclude_categories=["typo_category"])
    add_entity(hass, "light.one", area_id=salon)

    result = await resolve(core_client, {"domain": "light", "area_ids": [salon]})

    assert result.hidden_entity_count == 0
    assert len(result.warnings) == 1
    assert "typo_category" in result.warnings[0]


async def test_registry_hidden_by_excludes_even_with_visibility_filter_disabled(
    hass: HomeAssistant, core_client: HomeAssistantClient, salon: str
) -> None:
    """An entity the user hid in Home Assistant is never selected, also on an
    install that never turned ha-mcp's own visibility filter on."""
    add_entity(hass, "light.visible", area_id=salon)
    add_entity(hass, "light.hidden_by_user", area_id=salon, hidden_by_user=True)

    result = await resolve(core_client, {"domain": "light", "area_ids": [salon]})

    assert result.resolved_entity_ids == ("light.visible",)
    assert result.hidden_entity_count == 1
