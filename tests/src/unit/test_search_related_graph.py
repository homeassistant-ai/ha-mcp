"""Unit tests for the ``search/related`` reference-graph merge in deep_search.

Discussion #2258: on a large install the per-id config fetch is serialized by
Home Assistant (the endpoint takes a lock and re-parses the whole YAML per
request), so the wall-clock budget expires with a random slice of automations
never scanned. Worse, a YAML-defined automation returns 404 from
``/config/automation/config/<id>`` at ANY budget, so the legacy path can never
read it.

For an entity_id-shaped query, Home Assistant's own ``search/related`` command
answers "which automations/scripts/scenes reference this entity" from its
in-memory graph in one frame. These tests pin that the graph result is merged
into the config buckets so those two blind spots stop producing false negatives,
and that the merge never degrades what already worked.

``deep_search`` runs on the server's own client against a real ``hass``: the
automations, scripts and scenes are loaded by Core from YAML files in its
config directory, the graph is Core's ``search`` integration, and the per-id
config reads are Core's config views. Items in ``automations.yaml`` /
``scripts.yaml`` are readable there; items under a second ``manual`` key are
YAML-defined and answer 404.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config as conf_util
from homeassistant.components import search, websocket_api
from homeassistant.core import HomeAssistant, valid_entity_id
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.client.websocket_client import DEFAULT_COMMAND_WAIT_TIMEOUT
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config

QUERY = "climate.master_4"

STORED_AUTOMATIONS = "automations.yaml"
YAML_AUTOMATIONS = "automations_manual.yaml"
STORED_SCRIPTS = "scripts.yaml"
YAML_SCRIPTS = "scripts_manual.yaml"
YAML_SCENES = "scenes_manual.yaml"

_CONFIGURATION = (
    f"automation: !include {STORED_AUTOMATIONS}\n"
    f"automation manual: !include {YAML_AUTOMATIONS}\n"
    f"script: !include {STORED_SCRIPTS}\n"
    f"script manual: !include {YAML_SCRIPTS}\n"
    f"scene manual: !include {YAML_SCENES}\n"
)
_EMPTY: dict[str, Any] = {
    STORED_AUTOMATIONS: [],
    YAML_AUTOMATIONS: [],
    STORED_SCRIPTS: {},
    YAML_SCRIPTS: {},
    YAML_SCENES: [],
}

# The phrase only the scope sentence carries, and the one only the
# graph-unavailable disclosure carries.
SCOPE_CLAIM = "reference graph was consulted"
NOT_CONSULTED = "could not be consulted"


async def load_configuration(
    hass: HomeAssistant, files: dict[str, Any], *, graph: bool = True
) -> None:
    """Have Core load ``files`` (JSON is YAML) through configuration.yaml.

    ``graph`` sets up the ``search`` integration that answers ``search/related``.
    """
    contents = {"configuration.yaml": _CONFIGURATION, **_EMPTY, **files}
    for name, content in contents.items():
        text = content if isinstance(content, str) else json.dumps(content)
        await hass.async_add_executor_job(Path(hass.config.path(name)).write_text, text)
    config = await conf_util.async_hass_config_yaml(hass)
    domains = ["config", "automation", "script", "scene"] + (
        ["search"] if graph else []
    )
    for domain in domains:
        assert await async_setup_component(hass, domain, config)
    await hass.async_block_till_done()


def automation(alias: str, uid: str, target: str = QUERY) -> dict[str, Any]:
    """An automation whose action targets ``target`` (a plain reference)."""
    return {
        "id": uid,
        "alias": alias,
        "triggers": [{"trigger": "event", "event_type": "test"}],
        "actions": [{"action": "climate.turn_off", "target": {"entity_id": target}}],
    }


def templated_automation(alias: str, uid: str) -> dict[str, Any]:
    """An automation naming QUERY only inside a template, which the graph skips."""
    return {
        "id": uid,
        "alias": alias,
        "triggers": [{"trigger": "event", "event_type": "test"}],
        "conditions": [
            {
                "condition": "template",
                "value_template": "{{ states('" + QUERY + "') == 'heat' }}",
            }
        ],
        "actions": [],
    }


def script_sequence(target: str = QUERY) -> dict[str, Any]:
    return {
        "sequence": [{"action": "climate.turn_off", "target": {"entity_id": target}}]
    }


def clock_allowing(fetches: int) -> Callable[[], float]:
    """A budget clock that lets exactly ``fetches`` batches start.

    It reads 0s for the budget start and the first ``fetches`` batch checks,
    then a time past any budget.
    """
    readings = iter([0.0] * (fetches + 1))
    return lambda: next(readings, 1e9)


async def deep_search(
    client: HomeAssistantClient,
    *,
    clock: Callable[[], float] | None = None,
    query: str = QUERY,
    **kwargs: Any,
) -> dict[str, Any]:
    tools = SmartSearchTools(client=client, clock=clock)
    response: dict[str, Any] = await tools.deep_search(query=query, limit=20, **kwargs)
    return response


def bucket_ids(response: dict[str, Any], bucket: str) -> list[str]:
    return [r["entity_id"] for r in response.get(bucket, [])]


def record(response: dict[str, Any], bucket: str, entity_id: str) -> dict[str, Any]:
    (found,) = [r for r in response.get(bucket, []) if r["entity_id"] == entity_id]
    return found


def reason(response: dict[str, Any]) -> str:
    text: str = response.get("partial_reason") or ""
    return text


def graph_frames(sent: AsyncMock) -> int:
    """How many ``search/related`` commands the client sent."""
    return sum(
        1
        for call in sent.await_args_list
        if call.args and call.args[0].get("type") == "search/related"
    )


def graph_answers(monkeypatch: pytest.MonkeyPatch, payload: Any) -> None:
    """Core's search answers ``payload``: a shape this Core does not produce."""

    def async_search(
        self: search.Searcher, item_type: search.ItemType, item_id: str
    ) -> Any:
        return payload

    monkeypatch.setattr(search.Searcher, "async_search", async_search)


def socket_down(client: HomeAssistantClient) -> Any:
    """The WebSocket carries no frame while Core's REST API still answers."""
    return patch.object(
        client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantConnectionError("socket down")),
    )


# HA's slug rule: one valid id, then one value per way an id is malformed.
# A double underscore anywhere is invalid, object_id included (Core anchors the
# guard at the start of the whole id).
@pytest.mark.parametrize(
    "value",
    [
        "climate.master_4",
        "kitchen_temp",
        "binary_sensor.a__b",
        "climate._master",
        "climate.master_",
        "climate.master.4",
    ],
)
async def test_the_graph_is_asked_exactly_when_core_would_accept_the_entity_id(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock, value: str
) -> None:
    """A free-text term spends no frame; an entity id Core accepts gets one.

    ``search/related`` only accepts an item id, so a frame for a free-text
    term is a guaranteed-useless round trip on every ordinary search, and a
    gate stricter than Core's validator would skip entities Core references.
    """
    await load_configuration(hass, {})

    await deep_search(core_client, query=value, search_types=["automation"])

    assert graph_frames(sent) == int(valid_entity_id(value))


async def test_yaml_defined_automation_is_found_through_the_graph(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A referencing automation whose config cannot be read is still found.

    The per-id config endpoint only exposes UI-storage automations, so a
    YAML-defined one is invisible to the config-body search at any budget.
    Without the graph merge the user asks "which automations use
    climate.master_4" and gets an empty list that reads as "nothing uses it",
    the false negative that makes a rename look safe when it is not.
    """
    await load_configuration(
        hass,
        {
            YAML_AUTOMATIONS: [
                automation("Morning Routine", "uid_morning"),
                automation("Evening Lights", "uid_evening", target="light.porch"),
            ]
        },
    )

    response = await deep_search(core_client, search_types=["automation"])

    assert bucket_ids(response, "automations") == ["automation.morning_routine"]
    found = record(response, "automations", "automation.morning_routine")
    assert found["match_in_references"] is True
    assert found["match_in_config"] is False


async def test_graph_and_body_hit_collapse_into_one_record(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """An automation found both ways is one record carrying both flags.

    Reporting it twice would double-count it in total_matches and make an
    agent think two different automations reference the entity.
    """
    await load_configuration(
        hass, {STORED_AUTOMATIONS: [automation("Morning Routine", "uid_morning")]}
    )

    response = await deep_search(core_client, search_types=["automation"])

    assert bucket_ids(response, "automations") == ["automation.morning_routine"]
    found = record(response, "automations", "automation.morning_routine")
    assert found["match_in_references"] is True
    assert found["match_in_config"] is True


async def test_graph_failure_leaves_the_search_working(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A dead WebSocket must not break the body scan.

    The WebSocket can be down while Home Assistant's REST API is fine. The
    user must still get the config-body answer rather than an error.
    """
    await load_configuration(
        hass, {STORED_AUTOMATIONS: [automation("Morning Routine", "uid_morning")]}
    )

    with socket_down(core_client):
        response = await deep_search(core_client, search_types=["automation"])

    found = record(response, "automations", "automation.morning_routine")
    assert found["match_in_config"] is True
    assert found["match_in_references"] is False


async def test_configs_the_graph_confirmed_are_read_after_every_other_one(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under a budget, configs whose reference status is unknown are read first.

    HA's graph skips templated entity ids, so a config-body scan is the only
    thing that can find ``{{ states('climate.master_4') }}``. A flagged
    automation is already a confirmed match without its body, so reading it
    first would burn the budget learning nothing and lose the templated
    references outright -- #879's defect, re-created.
    """
    await load_configuration(
        hass,
        {
            STORED_AUTOMATIONS: [
                automation("Flagged", "uid_flagged"),
                templated_automation("Templated A", "uid_a"),
                templated_automation("Templated B", "uid_b"),
            ]
        },
    )
    monkeypatch.setattr(
        "ha_mcp.tools.smart_search._fetch.INDIVIDUAL_FETCH_BATCH_SIZE", 1
    )

    response = await deep_search(
        core_client,
        clock=clock_allowing(2),
        search_types=["automation"],
        config_time_budget=5,
    )

    for templated in ("automation.templated_a", "automation.templated_b"):
        assert record(response, "automations", templated)["match_in_config"] is True
    flagged = record(response, "automations", "automation.flagged")
    assert flagged["match_in_config"] is False, "the flagged config was read first"
    assert flagged["match_in_references"] is True


@pytest.fixture
async def unreadable_world(hass: HomeAssistant) -> None:
    """One YAML automation, so the body scan is always incomplete and the
    scope sentence is eligible to appear."""
    await load_configuration(
        hass, {YAML_AUTOMATIONS: [automation("Morning Routine", "uid_m")]}
    )


@pytest.mark.usefixtures("unreadable_world")
async def test_no_completeness_claim_when_the_graph_could_not_be_reached(
    core_client: HomeAssistantClient,
) -> None:
    """A dead graph must not leave the response claiming full coverage.

    Without this, a WebSocket outage silently downgrades the answer to a
    body-scan-only result while the prose still says every plain reference
    was reported.
    """
    with socket_down(core_client):
        response = await deep_search(core_client, search_types=["automation"])

    assert SCOPE_CLAIM not in reason(response)
    assert NOT_CONSULTED in reason(response)


@pytest.mark.usefixtures("unreadable_world")
@pytest.mark.parametrize(
    "payload",
    [
        {"frobnicate": ["x"]},
        {"automation": [{"entity_id": "automation.foo"}]},
    ],
    ids=["no-known-item-type", "unreadable-elements"],
)
async def test_an_answer_that_cannot_be_read_is_not_treated_as_no_references(
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
) -> None:
    """A reply whose shape we cannot parse must not become 'nothing matched'.

    An empty success and an unreadable answer are opposite evidence. If HA's
    payload shape ever changes (item types we do not know, or objects where
    ids were), collapsing the two would assert full coverage on the basis of
    zero understood bytes.
    """
    graph_answers(monkeypatch, payload)

    response = await deep_search(core_client, search_types=["automation"])

    assert NOT_CONSULTED in reason(response)
    assert SCOPE_CLAIM not in reason(response)


@pytest.mark.usefixtures("unreadable_world")
async def test_an_empty_graph_answer_licenses_a_scope_claim_bounded_by_visibility(
    core_client: HomeAssistantClient,
) -> None:
    """HA answering "nothing references this" is real evidence, unlike a failure.

    The mirror of the two tests above: if an empty answer were also treated as
    unusable, the scope sentence would never appear and the feature would tell
    the user nothing on the common clean case. The visibility scrub runs after
    the graph merge and can drop a record this sentence would otherwise say is
    counted, so the sentence must not vouch for concealed records.
    """
    response = await deep_search(
        core_client, query="climate.unreferenced", search_types=["automation"]
    )

    scope = reason(response)
    assert SCOPE_CLAIM in scope
    assert "visibility" in scope, (
        "the scope claim does not bound itself by what the caller can see"
    )


async def test_consumers_ha_reports_but_we_do_not_model_are_disclosed(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A group containing the entity breaks on rename and must be disclosed.

    ha_search has no group/person match shape, but discarding them AND
    claiming the reference list is complete turns a scoping decision into a
    false negative. Disclosed as a COUNT, never as ids: under enforce mode
    the outbound scan refuses any response carrying a hidden entity_id, so
    naming them would fail the whole search and leak what that mode exists
    to conceal.
    """
    await load_configuration(hass, {})
    assert await async_setup_component(
        hass, "group", {"group": {"downstairs": {"entities": [QUERY]}}}
    )
    await hass.async_block_till_done()

    response = await deep_search(core_client, search_types=["automation"])

    assert "1 group(s)" in reason(response)
    assert "group.downstairs" not in reason(response), (
        "a concealed-surface id was named in partial_reason"
    )


async def test_surfaces_excluded_by_search_types_are_disclosed(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """References on a surface the caller filtered out must still be named.

    The caller narrowed the search; they did not ask to be told the answer
    is exhaustive while known hits are withheld.
    """
    await load_configuration(
        hass, {YAML_SCENES: [{"name": "Movie Night", "entities": {QUERY: "heat"}}]}
    )

    response = await deep_search(core_client, search_types=["automation"])

    assert "scenes" in reason(response)


async def test_a_hidden_automation_named_by_the_graph_is_still_concealed(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enforce mode must strip a graph hit exactly as it strips a body hit.

    The graph names an automation without reading its config, so a graph
    record is built from data the config-body scrub never saw. If the merge
    ever moved after the scrub, these records would sail past the filter
    and hand the agent the entity_id of an automation the operator
    concealed -- the one thing enforce mode exists to prevent.
    """
    await load_configuration(
        hass,
        {
            YAML_AUTOMATIONS: [
                automation("Security Arm", "uid_sec"),
                automation("Morning Routine", "uid_m"),
            ]
        },
    )
    save_visibility_config(
        tmp_path,
        VisibilityConfig(
            enabled=True,
            enforce=True,
            exclude_categories=[],
            deny_entity_ids=["automation.security_arm"],
        ),
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

    response = await deep_search(core_client, search_types=["automation"])

    found = bucket_ids(response, "automations")
    assert "automation.security_arm" not in found, (
        "a concealed automation reached the caller through a graph hit"
    )
    assert "automation.morning_routine" in found


@pytest.fixture
async def renamed_yaml_scene(hass: HomeAssistant) -> str:
    """A YAML scene referencing QUERY whose storage key is not its slug (HA
    derives the entity_id from the name); returns its entity_id."""
    await load_configuration(
        hass,
        {
            YAML_SCENES: [
                {
                    "id": "night_light_led_desk_strip",
                    "name": "LED Desk Strip Night Light",
                    "entities": {QUERY: "heat"},
                }
            ]
        },
    )
    return "scene.led_desk_strip_night_light"


async def test_graph_scene_hit_carries_its_storage_key_not_its_slug(
    core_client: HomeAssistantClient, renamed_yaml_scene: str
) -> None:
    """A graph scene hit's scene_id must be the key its tools index on.

    For a renamed scene the slug is not the storage key. Handing the slug to
    ha_config_get_scene or ha_config_remove_scene finds the wrong scene or
    none at all, and a graph hit is exactly the case where no config body
    was read to correct it.
    """
    response = await deep_search(core_client, search_types=["scene"])

    found = record(response, "scenes", renamed_yaml_scene)
    assert (found["match_in_references"], found["match_in_config"]) == (True, False)
    assert found["scene_id"] == "night_light_led_desk_strip"


async def test_a_scene_only_search_with_unread_configs_still_scopes_the_unknown(
    core_client: HomeAssistantClient, renamed_yaml_scene: str
) -> None:
    """An incomplete scene scan must still say what the graph accounted for.

    The scope sentence was gated on automation and script counters only, so
    a scene-only search whose configs could not be read reported "scenes not
    scanned" with no statement that every plain reference was covered
    anyway. The caller is left assuming the reference list is as broken as
    the scan.
    """
    response = await deep_search(core_client, search_types=["scene"])

    assert SCOPE_CLAIM in reason(response), (
        f"scene incompleteness did not reach the scope gate: {reason(response)!r}"
    )


async def test_graph_script_hit_carries_its_script_id(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A script graph hit needs the id ha_config_get_script takes.

    Without it the agent has an entity_id and no way to open the script it
    was just told references the entity.
    """
    await load_configuration(hass, {YAML_SCRIPTS: {"good_night": script_sequence()}})

    response = await deep_search(core_client, search_types=["script"])

    found = record(response, "scripts", "script.good_night")
    assert (found["match_in_references"], found["match_in_config"]) == (True, False)
    assert found["script_id"] == "good_night"


async def test_graph_confirmed_scripts_are_read_last(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The script side keys deprioritization differently from automations.

    Scripts key on the entity slug, automations on a registry unique_id.
    A wrong key here does not raise: unmatched ids are simply never
    deprioritized, so the budget silently goes back to re-reading configs
    whose match is already settled.
    """
    templated = {
        "sequence": [
            {
                "condition": "template",
                "value_template": "{{ states('" + QUERY + "') == 'heat' }}",
            }
        ]
    }
    await load_configuration(
        hass,
        {STORED_SCRIPTS: {"flagged": script_sequence(), "templated": templated}},
    )
    monkeypatch.setattr(
        "ha_mcp.tools.smart_search._fetch.INDIVIDUAL_FETCH_BATCH_SIZE", 1
    )

    response = await deep_search(
        core_client,
        clock=clock_allowing(1),
        search_types=["script"],
        config_time_budget=5,
    )

    assert record(response, "scripts", "script.templated")["match_in_config"] is True
    assert record(response, "scripts", "script.flagged")["match_in_config"] is False


async def test_one_rejection_does_not_disable_the_graph(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """A single unknown_command may just mean HA is still starting.

    HA registers search/related during setup but accepts WebSocket clients
    before every integration has loaded, and the most safety-critical
    moment to consult the graph is right after a restart. Latching off the
    first rejection would silently skip the graph for the whole window.
    """
    await load_configuration(hass, {}, graph=False)

    for _ in range(2):
        await deep_search(core_client, search_types=["automation"])

    assert graph_frames(sent) == 2


async def test_repeated_rejection_stops_the_frames(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """Confirmed-unsupported installs must stop paying a rejected frame.

    Each rejection makes the transport log an ERROR, so retrying forever
    reproduces the log-spam regression of #1889.
    """
    await load_configuration(hass, {}, graph=False)

    for _ in range(3):
        await deep_search(core_client, search_types=["automation"])

    assert graph_frames(sent) == 2


async def test_a_success_clears_the_strike_count(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """One blip then a good answer must not accumulate toward suppression.

    Otherwise two unrelated hiccups hours apart (say, two restarts) would
    latch the graph off.
    """
    await load_configuration(hass, {}, graph=False)
    await deep_search(core_client, search_types=["automation"])
    assert await async_setup_component(hass, "search", {})
    await deep_search(core_client, search_types=["automation"])
    # The next restart, before search has loaded again.
    hass.data[websocket_api.DOMAIN].pop("search/related")

    for _ in range(2):
        await deep_search(core_client, search_types=["automation"])

    assert graph_frames(sent) == 4


async def test_an_error_that_is_not_unknown_command_never_arms_suppression(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two unrelated blips must not disable the graph for five minutes.

    Only a rejection meaning "this command does not exist" is evidence
    about the instance. Counting other failures toward the same threshold
    would silently drop dependency discovery after any two hiccups.
    """
    await load_configuration(hass, {})

    def async_search(
        self: search.Searcher, item_type: search.ItemType, item_id: str
    ) -> Any:
        raise HomeAssistantError("search index unavailable")

    monkeypatch.setattr(search.Searcher, "async_search", async_search)

    for _ in range(3):
        response = await deep_search(core_client, search_types=["automation"])
        assert NOT_CONSULTED in reason(response)

    assert graph_frames(sent) == 3


async def test_suppression_lifts_once_its_window_expires(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An install that gains the search integration must recover on its own.

    The cache is keyed by a client that outlives HA restarts and upgrades,
    so without expiry a suppression armed before an upgrade would outlast
    the condition that caused it.
    """
    await load_configuration(hass, {}, graph=False)
    for _ in range(3):
        await deep_search(core_client, search_types=["automation"])
    assert graph_frames(sent) == 2

    # The window has now run out.
    monkeypatch.setattr("ha_mcp.tools.smart_search._graph._UNSUPPORTED_TTL_S", 0)
    await deep_search(core_client, search_types=["automation"])

    assert graph_frames(sent) == 3


async def test_a_search_of_only_non_graph_surfaces_is_not_partial(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Skipping a frame the graph could not have answered is not a loss.

    The graph speaks only to automations, scripts and scenes, so a helper-
    or dashboard-only search never sends one. Reporting that as "the
    reference graph could not be consulted" marks a complete search
    ``partial``, which trains an agent to distrust a result that is in fact
    exhaustive -- and to re-run it, paying the whole scan again.
    """
    await load_configuration(hass, {})
    for helper in ("boolean", "number", "select", "text", "datetime", "button"):
        assert await async_setup_component(hass, f"input_{helper}", {})

    response = await deep_search(core_client, search_types=["helper"])

    assert NOT_CONSULTED not in reason(response)
    assert not response.get("partial"), (
        f"complete search reported partial: {reason(response)}"
    )


async def test_the_graph_lookup_waits_less_than_an_ordinary_command(
    hass: HomeAssistant, core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """An optional lookup must not stall the scan it exists to improve.

    It is awaited before any config fetch starts, so inheriting the client's
    command default would hold every entity_id search behind an
    unresponsive graph, potentially past the caller's own tool timeout.
    """
    await load_configuration(hass, {})

    await deep_search(core_client, search_types=["automation"])

    (frame,) = [
        call.args[0]
        for call in sent.await_args_list
        if call.args[0].get("type") == "search/related"
    ]
    assert frame["_wait_timeout"] < DEFAULT_COMMAND_WAIT_TIMEOUT
