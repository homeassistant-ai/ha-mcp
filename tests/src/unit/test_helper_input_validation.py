"""Helper create/update calls that reach Home Assistant (issue #1150).

Home Assistant validates storage-helper fields itself (#2632), so input Core
accepts must be stored, not refused by a check of the tool's own. These tests
pin what the tool still owns: a tag created without ``tag_id`` gets an id from
Core, a tag create does not wait for an entity state, and an update merges the
caller's fields into the stored item. Both write paths run: Core's own
``<type>/create`` and ``<type>/update`` (no component) and the component's
in-process write.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_helpers import HelperConfigTools

from ._helper_tool_core import create_item, fresh_name, set_up, stored_item
from ._helper_tool_core import tools as tools
from ._receipt_faults import at_entity_wait


async def test_tag_created_without_tag_id_gets_an_id_from_core(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> None:
    """Bug 9: Core's tag create reads ``tag_id``; left out, the create fails."""
    await set_up(hass, "tag")

    result = await tools.ha_config_set_helper(helper_type="tag", name=fresh_name())

    tag_id = result["data"]["id"]
    assert tag_id
    assert (await stored_item(core_client, "tag", tag_id))["id"] == tag_id


async def test_tag_created_with_tag_id_keeps_it(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> None:
    await set_up(hass, "tag")

    result = await tools.ha_config_set_helper(
        helper_type="tag", name=fresh_name(), config={"tag_id": "custom-tag-id"}
    )

    assert result["data"]["id"] == "custom-tag-id"
    assert await stored_item(core_client, "tag", "custom-tag-id")


async def _waits_for_entity(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    helper_type: str,
    config: dict[str, Any] | None = None,
) -> bool:
    """Whether a create without the component waits for the new entity."""
    await set_up(hass, helper_type)
    waits: list[bool] = []

    with at_entity_wait(hass, lambda: waits.append(True)):
        result = await HelperConfigTools(client).ha_config_set_helper(
            helper_type=helper_type, name=fresh_name(), config=config
        )

    assert result["success"] is True
    return bool(waits)


async def test_tag_create_does_not_wait_for_an_entity_state(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A tag's state is not what its create waits on: the wait would run to
    its timeout on every tag create."""
    assert not await _waits_for_entity(hass, core_client, "tag", {"tag_id": "some-tag"})


async def test_input_boolean_create_waits_for_its_entity(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The tag skip is scoped to tags: other helpers still wait, which gates
    their registry updates and the not-yet-queryable warning."""
    assert await _waits_for_entity(hass, core_client, "input_boolean")


@pytest.mark.parametrize(
    ("helper_type", "config"),
    [
        # Core and the frontend store a step wider than the range (#2632).
        ("input_number", {"min": 0, "max": 10, "step": 15}),
        ("input_text", {"min": 4, "max": 4}),
        ("input_select", {"options": ["A", "B", "C"], "initial": "B"}),
        ("input_datetime", {"has_date": True, "has_time": False}),
    ],
    ids=["step-wider-than-range", "exact-length", "initial-in-options", "date-only"],
)
async def test_create_stores_input_core_accepts(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools: HelperConfigTools,
    helper_type: str,
    config: dict[str, Any],
) -> None:
    """A check of the tool's own that refuses these blocks a helper the Home
    Assistant dialog creates."""
    await set_up(hass, helper_type)

    result = await tools.ha_config_set_helper(
        helper_type=helper_type, name=fresh_name(), config=config
    )

    item = await stored_item(core_client, helper_type, result["data"]["id"])
    assert {key: item[key] for key in config} == config


async def test_schedule_create_stores_ranges_that_touch(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> None:
    """07:00-12:00 and 12:00-14:00 do not overlap: Core stores both."""
    await set_up(hass, "schedule")

    result = await tools.ha_config_set_helper(
        helper_type="schedule",
        name=fresh_name(),
        config={
            "monday": [
                {"from": "07:00", "to": "12:00"},
                {"from": "12:00", "to": "14:00"},
            ]
        },
    )

    item_id = result["data"]["id"]
    monday = (await stored_item(core_client, "schedule", item_id))["monday"]
    assert [(r["from"][:5], r["to"][:5]) for r in monday] == [
        ("07:00", "12:00"),
        ("12:00", "14:00"),
    ]
    # Removing the schedule cancels the timer for its next block.
    removed = await core_client.send_websocket_message(
        {"type": "schedule/delete", "schedule_id": item_id}
    )
    assert removed["success"], removed


async def test_input_select_update_takes_new_options_with_an_initial_among_them(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> None:
    """The new ``initial`` is not among the stored options, only the new ones."""
    await set_up(hass, "input_select")
    item_id = await create_item(
        core_client, "input_select", {"options": ["A", "B"], "initial": "A"}
    )

    await tools.ha_config_set_helper(
        helper_type="input_select",
        helper_id=item_id,
        config={"options": ["A", "B", "C"], "initial": "C"},
    )

    item = await stored_item(core_client, "input_select", item_id)
    assert (item["options"], item["initial"]) == (["A", "B", "C"], "C")


async def test_input_datetime_update_of_has_date_keeps_the_stored_has_time(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> None:
    """Core's update replaces the whole item and defaults a missing
    ``has_time`` to off, so an update sending only ``has_date`` drops the time."""
    await set_up(hass, "input_datetime")
    item_id = await create_item(
        core_client, "input_datetime", {"has_date": True, "has_time": True}
    )

    await tools.ha_config_set_helper(
        helper_type="input_datetime", helper_id=item_id, config={"has_date": True}
    )

    item = await stored_item(core_client, "input_datetime", item_id)
    assert (item["has_date"], item["has_time"]) == (True, True)
