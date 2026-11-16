"""What a scene config search finds and how it says what it could not read.

``deep_search`` runs on the server's own client against a real ``hass``. Core
supplies every scene: a storage scene in ``scenes.yaml`` (the file the per-id
config endpoint reads), a YAML scene in ``configuration.yaml`` (registered under
platform ``homeassistant`` like a storage scene, yet a 404 on that endpoint) and
an integration-managed scene (a registry entry of another platform). The entity
registry maps each scene's entity-id slug to its storage key; a scene renamed in
the UI keeps its storage key, so without that map its config is never fetched.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import dump

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.smart_search import INDIVIDUAL_FETCH_BATCH_SIZE, SmartSearchTools

from ._command_faults import received, unanswered
from ._config_dir_yaml import (
    setup_from_configuration,
    setup_scenes,
    write_config_files,
)
from ._held_command import held_command

REGISTRY_LIST = "config/entity_registry/list"
REGISTRY_GET = "config/entity_registry/get"
LOG_HINT = "in the Home Assistant log"
# The per-id endpoint loads scenes.yaml and rejects a secret before it looks the
# id up, so every scene fetch answers HTTP 500.
UNREADABLE_SCENES = "- id: s0\n  name: S0\n  entities: {}\n  icon: !secret nope\n"
# Each fetch waits for Core's real answer, however long the machine takes: a
# fetch that runs out of time is reported as a timeout, not the answer a test
# sets up. The test that reports a timeout causes it itself.
NO_FETCH_TIMEOUT = 600.0


def _scene(scene_id: str, name: str) -> dict[str, Any]:
    return {"id": scene_id, "name": name, "entities": {"light.desk": "on"}}


@pytest.fixture
def search(core_client: HomeAssistantClient) -> SmartSearchTools:
    return SmartSearchTools(client=core_client, config_timeout=NO_FETCH_TIMEOUT)


async def _yaml_scenes_with_unreadable_store(
    hass: HomeAssistant, scenes: list[dict[str, Any]]
) -> None:
    """``scenes`` in configuration.yaml; scenes.yaml, which the per-id endpoint
    loads, holds a ``!secret`` it cannot resolve."""
    indented = "".join(f"  {line}\n" for line in dump(scenes).splitlines())
    await write_config_files(
        hass,
        {
            "configuration.yaml": f"scene:\n{indented}",
            "scenes.yaml": UNREADABLE_SCENES,
        },
    )
    await setup_from_configuration(hass, "scene")


def _integration_scene(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    object_id: str,
    unique_id: str,
    name: str,
) -> None:
    """A scene owned by another integration: registry entry plus state."""
    entry = entity_registry.async_get_or_create(
        "scene", "hue", unique_id, suggested_object_id=object_id
    )
    hass.states.async_set(entry.entity_id, "unknown", {"friendly_name": name})


def _clause(reason: str, marker: str) -> str:
    """The one ``partial_reason`` clause that contains ``marker``."""
    matching = [part for part in reason.split(" ; ") if marker in part]
    assert len(matching) == 1, reason
    return matching[0]


def _assert_states_incompleteness(clause: str) -> None:
    assert "not scanned" in clause
    assert "match status is unknown" in clause
    assert "not exhaustive" in clause


def _assert_informational(clause: str) -> None:
    assert "not scanned" not in clause
    assert "match status is unknown" not in clause
    assert "not exhaustive" not in clause


async def test_renamed_scene_config_is_found_under_its_new_entity_id(
    hass: HomeAssistant, search: SmartSearchTools
) -> None:
    """A scene renamed in the UI keeps its storage key; its config must still be searched and returned."""
    await setup_scenes(
        hass, [_scene("night_light_led_desk_strip", "LED Desk Strip Night Light")]
    )

    result = await search.deep_search("led desk strip", ["scene"], include_config=True)

    assert [m["entity_id"] for m in result["scenes"]] == [
        "scene.led_desk_strip_night_light"
    ]
    match = result["scenes"][0]
    assert match["scene_id"] == "night_light_led_desk_strip"
    assert match["config"]["entities"] == {"light.desk": "on"}


async def test_renamed_scene_still_resolves_when_the_registry_list_fails(
    hass: HomeAssistant,
    search: SmartSearchTools,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the registry list each scene is resolved on its own, so a renamed scene's config is still found."""
    await setup_scenes(
        hass, [_scene("night_light_led_desk_strip", "LED Desk Strip Night Light")]
    )

    with received(hass, REGISTRY_GET) as seen:
        async with unanswered(hass, REGISTRY_LIST, monkeypatch):
            result = await search.deep_search(
                "led desk strip", ["scene"], include_config=True
            )

    assert [m["entity_id"] for m in result["scenes"]] == [
        "scene.led_desk_strip_night_light"
    ]
    assert result["scenes"][0]["config"]["entities"] == {"light.desk": "on"}
    assert [msg["entity_id"] for msg in seen[REGISTRY_GET]] == [
        "scene.led_desk_strip_night_light"
    ]


async def test_scenes_are_not_looked_up_again_when_the_registry_list_worked(
    hass: HomeAssistant, search: SmartSearchTools
) -> None:
    """The registry list already resolved every scene; a lookup per scene would cost one round trip each (#2302)."""
    await setup_scenes(
        hass,
        [_scene("bedroom", "Bedroom"), _scene("kitchen", "Kitchen")],
        yaml_only=[_scene("movie_night_yaml", "Movie Night")],
    )

    with received(hass, REGISTRY_GET) as seen:
        result = await search.deep_search("bedroom", ["scene"])

    assert [m["entity_id"] for m in result["scenes"]] == ["scene.bedroom"]
    assert seen[REGISTRY_GET] == []


async def test_failed_registry_list_alone_does_not_mark_the_result_partial(
    hass: HomeAssistant,
    search: SmartSearchTools,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry list that fails while every scene config is still read loses nothing, so the result is complete."""
    await setup_scenes(hass, [_scene("movie_night", "Movie Night")])

    async with unanswered(hass, REGISTRY_LIST, monkeypatch):
        result = await search.deep_search("movie", ["scene"], include_config=True)

    assert result["scenes"][0]["config"] is not None
    assert "partial" not in result
    assert "partial_reason" not in result


async def test_search_that_read_every_scene_config_is_complete(
    hass: HomeAssistant, search: SmartSearchTools
) -> None:
    """A scene search that read every config is not marked partial."""
    await setup_scenes(
        hass, [_scene("bedroom", "Bedroom"), _scene("kitchen", "Kitchen")]
    )

    result = await search.deep_search("bedroom", ["scene"])

    assert [m["entity_id"] for m in result["scenes"]] == ["scene.bedroom"]
    assert "partial" not in result
    assert "partial_reason" not in result


async def test_failed_scene_fetches_are_named_with_their_error_and_the_log(
    hass: HomeAssistant, search: SmartSearchTools
) -> None:
    """Scene configs whose fetch fails are counted as unscanned, with one sample error and where it is logged (#1784)."""
    await _yaml_scenes_with_unreadable_store(
        hass, [_scene("bedroom", "Bedroom"), _scene("kitchen", "Kitchen")]
    )

    result = await search.deep_search("bedroom", ["scene"])

    assert result["partial"] is True
    failed = _clause(result["partial_reason"], "per-id fetch raised")
    assert failed.startswith(
        "2 scene(s) not scanned (per-id fetch raised; "
        "e.g. HTTP 500: 500 Internal Server Error)"
    )
    _assert_states_incompleteness(failed)
    assert LOG_HINT in failed


@pytest.fixture
async def movie_night_and_hue_scenes(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """One Home Assistant scene and two integration-managed scenes."""
    await setup_scenes(hass, [_scene("movie_night", "Movie Night")])
    _integration_scene(hass, entity_registry, "hue_relax", "hue-relax-1", "Hue Relax")
    _integration_scene(
        hass, entity_registry, "hue_concentrate", "hue-concentrate-1", "Hue Concentrate"
    )


@pytest.mark.usefixtures("movie_night_and_hue_scenes")
async def test_integration_scenes_are_not_fetched_or_counted_as_failures(
    core_client: HomeAssistantClient, search: SmartSearchTools
) -> None:
    """Integration-managed scenes have no config to fetch; trying would flag every Hue install partial (#1168)."""
    with patch.object(
        core_client, "get_scene_config", wraps=core_client.get_scene_config
    ) as fetch:
        result = await search.deep_search("hue", ["scene"])

    assert [call.args[0] for call in fetch.await_args_list] == ["movie_night"]
    assert {m["entity_id"] for m in result["scenes"]} == {
        "scene.hue_relax",
        "scene.hue_concentrate",
    }
    assert "partial" not in result


@pytest.mark.usefixtures("movie_night_and_hue_scenes")
async def test_integration_scene_result_names_its_registry_unique_id(
    search: SmartSearchTools,
) -> None:
    """An integration scene's ``scene_id`` is its registry key, not a guess from its entity id."""
    result = await search.deep_search("hue relax", ["scene"])

    assert [(m["entity_id"], m["scene_id"]) for m in result["scenes"]] == [
        ("scene.hue_relax", "hue-relax-1")
    ]


async def test_reason_tells_integration_scenes_apart_from_failed_fetches(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    search: SmartSearchTools,
) -> None:
    """With a failed fetch and integration scenes, each gets its own clause, and the integration one claims nothing unscanned (#1168)."""
    await _yaml_scenes_with_unreadable_store(
        hass, [_scene("movie_night", "Movie Night")]
    )
    _integration_scene(hass, entity_registry, "hue_relax", "hue-relax-1", "Hue Relax")
    _integration_scene(
        hass, entity_registry, "hue_concentrate", "hue-concentrate-1", "Hue Concentrate"
    )

    result = await search.deep_search("movie", ["scene"])

    assert result["partial"] is True
    reason = result["partial_reason"]
    failed = _clause(reason, "per-id fetch raised")
    integration = _clause(reason, "integration-managed scenes are scored")
    assert failed.startswith("1 scene(s) not scanned")
    _assert_states_incompleteness(failed)
    assert integration.startswith(
        "2 integration-managed scenes are scored by attribute only"
    )
    _assert_informational(integration)


@pytest.fixture
def dead_websocket(core_client: HomeAssistantClient) -> Any:
    """Every WebSocket command the client sends gets no answer."""
    return patch.object(
        core_client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantConnectionError("connection dropped")),
    )


async def test_registry_outage_is_logged_as_a_warning(
    hass: HomeAssistant,
    search: SmartSearchTools,
    dead_websocket: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A registry outage is logged at WARNING: a DEBUG line hid real outages from operators (#1168)."""
    await setup_scenes(hass, [_scene("bedroom", "Bedroom")])
    caplog.set_level(logging.WARNING, logger="ha_mcp.tools.smart_search")

    with dead_websocket:
        await search.deep_search("bedroom", ["scene"])

    assert any(
        record.levelno == logging.WARNING
        and "entity-registry augmentation failed" in record.message
        for record in caplog.records
    ), [(r.levelname, r.message) for r in caplog.records]


async def test_registry_outage_is_explained_in_the_partial_reason(
    hass: HomeAssistant, search: SmartSearchTools, dead_websocket: Any
) -> None:
    """When the registry is down, the reason says so, so the failed fetches are not read as a config outage (#1168)."""
    await setup_scenes(hass, [_scene("bedroom", "Bedroom")])

    with dead_websocket:
        result = await search.deep_search("bedroom", ["scene"])

    assert result["partial"] is True
    reason = result["partial_reason"]
    failed = _clause(reason, "per-id fetch raised")
    registry = _clause(reason, "Entity-registry fetch failed")
    _assert_states_incompleteness(failed)
    assert "integration-platform filter unavailable" in registry
    _assert_informational(registry)


async def test_registry_list_refused_by_core_is_reported_like_an_outage(
    hass: HomeAssistant, search: SmartSearchTools
) -> None:
    """A registry list Core answers with an error must not read as a complete search with no scene configs.

    Without Core's ``config`` integration the list is an unknown command, which
    the client returns as a non-success reply rather than raising.
    """
    assert await async_setup_component(
        hass, "scene", {"scene": [_scene("bedroom", "Bedroom")]}
    )

    result = await search.deep_search("bedroom", ["scene"])

    assert result["partial"] is True
    _clause(result["partial_reason"], "Entity-registry fetch failed")


async def test_yaml_scene_is_reported_as_a_structural_gap(
    hass: HomeAssistant, search: SmartSearchTools
) -> None:
    """A YAML scene with an ``id`` is a 404 on the per-id endpoint: named YAML-defined, not a failure, and still found by name (#2292).

    Its storage key differs from its entity-id slug, so the scan must hand the
    client the registry's answer; resolving the bare key as a slug would miss
    and report a vanished scene instead.
    """
    await setup_scenes(hass, [], yaml_only=[_scene("movie_night_yaml", "Movie Night")])

    result = await search.deep_search("movie", ["scene"], include_config=True)

    assert [(m["entity_id"], m["config"]) for m in result["scenes"]] == [
        ("scene.movie_night", None)
    ]
    assert result["partial"] is True
    reason = result["partial_reason"]
    yaml_defined = _clause(reason, "YAML-defined")
    assert yaml_defined.startswith(
        "1 scene(s) not scanned (per-id config endpoint returned 404"
    )
    _assert_states_incompleteness(yaml_defined)
    assert "per-id fetch raised" not in reason
    assert "Entity-registry fetch failed" not in reason


async def test_scene_that_vanished_during_the_search_is_a_failure_with_a_sample(
    hass: HomeAssistant,
    search: SmartSearchTools,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A scene gone between listing and fetch is a real gap, not a YAML scene (#2292).

    The registry list gets no answer, so the scene is resolved on its own; Core
    holds that lookup while the test removes the scene's state.
    """
    await setup_scenes(hass, [])
    hass.states.async_set("scene.ghost", "unknown", {"friendly_name": "Ghost"})

    async with (
        unanswered(hass, REGISTRY_LIST, monkeypatch),
        held_command(hass, REGISTRY_GET) as lookup,
    ):
        task = asyncio.create_task(search.deep_search("ghost", ["scene"]))
        async with asyncio.timeout(30):
            await lookup.arrived.wait()
        hass.states.async_remove("scene.ghost")
        lookup.release.set()
        result = await task

    assert result["partial"] is True
    reason = result["partial_reason"]
    failed = _clause(reason, "per-id fetch raised")
    assert failed.startswith(
        "1 scene(s) not scanned (per-id fetch raised; e.g. HTTP 404: Scene not found"
    )
    assert "YAML-defined" not in reason


async def test_timed_out_scene_fetch_points_at_the_concurrency_settings(
    hass: HomeAssistant, core_client: HomeAssistantClient, search: SmartSearchTools
) -> None:
    """A scene fetch with no answer in time is a timeout with its tuning settings, not a failure (#1784)."""
    await setup_scenes(hass, [_scene("bedroom", "Bedroom")])

    with patch.object(
        core_client, "get_scene_config", AsyncMock(side_effect=TimeoutError())
    ):
        result = await search.deep_search("bedroom", ["scene"])

    assert result["partial"] is True
    reason = result["partial_reason"]
    timed_out = _clause(reason, "per-id fetch timed out")
    assert timed_out.startswith("1 scene(s) not scanned")
    assert "HAMCP_INDIVIDUAL_FETCH_BATCH_SIZE" in timed_out
    assert "HAMCP_INDIVIDUAL_CONFIG_TIMEOUT" in timed_out
    _assert_states_incompleteness(timed_out)
    assert "per-id fetch raised" not in reason


async def test_exhausted_budget_names_the_unscanned_scenes_and_their_setting(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Scenes left when the time budget runs out are counted and point at the scene budget setting."""
    total = INDIVIDUAL_FETCH_BATCH_SIZE + 1
    await setup_scenes(hass, [_scene(f"s{i}", f"S{i}") for i in range(total)])
    # Each clock reading is one second after the last: 0 when the fetch
    # starts, 1 before the first batch, 2 before the second. A 1.5 s budget
    # runs out between the two, so exactly the second batch is left.
    ticks = itertools.count()
    search = SmartSearchTools(
        client=core_client,
        clock=lambda: float(next(ticks)),
        config_timeout=NO_FETCH_TIMEOUT,
    )

    result = await search.deep_search("s", ["scene"], config_time_budget=1.5)

    assert result["partial"] is True
    skipped = _clause(result["partial_reason"], "time budget exhausted")
    assert skipped.startswith(
        f"{total - INDIVIDUAL_FETCH_BATCH_SIZE} scene(s) not scanned"
    )
    _assert_states_incompleteness(skipped)
    assert "HAMCP_SCENE_CONFIG_TIME_BUDGET" in skipped
