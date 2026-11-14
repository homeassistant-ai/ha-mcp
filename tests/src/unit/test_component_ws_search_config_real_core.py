"""``ha_mcp_tools/search`` config and helper hits on a real ``hass``.

Automations, scenes, helpers and config entries are set up by Core, and
``secrets.yaml`` sits in Core's own config directory. The search runs through
Core's WebSocket API (``component_ws``).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools.websocket_api.constants import WS_SEARCH

from ._component_core import ws_result
from ._core_setup import set_up_domain as _set_up
from ._core_setup import write_secrets
from ._executor_spy import executor_jobs, jobs_from

_SECRET = "s3cr3tprobevaluexyz"


def _automation(
    alias: str, *, item_id: str | None = None, **data: Any
) -> dict[str, Any]:
    """An automation config; one without ``item_id`` is YAML-only to the search."""
    config: dict[str, Any] = {
        "alias": alias,
        "triggers": [{"trigger": "event", "event_type": "probe"}],
        "actions": [{"action": "notify.x", "data": data}],
    }
    if item_id is not None:
        config["id"] = item_id
    return config


async def _search(ws: MockHAClientWebSocket, **params: Any) -> Any:
    return await ws_result(ws, WS_SEARCH, **params)


def _kind(result: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [hit for hit in result["helpers"] if hit["kind"] == kind]


@pytest.fixture
async def yaml_and_storage_automations(hass: HomeAssistant) -> None:
    """``automation.package_auto`` has no id, ``automation.ui_auto`` has one."""
    await _set_up(
        hass,
        "automation",
        [
            _automation("Package Auto", message="YAMLSECRET"),
            _automation("UI Auto", item_id="uid-1"),
        ],
    )


async def test_a_yaml_body_is_never_sent_and_a_storage_body_is(
    component_ws: MockHAClientWebSocket, yaml_and_storage_automations: None
) -> None:
    result = await _search(component_ws, query="auto", include_config=True)

    by_source = {hit["source"]: hit for hit in result["automations"]}
    assert (by_source["yaml"]["config"], by_source["yaml"]["id"]) == (None, None)
    assert by_source["storage"]["config"]["id"] == "uid-1"
    assert "YAMLSECRET" not in json.dumps(result)


async def test_a_yaml_body_is_matched_but_withheld(
    component_ws: MockHAClientWebSocket, yaml_and_storage_automations: None
) -> None:
    result = await _search(component_ws, query="yamlsecret", include_config=True)

    (hit,) = [hit for hit in result["automations"] if hit["source"] == "yaml"]
    assert hit["match_in_config"] is True
    assert hit["config"] is None
    assert "YAMLSECRET" not in json.dumps(result)


async def test_a_storage_body_is_withheld_without_include_config(
    component_ws: MockHAClientWebSocket, yaml_and_storage_automations: None
) -> None:
    result = await _search(component_ws, query="auto")

    assert result["automations"]
    assert all(hit["config"] is None for hit in result["automations"])


async def test_a_scene_matches_by_name_and_never_sends_a_body(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _set_up(
        hass,
        "scene",
        [
            {
                "id": "scn-1",
                "name": "Movie Night",
                "icon": "mdi:movie",
                "entities": {"light.tv": "on", "media_player.lr": "playing"},
            }
        ],
    )

    result = await _search(component_ws, query="movie", include_config=True)

    (hit,) = result["scenes"]
    assert (hit["name"], hit["source"]) == ("Movie Night", "storage")
    assert hit["match_in_name"] is True
    assert hit["config"] is None


async def test_a_scene_matches_an_entity_it_sets(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """ "Which scenes touch light.porch" finds the scene by its entity ids."""
    await _set_up(
        hass,
        "scene",
        [{"id": "scn-2", "name": "Evening", "entities": {"light.porch": "on"}}],
    )

    result = await _search(component_ws, query="light.porch", include_config=True)

    (hit,) = result["scenes"]
    assert hit["match_in_config"] is True
    assert hit["config"] is None


async def test_a_scenes_runtime_states_are_neither_matched_nor_sent(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A scene holds Core ``State`` objects; stringified they were garbage in
    the match corpus and the reply."""
    await _set_up(
        hass,
        "scene",
        [
            {
                "id": "scn-1",
                "name": "Movie Night",
                "entities": {
                    "light.tv": {"state": "on", "effect": "scenegarbagevalue"}
                },
            }
        ],
    )

    by_value = await _search(
        component_ws, query="scenegarbagevalue", include_config=True
    )
    by_name = await _search(component_ws, query="movie", include_config=True)

    assert by_value["scenes"] == []
    assert by_name["scenes"]
    assert "scenegarbagevalue" not in json.dumps(by_name)


async def test_config_hits_share_one_pagination_window(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _set_up(
        hass,
        "automation",
        [_automation(f"Auto {i}", item_id=f"u{i}") for i in range(3)],
    )
    await _set_up(
        hass,
        "scene",
        [{"id": "sid0", "name": "Auto Scene", "entities": {"light.x": "on"}}],
    )

    result = await _search(component_ws, query="auto", limit=2)

    assert result["config_total_matches"] == 4
    assert result["config_has_more"] is True
    assert len(result["automations"]) + len(result["scenes"]) == 2


async def test_search_types_select_the_surfaces_searched(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    hass.states.async_set("light.k", "on", {"friendly_name": "Kitchen"})
    await _set_up(hass, "automation", [_automation("Kitchen Auto", item_id="uid")])

    entities = await _search(component_ws, query="kitchen", search_types=["entity"])
    automations = await _search(
        component_ws, query="kitchen", search_types=["automation"]
    )

    assert entities["entities"] and not entities["automations"]
    assert automations["automations"] and not automations["entities"]


async def test_a_config_name_substring_scores_100(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """Config name hits are binary 100/0 like the server's exact path, not the
    entity 80 tier."""
    await _set_up(hass, "automation", [_automation("Kitchen Auto", item_id="uid")])

    result = await _search(component_ws, query="kit", search_types=["automation"])

    (hit,) = result["automations"]
    assert (hit["score"], hit["match_in_name"]) == (100, True)


async def test_a_domain_that_is_not_set_up_is_counted_inaccessible(
    component_ws: MockHAClientWebSocket,
) -> None:
    result = await _search(component_ws, query="x", search_types=["automation"])

    assert result["automations"] == []
    assert result["diagnostics"]["config_components_inaccessible"] == 1


def _template_helper(hass: HomeAssistant, options: dict[str, Any]) -> None:
    MockConfigEntry(
        domain="template",
        title="Sun Sensor",
        options=options,
        data={"api_key": "DATA_SECRET_XYZ"},
        entry_id="e1",
    ).add_to_hass(hass)


async def test_flow_helper_options_are_searched_and_entry_data_never_sent(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    options = {"state": "{{ is_state('sun.sun', 'above_horizon') }}"}
    _template_helper(hass, options)

    result = await _search(component_ws, query="sun", include_config=True)
    by_option = await _search(component_ws, query="above_horizon")

    (hit,) = _kind(result, "flow")
    assert (hit["helper_type"], hit["entry_id"]) == ("template", "e1")
    assert hit["options"] == options
    assert "DATA_SECRET_XYZ" not in json.dumps(result)
    assert "api_key" not in json.dumps(result)
    assert _kind(by_option, "flow")


async def test_flow_helper_options_are_withheld_without_include_config(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    _template_helper(hass, {"state": "x"})

    result = await _search(component_ws, query="sun")

    (hit,) = _kind(result, "flow")
    assert hit["options"] is None


@pytest.mark.parametrize("exact", [True, False])
async def test_flow_helper_options_match_in_both_modes(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, exact: bool
) -> None:
    """Core keeps ``ConfigEntry.options`` as a MappingProxyType; a dict-only
    guard once dropped it and no template helper body was searchable (e2e
    test_deep_search_finds_ui_template_helper)."""
    marker = "deepsearchtemplatebody4471"
    _template_helper(
        hass, {"name": "Template", "state": f"{{{{ states('{marker}') }}}}"}
    )

    result = await _search(
        component_ws,
        query=marker,
        search_types=["helper"],
        exact=exact,
        include_config=True,
    )

    (hit,) = _kind(result, "flow")
    assert (hit["entry_id"], hit["helper_type"]) == ("e1", "template")
    assert hit["match_in_config"] is True
    assert marker in json.dumps(hit["options"])


async def test_a_collection_helper_is_found_by_its_name(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    await _set_up(hass, "input_boolean", {"guest_mode": {"name": "Guest Mode"}})

    result = await _search(component_ws, query="guest", search_types=["helper"])

    (hit,) = _kind(result, "collection")
    assert (hit["helper_type"], hit["object_id"]) == ("input_boolean", "guest_mode")
    assert hit["config"] is None


@pytest.fixture
async def house_mode(hass: HomeAssistant) -> None:
    """A YAML input_select whose option value is not in its name."""
    await _set_up(
        hass,
        "input_select",
        {
            "house_mode": {
                "name": "House Mode",
                "options": ["day", "deep_search_option_a", "night"],
            }
        },
    )


async def test_a_collection_helper_is_found_by_an_option_value(
    component_ws: MockHAClientWebSocket, house_mode: None
) -> None:
    """e2e test_deep_search_helper: the value is in the helper's config, not
    its name, like the legacy ``<type>/list`` body search."""
    result = await _search(
        component_ws,
        query="deep_search_option_a",
        search_types=["helper"],
        include_config=True,
    )

    (hit,) = _kind(result, "collection")
    assert hit["helper_type"] == "input_select"
    assert (hit["match_in_config"], hit["match_in_name"]) == (True, False)
    assert "deep_search_option_a" in json.dumps(hit["config"])


async def test_a_collection_helper_body_is_withheld_without_include_config(
    component_ws: MockHAClientWebSocket, house_mode: None
) -> None:
    result = await _search(
        component_ws, query="deep_search_option_a", search_types=["helper"]
    )

    (hit,) = _kind(result, "collection")
    assert hit["config"] is None


async def _stored_mode(hass: HomeAssistant, ws: MockHAClientWebSocket) -> Any:
    """A stored input_select ``Mode``, created the way the frontend does."""
    assert await async_setup_component(hass, "input_select", {})
    return await ws_result(ws, "input_select/create", name="Mode", options=["stored_a"])


async def test_a_stored_helper_is_searched_by_its_stored_config(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """#2901: options set by ``set_options`` are runtime state; search
    matches and sends the stored helper."""
    stored = await _stored_mode(hass, component_ws)
    await hass.services.async_call(
        "input_select",
        "set_options",
        {"entity_id": "input_select.mode", "options": ["runtime_x"]},
        blocking=True,
    )

    by_stored = await _search(
        component_ws, query="stored_a", search_types=["helper"], include_config=True
    )
    by_runtime = await _search(
        component_ws, query="runtime_x", search_types=["helper"], include_config=True
    )

    (hit,) = _kind(by_stored, "collection")
    assert hit["match_in_config"] is True
    assert hit["config"] == stored
    assert _kind(by_runtime, "collection") == []


async def test_a_yaml_helpers_config_is_searched(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
) -> None:
    """A YAML schedule's weekday blocks are only in the config its entity was
    built from, not in its state attributes."""
    await _set_up(
        hass,
        "schedule",
        {"work": {"name": "Work", "monday": [{"from": "07:07:07", "to": "09:00:00"}]}},
    )

    result = await _search(
        component_ws, query="07:07:07", search_types=["helper"], include_config=True
    )
    # Removing the entity cancels the timer it set for its next block.
    entity_registry.async_remove("schedule.work")
    await hass.async_block_till_done()

    assert [hit["entity_id"] for hit in result["helpers"]] == ["schedule.work"]
    assert result["helpers"][0]["match_in_config"] is True


async def test_an_unreadable_entity_registry_makes_the_helper_search_partial(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
) -> None:
    """A stored helper whose entity the registry cannot name falls back to its
    state; the search says it is partial instead of hiding the helper."""
    await _stored_mode(hass, component_ws)

    with patch.object(
        entity_registry,
        "async_get_entity_id",
        side_effect=RuntimeError("registry unavailable"),
    ):
        result = await _search(component_ws, query="mode", search_types=["helper"])

    assert result["partial"] is True
    assert [hit["entity_id"] for hit in result["helpers"]] == ["input_select.mode"]


@pytest.fixture
async def leaky_automation(hass: HomeAssistant) -> None:
    """A YAML automation whose body holds a resolved secret and a plain token."""
    await _set_up(
        hass,
        "automation",
        [_automation("Leaky Auto", api_password=_SECRET, message="normalbodytoken")],
    )


async def test_a_resolved_secret_never_matches_but_a_plain_token_does(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, leaky_automation: None
) -> None:
    """A match on a resolved ``!secret`` would make ``ha_search`` a probe
    oracle: query a suspected secret, confirm it through ``match_in_config``."""
    write_secrets(hass, f"api_password: {_SECRET}\n")

    by_secret = await _search(component_ws, query=_SECRET, search_types=["automation"])
    by_token = await _search(
        component_ws, query="normalbodytoken", search_types=["automation"]
    )

    assert by_secret["automations"] == []
    assert any(hit["match_in_config"] for hit in by_token["automations"])


async def test_a_resolved_secret_never_matches_in_fuzzy_mode(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, leaky_automation: None
) -> None:
    write_secrets(hass, f"api_password: {_SECRET}\n")

    result = await _search(
        component_ws, query=_SECRET, search_types=["automation"], exact=False
    )

    assert result["automations"] == []


async def test_a_flow_helper_option_holding_a_secret_never_matches(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    write_secrets(hass, f"api_password: {_SECRET}\n")
    _template_helper(hass, {"state": _SECRET, "name": "Sun Sensor"})

    result = await _search(component_ws, query=_SECRET, search_types=["helper"])

    assert _kind(result, "flow") == []


async def test_without_secrets_yaml_the_value_matches(
    component_ws: MockHAClientWebSocket, leaky_automation: None
) -> None:
    """The scrub is what blocks the match, and its absence blocks nothing."""
    result = await _search(component_ws, query=_SECRET, search_types=["automation"])

    assert result["automations"]


async def test_the_default_search_scrubs_secrets(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, leaky_automation: None
) -> None:
    """No ``search_types`` means every surface, the config ones included."""
    write_secrets(hass, f"api_password: {_SECRET}\n")

    result = await _search(component_ws, query=_SECRET)

    assert result["automations"] == []


async def test_an_entity_only_search_never_reads_secrets_yaml(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The scrub only applies to config and helper bodies (perf gate)."""
    write_secrets(hass, f"api_password: {_SECRET}\n")
    hass.states.async_set("light.k", "on", {"friendly_name": "Kitchen"})

    with executor_jobs(hass) as spy:
        result = await _search(component_ws, query="kitchen", search_types=["entity"])

    assert result["entities"]
    assert jobs_from(spy, "secrets") == 0


async def test_a_config_search_reads_secrets_yaml_off_the_event_loop(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, leaky_automation: None
) -> None:
    write_secrets(hass, f"api_password: {_SECRET}\n")

    with executor_jobs(hass) as spy:
        result = await _search(component_ws, query=_SECRET, search_types=["automation"])

    assert jobs_from(spy, "secrets") == 1
    assert result["automations"] == []
