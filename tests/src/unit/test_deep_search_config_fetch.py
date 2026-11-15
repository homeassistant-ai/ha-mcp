"""deep_search reads every automation, script and scene config, under a budget.

Home Assistant has no bulk config endpoint, so deep_search fetches each config
by id in parallel batches under a wall-clock budget (#879). These tests run it
on the server's own client against a real ``hass`` whose config views read the
files ``_deep_search_core`` writes, and check what the caller is told when a
config could not be read.
"""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import NON_ADMIN_TOKEN_WARNING, HomeAssistantClient
from ha_mcp.tools.smart_search import (
    AUTOMATION_CONFIG_TIME_BUDGET,
    INDIVIDUAL_FETCH_BATCH_SIZE,
    SCENE_CONFIG_TIME_BUDGET,
)

from ._deep_search_core import (
    AUTOMATIONS_FILE,
    automation,
    load_automations,
    load_scenes,
    load_scripts,
    scene,
    script,
    search_tools,
    stepping_clock,
    write_secret_reference,
)

# Three batches' worth, so a budget that runs out after the first leaves two.
THREE_BATCHES = 3 * INDIVIDUAL_FETCH_BATCH_SIZE


async def test_an_automation_referencing_the_query_only_in_a_condition_is_found(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """#879: every config is read, not only those whose name matches, so a
    reference inside a condition is found."""
    await load_automations(
        hass,
        [
            automation(
                "uid_morning",
                "Morning Routine",
                conditions=[
                    {
                        "condition": "numeric_state",
                        "entity_id": "sensor.kitchen_temp",
                        "below": 20,
                    }
                ],
            ),
            automation("uid_evening", "Evening Lights"),
        ],
    )

    result = await search_tools(core_client).deep_search(
        query="kitchen_temp", search_types=["automation"], limit=10
    )

    assert [r["entity_id"] for r in result["automations"]] == [
        "automation.morning_routine"
    ]
    assert "partial" not in result


async def test_a_script_referencing_the_query_only_in_its_sequence_is_found(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await load_scripts(
        hass,
        {
            "morning_coffee": script("Morning Coffee"),
            "night_lockup": script(
                "Night Lockup",
                {"action": "lock.lock", "target": {"entity_id": "lock.front_door"}},
            ),
        },
    )

    result = await search_tools(core_client).deep_search(
        query="front_door", search_types=["script"], limit=10
    )

    assert [r["entity_id"] for r in result["scripts"]] == ["script.night_lockup"]
    assert "partial" not in result


async def test_the_default_budget_stops_launching_batches_once_spent(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The first batch runs; the budget is spent before the second, so the
    other two batches are reported as not scanned."""
    await load_automations(
        hass, [automation(f"uid_{i}", f"Automation {i}") for i in range(THREE_BATCHES)]
    )
    clock = stepping_clock(0, 0, AUTOMATION_CONFIG_TIME_BUDGET + 1)

    result = await search_tools(core_client, clock=clock).deep_search(
        query="test", search_types=["automation"], limit=10
    )

    assert (
        f"{2 * INDIVIDUAL_FETCH_BATCH_SIZE} automation(s) not scanned "
        "(time budget exhausted)" in result["partial_reason"]
    )


async def test_a_per_call_budget_replaces_the_scene_default(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """``config_time_budget=`` holds for scenes too: the clock passes half the
    default after the first batch, which the default allows and the
    half-default override does not."""
    await load_scenes(
        hass, [scene(f"uid_{i}", f"Scene {i}") for i in range(THREE_BATCHES)]
    )
    clock = stepping_clock(0, 0, SCENE_CONFIG_TIME_BUDGET * 0.75)

    result = await search_tools(core_client, clock=clock).deep_search(
        query="test",
        search_types=["scene"],
        limit=10,
        config_time_budget=SCENE_CONFIG_TIME_BUDGET / 2,
    )

    assert (
        f"{2 * INDIVIDUAL_FETCH_BATCH_SIZE} scene(s) not scanned "
        "(time budget exhausted)" in result["partial_reason"]
    )


async def test_a_stored_automation_matches_beside_a_yaml_defined_one(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Only the YAML-defined automation is reported as skipped; the stored one
    is matched on a reference that is only in its config."""
    await load_automations(
        hass,
        [
            automation(
                "uid_ui_one",
                "UI One",
                actions=[
                    {
                        "action": "light.turn_on",
                        "target": {"entity_id": "light.lockup_marker"},
                    }
                ],
            )
        ],
        yaml_only=[automation("uid_yaml_one", "YAML One")],
    )

    result = await search_tools(core_client).deep_search(
        query="lockup_marker", search_types=["automation"], limit=10
    )

    assert [r["entity_id"] for r in result["automations"]] == ["automation.ui_one"]
    reason = result["partial_reason"]
    assert "1 automation(s) not scanned (per-id config endpoint returned 404" in reason
    assert "per-id fetch raised" not in reason


async def test_a_non_admin_token_names_the_admin_requirement(
    hass: HomeAssistant, read_only_client: HomeAssistantClient
) -> None:
    """#2546: the config views are admin-only, so a non-admin token's fetches
    are refused before they are sent, and the sample says so."""
    assert await async_setup_component(hass, "auth", {})
    await load_automations(
        hass, [automation(f"uid_{i}", f"Automation {i}") for i in range(3)]
    )

    result = await search_tools(read_only_client).deep_search(
        query="anything", search_types=["automation"], limit=10
    )

    reason = result["partial_reason"]
    assert (
        "3 automation(s) not scanned (per-id fetch raised a non-404 error; "
        "e.g. HomeAssistantAdminRequiredError: " in reason
    )
    assert "admin-only" in reason


@pytest.mark.parametrize("search_type", ["automation", "script", "scene"])
async def test_refused_admin_reads_add_the_non_admin_warning(
    hass: HomeAssistant, read_only_client: HomeAssistantClient, search_type: str
) -> None:
    """The warning keys on the client, not on the failure sample, which names
    only the first error."""
    assert await async_setup_component(hass, "auth", {})
    await load_automations(hass, [automation("uid_one", "Automation One")])
    await load_scripts(hass, {"script_one": script("Script One")})
    await load_scenes(hass, [scene("uid_scene", "Scene One")])

    result = await search_tools(read_only_client).deep_search(
        query="anything", search_types=[search_type], limit=10
    )

    assert NON_ADMIN_TOKEN_WARNING in result.get("warnings", [])


async def test_a_failed_helper_backend_on_a_refused_token_adds_the_warning(
    hass: HomeAssistant, read_only_client: HomeAssistantClient
) -> None:
    """Core here loads no ``input_*`` integration, so every helper list is an
    unknown command; the client already knows its token is not an admin's."""
    assert await async_setup_component(hass, "auth", {})
    assert await read_only_client.token_is_admin() is False

    result = await search_tools(read_only_client).deep_search(
        query="anything", search_types=["helper"], limit=10
    )

    assert "helper backend(s) not scanned" in result["partial_reason"]
    assert NON_ADMIN_TOKEN_WARNING in result.get("warnings", [])


async def test_an_admin_tokens_failed_fetch_adds_no_non_admin_warning(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await load_automations(hass, [], yaml_only=[automation("uid_one", "Secret")])
    await write_secret_reference(hass, AUTOMATIONS_FILE)

    result = await search_tools(core_client).deep_search(
        query="anything", search_types=["automation"], limit=10
    )

    assert "per-id fetch raised" in result["partial_reason"]
    assert NON_ADMIN_TOKEN_WARNING not in result.get("warnings", [])


async def test_a_refused_token_gets_no_warning_when_nothing_failed(
    hass: HomeAssistant, read_only_client: HomeAssistantClient
) -> None:
    """Once refused, the client stays refused; a later search that lost
    nothing must not carry the warning."""
    assert await async_setup_component(hass, "auth", {})
    await load_automations(hass, [automation("uid_one", "Automation One")])
    tools = search_tools(read_only_client)
    await tools.deep_search(query="anything", search_types=["automation"])
    assert read_only_client.admin_route_refused is True

    result = await tools.deep_search(query="anything", search_types=["script"])

    assert "partial" not in result
    assert NON_ADMIN_TOKEN_WARNING not in result.get("warnings", [])
