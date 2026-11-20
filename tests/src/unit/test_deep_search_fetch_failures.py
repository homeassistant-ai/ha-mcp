"""What deep_search says when a per-id config fetch times out or fails.

A timeout points at the batch-size and timeout knobs, not at the entities
(#1784). A failure names one representative error, with a hint to read the
Home Assistant log when it is an HTTP 500 whose body names no cause (#1784's
follow-up). A scene that exists but has no stored config is a structural gap
(#2292). Each test runs deep_search on the server's own client against a real
``hass``; the timeouts come from a zero timeout, the failures from Core.
"""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantClient
from ha_mcp.tools.smart_search import (
    AUTOMATION_CONFIG_TIME_BUDGET,
    INDIVIDUAL_FETCH_BATCH_SIZE,
    record_first_failure,
    summarize_fetch_error,
)

from ._deep_search_core import (
    SCENES_FILE,
    SCRIPTS_FILE,
    automation,
    client_without_patience,
    load_automations,
    load_scenes,
    scene,
    search_tools,
    stepping_clock,
    write_config_file,
    write_secret_reference,
)

TIMEOUT_KNOBS = ("HAMCP_INDIVIDUAL_FETCH_BATCH_SIZE", "HAMCP_INDIVIDUAL_CONFIG_TIMEOUT")
HA_LOG_HINT = "`!secret` reference in the config file HA loads"


async def test_automation_fetches_past_the_timeout_point_at_the_knobs(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A per-id fetch that outlives the per-request timeout is a timeout, not
    "a non-404 error" that sends the user hunting for broken automations."""
    await load_automations(
        hass,
        [
            automation("uid_slow_one", "Slow One"),
            automation("uid_slow_two", "Slow Two"),
        ],
    )

    result = await search_tools(core_client, config_timeout=0).deep_search(
        query="anything", search_types=["automation"], limit=10
    )

    assert result["partial"] is True
    reason = result["partial_reason"]
    assert "2 automation(s) not scanned (per-id fetch timed out" in reason
    assert all(knob in reason for knob in TIMEOUT_KNOBS)
    assert "non-404" not in reason


async def test_scene_fetches_past_the_timeout_point_at_the_knobs(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await load_scenes(
        hass, [scene("uid_slow_one", "Slow One"), scene("uid_slow_two", "Slow Two")]
    )

    result = await search_tools(core_client, config_timeout=0).deep_search(
        query="anything", search_types=["scene"], limit=10
    )

    assert result["partial"] is True
    reason = result["partial_reason"]
    assert "2 scene(s) not scanned (per-id fetch timed out" in reason
    assert all(knob in reason for knob in TIMEOUT_KNOBS)
    assert "per-id fetch raised" not in reason


async def test_the_clients_own_timeout_is_reported_as_a_timeout(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """When the client's HTTP timeout fires before the per-request one, the
    timeout arrives wrapped in a connection error and still counts as one."""
    await load_automations(hass, [automation("uid_capped", "Capped")])
    states = await core_client.get_states()

    async with client_without_patience(core_client) as client:
        result = await search_tools(client).deep_search(
            query="anything",
            search_types=["automation"],
            limit=10,
            prefetched_states=states,
        )

    reason = result["partial_reason"]
    assert "1 automation(s) not scanned (per-id fetch timed out" in reason
    assert "per-id fetch raised" not in reason


async def test_a_refused_automation_fetch_is_a_failure_that_names_itself(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    unreachable_client: HomeAssistantClient,
) -> None:
    """The timeout carve-out must not swallow a real connectivity failure. The
    states were read before Home Assistant became unreachable."""
    await load_automations(hass, [automation("uid_down", "Down")])
    states = await core_client.get_states()

    result = await search_tools(unreachable_client).deep_search(
        query="anything",
        search_types=["automation"],
        limit=10,
        prefetched_states=states,
    )

    reason = result["partial_reason"]
    assert (
        "1 automation(s) not scanned (per-id fetch raised a non-404 error; "
        "e.g. HomeAssistantConnectionError: Failed to connect to Home Assistant"
        in reason
    )
    assert "timed out" not in reason


async def test_a_spent_budget_does_not_count_timed_out_ids_as_skipped(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The first batch times out and the budget is then spent: those ids are
    timeouts only, and the skipped count is the two batches never launched."""
    await load_automations(
        hass,
        [
            automation(f"uid_{i}", f"Automation {i}")
            for i in range(3 * INDIVIDUAL_FETCH_BATCH_SIZE)
        ],
    )
    clock = stepping_clock(0, 0, AUTOMATION_CONFIG_TIME_BUDGET + 1)

    result = await search_tools(core_client, clock=clock, config_timeout=0).deep_search(
        query="anything", search_types=["automation"], limit=10
    )

    reason = result["partial_reason"]
    assert (
        f"{INDIVIDUAL_FETCH_BATCH_SIZE} automation(s) not scanned "
        "(per-id fetch timed out" in reason
    )
    assert (
        f"{2 * INDIVIDUAL_FETCH_BATCH_SIZE} automation(s) not scanned "
        "(time budget exhausted)" in reason
    )


async def test_failing_scene_fetches_name_the_error_and_no_other_file(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The log hint names no file: a scene 500 has nothing to do with
    scripts.yaml or automations.yaml."""
    await load_scenes(
        hass,
        [],
        yaml_only=[scene("uid_one", "Broken One"), scene("uid_two", "Broken Two")],
    )
    await write_secret_reference(hass, SCENES_FILE)

    result = await search_tools(core_client).deep_search(
        query="anything", search_types=["scene"], limit=10
    )

    reason = result["partial_reason"]
    assert (
        "2 scene(s) not scanned (per-id fetch raised; "
        "e.g. HTTP 500: 500 Internal Server Error)" in reason
    )
    assert HA_LOG_HINT in reason
    assert "scripts.yaml" not in reason
    assert "automations.yaml" not in reason


async def test_a_yaml_defined_scene_is_a_structural_gap_not_a_failure(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """#2292: a YAML scene with an ``id`` registers like a stored one, then
    has no stored config. The caller is told where its definition lives, with
    no failure fragment and no 500 hint."""
    await load_scenes(hass, [], yaml_only=[scene("yaml_defined", "YAML Defined")])

    result = await search_tools(core_client).deep_search(
        query="anything", search_types=["scene"], limit=10
    )

    assert result["partial"] is True
    reason = result["partial_reason"]
    assert "1 scene(s) not scanned (per-id config endpoint returned 404" in reason
    assert "YAML-defined" in reason
    assert "per-id fetch raised" not in reason
    assert "in the Home Assistant log" not in reason


async def test_a_scene_gone_since_the_state_read_stays_a_failure(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A scene that is in neither the registry nor the state machine at fetch
    time vanished after the states were read: a real gap, not a structural
    one. The registry list the search was handed had failed, so every scene
    was attempted."""
    failed_registry = await core_client.send_websocket_message(
        {"type": "config/entity_registry/list"}
    )
    assert failed_registry["success"] is False
    hass.states.async_set("scene.vanished", "scening", {"friendly_name": "Vanished"})
    states = await core_client.get_states()
    hass.states.async_remove("scene.vanished")
    await write_config_file(hass, SCENES_FILE, "[]")

    result = await search_tools(core_client).deep_search(
        query="anything",
        search_types=["scene"],
        limit=10,
        prefetched_states=states,
        prefetched_registry=failed_registry,
    )

    reason = result["partial_reason"]
    assert (
        "1 scene(s) not scanned (per-id fetch raised; "
        "e.g. HTTP 404: Scene not found: vanished)" in reason
    )
    assert "per-id config endpoint returned 404" not in reason
    assert "in the Home Assistant log" not in reason


async def test_a_yaml_defined_scene_still_gets_the_graph_scope_sentence(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A scene whose config lives outside storage is a permanent gap; the
    caller must still be told what Home Assistant's reference graph covered."""
    await load_scenes(
        hass,
        [],
        yaml_only=[
            scene("movie_night", "Movie Night", {"climate.master_4": "heat"}),
        ],
    )
    assert await async_setup_component(hass, "search", {})

    result = await search_tools(core_client).deep_search(
        query="climate.master_4", search_types=["scene"], limit=20
    )

    reason = result["partial_reason"]
    assert "per-id config endpoint returned 404" in reason
    assert "reference graph was consulted" in reason


@pytest.fixture
async def http_500(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantAPIError:
    """The error the client raises for Core's 500 on a per-id script read."""
    await write_secret_reference(hass, SCRIPTS_FILE)
    with pytest.raises(HomeAssistantAPIError) as caught:
        await core_client.get_script_config("any_script")
    assert caught.value.status_code == 500
    return caught.value


@pytest.fixture
async def http_404(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantAPIError:
    """The error the client raises for Core's 404 on a missing script."""
    await write_config_file(hass, SCRIPTS_FILE, "{}")
    with pytest.raises(HomeAssistantAPIError) as caught:
        await core_client.get_script_config("missing_script")
    assert caught.value.status_code == 404
    return caught.value


def test_an_error_without_a_message_is_summarized_by_its_type() -> None:
    assert summarize_fetch_error(RuntimeError()) == "RuntimeError"


def test_a_long_error_is_cut_to_one_short_line() -> None:
    long_message = "x" * 500

    summary = summarize_fetch_error(RuntimeError(long_message))

    assert len(summary) < len(long_message)
    assert summary.startswith("RuntimeError: x")
    assert summary.endswith("…")


def test_the_first_failure_keeps_the_sample_when_none_is_a_500() -> None:
    errors: list[str] = []
    record_first_failure(errors, RuntimeError("first"))
    record_first_failure(errors, ValueError("second"))
    assert errors == ["RuntimeError: first"]


def test_a_500_takes_the_sample_from_a_faster_non_500_once(
    http_500: HomeAssistantAPIError,
) -> None:
    """Failure order under ``asyncio.gather`` follows latency, so a fast blip
    would otherwise hide the 500 and its log hint."""
    errors: list[str] = []
    record_first_failure(errors, RuntimeError("fast blip"))
    record_first_failure(errors, http_500)
    assert errors == [summarize_fetch_error(http_500)]
    assert errors[0].startswith("HTTP 500")

    record_first_failure(errors, RuntimeError("late blip"))
    assert errors == [summarize_fetch_error(http_500)]


def test_a_non_500_http_error_does_not_take_the_sample(
    http_404: HomeAssistantAPIError,
) -> None:
    errors: list[str] = []
    record_first_failure(errors, RuntimeError("fast blip"))
    record_first_failure(errors, http_404)
    assert errors == ["RuntimeError: fast blip"]
