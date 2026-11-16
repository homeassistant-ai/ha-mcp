"""``ha_bulk_control`` operations mode, and the choice between the two modes.

An operations batch that targets a group/aggregate together with one of its
own members fails closed: Home Assistant fans a call on the group out to every
member, whatever else the batch lists. The groups are Core states whose
``entity_id`` attribute lists their members, read back through the server's
own client; ``SwitchedStates`` serves the light services as a device would.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.tools.tools_service import BulkControlOperation, ServiceTools

from ._ha_bulk_control_selector_core import SALON, bulk_tools, switched
from ._switched_states import SwitchedStates


@pytest.fixture
def devices(hass: HomeAssistant) -> SwitchedStates:
    return SwitchedStates(hass)


def furnish(hass: HomeAssistant, entities: dict[str, list[str] | None]) -> None:
    """Each entity ``on``; one with a member list is a group of them."""
    for entity_id, members in entities.items():
        attributes = {} if members is None else {"entity_id": members}
        hass.states.async_set(entity_id, "on", attributes)


def off(*entity_ids: str) -> list[BulkControlOperation]:
    return [{"entity_id": entity_id, "action": "off"} for entity_id in entity_ids]


async def rejection(client: HomeAssistantClient, operations: list[Any]) -> str:
    with pytest.raises(ToolError) as exc_info:
        await bulk_tools(client).ha_bulk_control(operations, False)
    message: str = json.loads(str(exc_info.value))["error"]["message"]
    return message


# --- choosing a mode -------------------------------------------------------------


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({}, id="neither"),
        pytest.param(
            {"operations": off("light.one"), "selector": SALON, "action": "off"},
            id="both",
        ),
    ],
)
async def test_exactly_one_target_mode_is_required(
    core_client: HomeAssistantClient, arguments: dict[str, Any]
) -> None:
    with pytest.raises(ToolError, match="exactly one"):
        await bulk_tools(core_client).ha_bulk_control(**arguments)


async def test_operations_mode_rejects_dry_run(
    core_client: HomeAssistantClient,
) -> None:
    """The message names ``dry_run``, not ``selector``, which was never passed."""
    with pytest.raises(ToolError, match="'dry_run' is a selector-only parameter"):
        await bulk_tools(core_client).ha_bulk_control(
            operations=off("light.one"), dry_run=True
        )


@pytest.mark.parametrize(
    ("kwargs", "offender"),
    [
        ({"action": "off"}, "action"),
        ({"parameters": {"brightness_pct": 30}}, "parameters"),
        ({"timeout_seconds": 5.0}, "timeout_seconds"),
        ({"validate_first": False}, "validate_first"),
    ],
)
async def test_a_per_row_field_given_at_the_top_level_is_sent_onto_each_row(
    core_client: HomeAssistantClient, kwargs: dict[str, Any], offender: str
) -> None:
    """Each names itself, and the remedy moves the value onto each row rather
    than discarding what the caller asked for."""
    with pytest.raises(
        ToolError, match=f"'{offender}' is a per-operation field in operations mode"
    ):
        await bulk_tools(core_client).ha_bulk_control(
            operations=off("light.one"), **kwargs
        )


async def test_the_action_example_row_has_one_action_key(
    core_client: HomeAssistantClient,
) -> None:
    """A model told to copy the example would copy a duplicate key."""
    with pytest.raises(ToolError) as exc_info:
        await bulk_tools(core_client).ha_bulk_control(
            operations=off("light.one"), action="off"
        )

    message = json.loads(str(exc_info.value))["error"]["message"]
    assert "'action': 'on', 'action'" not in message, message


async def test_the_timeout_example_row_shows_a_number(
    core_client: HomeAssistantClient,
) -> None:
    """A quoted placeholder would teach the model to send a string."""
    with pytest.raises(ToolError) as exc_info:
        await bulk_tools(core_client).ha_bulk_control(
            operations=off("light.one"), timeout_seconds=5.0
        )

    message = json.loads(str(exc_info.value))["error"]["message"]
    assert "'timeout_seconds': 5" in message, message
    assert "'timeout_seconds': '...'" not in message, message


# --- operations that dispatch --------------------------------------------------------


async def test_positional_operations_and_parallel_still_dispatch(
    hass: HomeAssistant, core_client: HomeAssistantClient, devices: SwitchedStates
) -> None:
    furnish(hass, {"light.one": None})
    device_tools = DeviceControlTools(core_client)
    operations = off("light.one")

    with patch.object(
        device_tools, "bulk_device_control", wraps=device_tools.bulk_device_control
    ) as bulk:
        await ServiceTools(core_client, device_tools).ha_bulk_control(operations, False)

    bulk.assert_awaited_once_with(operations=operations, parallel=False, ctx=None)
    assert switched(devices) == [("turn_off", "light.one")]


@pytest.mark.parametrize(
    ("entity_ids", "states_reads"),
    [
        pytest.param(("light.one", "light.one"), 0, id="one-entity-twice"),
        pytest.param(("light.one", "light.two"), 1, id="two-entities"),
    ],
)
async def test_the_group_check_reads_states_only_for_two_or_more_entities(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    devices: SwitchedStates,
    entity_ids: tuple[str, ...],
    states_reads: int,
) -> None:
    """One entity cannot conflict with itself, so it costs no states read."""
    furnish(hass, {"light.one": None, "light.two": None})

    with patch.object(
        core_client, "get_states", AsyncMock(wraps=core_client.get_states)
    ) as get_states:
        await bulk_tools(core_client).ha_bulk_control(off(*entity_ids), False)

    assert get_states.await_count == states_reads
    assert len(devices.calls) == len(entity_ids)


@pytest.mark.parametrize(
    ("entities", "targets"),
    [
        pytest.param(
            {"light.group": ["light.a", "light.b"], "light.other": None},
            ("light.group", "light.other"),
            id="the-group-without-its-members",
        ),
        pytest.param(
            {
                "light.group": ["light.a", "light.b", "light.c"],
                "light.a": None,
                "light.b": None,
            },
            ("light.a", "light.b"),
            id="members-without-their-group",
        ),
        pytest.param(
            {
                "light.outer_a": ["light.inner_a"],
                "light.inner_a": ["light.leaf_a"],
                "light.leaf_a": None,
                "light.outer_b": ["light.inner_b"],
                "light.inner_b": ["light.leaf_b"],
                "light.leaf_b": None,
            },
            ("light.outer_a", "light.leaf_b"),
            id="unrelated-nested-groups",
        ),
        pytest.param(
            {"light.a": ["light.b"], "light.b": ["light.a"], "light.c": None},
            ("light.a", "light.c"),
            id="a-membership-cycle",
        ),
    ],
)
async def test_a_batch_without_a_group_member_conflict_dispatches(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    devices: SwitchedStates,
    entities: dict[str, list[str] | None],
    targets: tuple[str, ...],
) -> None:
    furnish(hass, entities)

    await bulk_tools(core_client).ha_bulk_control(off(*targets), False)

    assert switched(devices) == [("turn_off", target) for target in targets]


# --- group/member conflicts ------------------------------------------------------------


async def test_a_group_with_most_of_its_members_names_the_one_left_out(
    hass: HomeAssistant, core_client: HomeAssistantClient, devices: SwitchedStates
) -> None:
    """Confirmed live: a Hue room plus 4 of its 5 spots, the 5th left out to
    spare it, still switched the 5th through the room's own fan-out. The
    message names it and shows selector mode with ``exclude_entity_ids``
    inside ``selector``, the call the model got wrong when retrying."""
    spots = [f"light.spot_{index}" for index in range(1, 5)]
    furnish(hass, {"light.room": [*spots, "light.stairs"]})

    message = await rejection(core_client, off("light.room", *spots))

    assert "group/aggregate entity" in message
    assert "will also affect light.stairs" in message
    assert "is redundant with" not in message
    assert '"selector": {"domain": "light"' in message
    assert '"exclude_entity_ids"' in message
    assert "ha_list_floors_areas" in message
    assert devices.calls == []


async def test_a_large_group_lists_ten_members_and_counts_the_rest(
    hass: HomeAssistant, core_client: HomeAssistantClient, devices: SwitchedStates
) -> None:
    """The visibility check refuses an error naming a hidden entity, so a
    large group must not list every member."""
    members = [f"light.member_{index:02d}" for index in range(20)]
    furnish(hass, {"light.group": members})

    message = await rejection(core_client, off("light.group", "light.member_00"))

    assert "(+9 more)" in message
    assert message.count("light.member_") == 10
    assert devices.calls == []


async def test_a_scene_with_an_entity_it_sets_is_refused_without_the_selector_remedy(
    hass: HomeAssistant, core_client: HomeAssistantClient, devices: SwitchedStates
) -> None:
    """``scene.turn_on`` sets every entity the scene configures, the same race
    as a group; selector mode cannot exclude part of a scene, so it is not
    offered."""
    furnish(hass, {"scene.movie_night": ["light.living_room"]})

    message = await rejection(
        core_client,
        [
            {"entity_id": "scene.movie_night", "action": "on"},
            {"entity_id": "light.living_room", "action": "off"},
        ],
    )

    assert "'scene.movie_night' is redundant with" in message
    assert "will also affect" not in message
    assert "selector mode" not in message
    assert devices.calls == []


@pytest.mark.parametrize(
    ("entities", "operations"),
    [
        pytest.param(
            {"light.group": ["light.a", "light.b"]},
            [
                {"entity_id": "light.group", "action": "on"},
                {"entity_id": "light.a", "action": "off"},
            ],
            id="opposing-actions",
        ),
        pytest.param(
            {
                "light.outer": ["light.inner"],
                "light.inner": ["light.leaf_a", "light.leaf_b"],
            },
            off("light.outer", "light.leaf_a"),
            id="a-leaf-of-a-nested-group",
        ),
        pytest.param(
            {
                "light.group": ["scene.movie_night"],
                "scene.movie_night": ["light.living_room"],
            },
            off("light.group", "light.living_room"),
            id="an-entity-set-by-a-member-scene",
        ),
    ],
)
async def test_a_group_with_one_of_its_transitive_members_is_refused(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    devices: SwitchedStates,
    entities: dict[str, list[str] | None],
    operations: list[dict[str, Any]],
) -> None:
    """An opposing action races the fan-out as much as the same one does."""
    furnish(hass, entities)

    message = await rejection(core_client, operations)

    assert "group/aggregate entity" in message
    assert devices.calls == []


async def test_a_batch_whose_groups_cannot_be_checked_is_not_dispatched(
    hass: HomeAssistant, core_client: HomeAssistantClient, devices: SwitchedStates
) -> None:
    """An unverifiable batch is not a verified-safe one."""
    furnish(hass, {"light.one": None, "light.two": None})

    with (
        patch.object(
            core_client,
            "get_states",
            AsyncMock(side_effect=HomeAssistantConnectionError("websocket dropped")),
        ),
        pytest.raises(ToolError, match="CONNECTION_FAILED"),
    ):
        await bulk_tools(core_client).ha_bulk_control(
            off("light.one", "light.two"), False
        )

    assert devices.calls == []
