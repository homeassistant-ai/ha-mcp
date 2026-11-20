"""What ``ha_search`` returns when it searches entities and configurations at once.

A query runs two surfaces, the entity registry and the configuration bodies,
and merges what each returns into one envelope: which surfaces run follows
from the inputs, the caller's own inputs are echoed as sent, every top-level
key is one the ``fields=`` description documents, the flat pagination keys page
whichever surface still has results, and an incomplete configuration search is
reported where agents read it, also under a ``fields=`` projection.

``ha_search`` runs on the server's own client against a real ``hass`` without
the component, so Core answers its capability probe with ``unknown_command``
and the search takes the legacy route. Core holds lights in a Kitchen area on a
Main floor and automations stored in ``automations.yaml`` that turn them on;
for the incomplete searches, one more automation is defined in YAML only, and
Core answers its per-id config with 404. The last test takes the component
route instead, with the component's commands registered on the same Core.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any, get_args
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.setup import async_setup_component
from pydantic.fields import FieldInfo

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_search import SearchTools

from ._command_faults import unanswered
from ._deep_search_core import automation, load_automations
from ._search_tool import search_tools_on
from ._tool_errors import rejected_call_body

QUERY = "kitchen"
SKIPPED_FOR_ENTITY_FILTERS = "config-body search skipped"
MIRRORED = "incomplete results: "
# Without these, each helper type's list is an unknown command and every
# search would be incomplete.
HELPER_DOMAINS = (
    "input_boolean",
    "input_number",
    "input_select",
    "input_text",
    "input_datetime",
    "input_button",
)


def _turn_on(entity_id: str) -> list[dict[str, Any]]:
    return [{"action": "light.turn_on", "target": {"entity_id": entity_id}}]


STORED = [
    automation("evening", "Evening", actions=_turn_on("light.kitchen")),
    automation("dusk", "Dusk", actions=_turn_on("light.porch_a")),
    automation("dawn", "Dawn", actions=_turn_on("light.porch_a")),
]


@pytest.fixture
async def world(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SearchTools:
    """``ha_search`` on Core holding the lights and sensors below; the
    automations ``STORED`` turns them on are added by ``search``.

    ``kitchen``: one light, one automation. ``porch``: two lights, two
    automations. ``porch_a``: one light, two automations. ``porch_b``: a light
    and a sensor, no automation.
    """
    kitchen = area_registry.async_create(
        "Kitchen", floor_id=floor_registry.async_create("Main").floor_id
    )
    for entity_id in (
        "light.kitchen",
        "light.porch_a",
        "light.porch_b",
        "sensor.porch_b_temp",
    ):
        domain, object_id = entity_id.split(".")
        entry = entity_registry.async_get_or_create(
            domain, "test", object_id, suggested_object_id=object_id
        )
        if object_id == QUERY:
            entity_registry.async_update_entity(entry.entity_id, area_id=kitchen.id)
        hass.states.async_set(entity_id, "on", {"friendly_name": object_id.title()})
    for domain in HELPER_DOMAINS:
        assert await async_setup_component(hass, domain, {})
    return search_tools_on(core_client, tmp_path, monkeypatch)


@pytest.fixture
async def search(hass: HomeAssistant, world: SearchTools) -> SearchTools:
    """Every automation stored, so every search reads every config."""
    await load_automations(hass, STORED)
    return world


@pytest.fixture
async def incomplete_search(hass: HomeAssistant, world: SearchTools) -> SearchTools:
    """One more automation, defined in YAML only: every config search that
    reaches it is incomplete."""
    await load_automations(
        hass, STORED, yaml_only=[automation("garage_door", "Garage door")]
    )
    return world


def _ids(records: list[dict[str, Any]]) -> list[str]:
    return [record["entity_id"] for record in records]


def _documented_keys() -> set[str]:
    """The top-level keys the ``fields=`` description lists as available."""
    annotation = inspect.signature(SearchTools.ha_search).parameters["fields"]
    (description,) = [
        meta.description
        for meta in get_args(annotation.annotation)[1:]
        if isinstance(meta, FieldInfo) and meta.description
    ]
    listed = re.search(r"Available keys: ([^.]+)\.", description)
    assert listed, description
    return {key.strip() for key in listed.group(1).split(",")}


@pytest.mark.parametrize(
    ("search_types", "named"),
    [
        ([], "non-empty"),
        (["automation", "frobnicate", "scene"], "['frobnicate']"),
        (["blueprint"], "['blueprint']"),
    ],
    ids=["empty", "unknown-among-valid", "blueprint"],
)
async def test_search_types_it_cannot_search_are_rejected(
    search: SearchTools, search_types: list[str], named: str
) -> None:
    """An unknown type used to return zero matches with no warning; an empty
    list searched the default types while the response echoed them. The error
    names only the unknown value, so the agent can fix just that one."""
    body = await rejected_call_body(
        search.ha_search(query=QUERY, search_types=search_types)
    )

    assert named in body["error"]["message"]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": "   "},
        {"state_filter": "   "},
        {"search_types": ["automation"]},
        {"state_filter": "on", "search_types": ["automation"]},
    ],
    ids=[
        "nothing",
        "blank-query",
        "blank-state",
        "types-only",
        "state-and-types",
    ],
)
async def test_call_with_nothing_to_search_is_rejected(
    search: SearchTools, arguments: dict[str, Any]
) -> None:
    """Without a term the configuration search has nothing to match, and an
    explicit ``search_types`` turns the entity listing off: rejected, not an
    empty result that reads as "nothing matched"."""
    body = await rejected_call_body(search.ha_search(**arguments))

    assert body["error"]["code"].startswith("VALIDATION")


@pytest.mark.parametrize(
    ("arguments", "entities_searched", "configs_searched", "warned"),
    [
        ({"query": QUERY}, True, True, False),
        ({"query": QUERY, "domain_filter": "light"}, True, False, True),
        ({"query": QUERY, "area_filter": "Kitchen"}, True, False, True),
        ({"query": QUERY, "state_filter": "on"}, True, False, True),
        ({"query": QUERY, "search_types": ["automation"]}, False, True, False),
        (
            {"query": QUERY, "domain_filter": "light", "search_types": ["automation"]},
            False,
            True,
            False,
        ),
        ({"domain_filter": "light"}, True, False, False),
        ({"area_filter": "Kitchen"}, True, False, False),
        ({"state_filter": "on"}, True, False, False),
        (
            {"domain_filter": "light", "area_filter": "Kitchen", "state_filter": "on"},
            True,
            False,
            False,
        ),
    ],
    ids=[
        "query",
        "query-domain",
        "query-area",
        "query-state",
        "query-types",
        "query-domain-types",
        "domain",
        "area",
        "state",
        "filters",
    ],
)
async def test_surfaces_searched_follow_the_inputs(
    search: SearchTools,
    arguments: dict[str, Any],
    entities_searched: bool,
    configs_searched: bool,
    warned: bool,
) -> None:
    """An entity filter beside a query means an entity lookup, so the costly
    configuration search is skipped, and one warning says how to get it back;
    an explicit ``search_types`` searches configurations only. Without a query
    there is no term to skip for, so no warning."""
    response = await search.ha_search(**arguments)

    assert ("light.kitchen" in _ids(response["entities"])) is entities_searched
    assert ("automation.evening" in _ids(response["automations"])) is configs_searched
    skip_warnings = [
        w
        for w in response.get("warnings", [])
        if w.startswith(SKIPPED_FOR_ENTITY_FILTERS)
    ]
    assert len(skip_warnings) == (1 if warned else 0)


async def test_the_query_is_echoed_as_the_caller_sent_it(search: SearchTools) -> None:
    """The entity surface echoes the query it searched for, stripped; that echo
    must not replace the caller's own value at the top of the response."""
    response = await search.ha_search(query=f"  {QUERY}  ")

    assert response["query"] == f"  {QUERY}  "
    assert _ids(response["entities"]) == ["light.kitchen"]


async def test_warnings_of_the_search_and_of_the_entity_surface_are_both_kept(
    search: SearchTools,
) -> None:
    """The skipped-configurations warning and the entity surface's own floor
    warning both reach the caller; neither replaces the other."""
    response = await search.ha_search(query=QUERY, area_filter="Main")

    assert _ids(response["entities"]) == ["light.kitchen"]
    warnings = response["warnings"]
    assert sum(w.startswith(SKIPPED_FOR_ENTITY_FILTERS) for w in warnings) == 1
    assert any("expanded to 1 area(s)" in w for w in warnings), warnings


@pytest.mark.parametrize(
    ("arguments", "surfaced"),
    [
        ({"query": QUERY}, {"search_type"}),
        ({"domain_filter": "light"}, {"domain_filter", "search_type"}),
        ({"area_filter": "Kitchen"}, {"area_filter", "area_names"}),
        ({"state_filter": "on"}, {"search_type"}),
        ({"query": QUERY, "area_filter": "Main"}, {"area_filter"}),
        (
            {
                "query": QUERY,
                "state_filter": "on",
                "exact_match": False,
                "group_by_domain": True,
            },
            {"state_filter_note", "by_domain"},
        ),
    ],
    ids=["query", "domain", "area", "state", "query-floor", "fuzzy-state-grouped"],
)
async def test_every_top_level_key_is_documented_and_entity_context_is_kept(
    search: SearchTools, arguments: dict[str, Any], surfaced: set[str]
) -> None:
    """A key the ``fields=`` description does not list is one an agent cannot
    know to project, so the entity surface's own bookkeeping stays out of the
    envelope; the context callers read back (the mode, the filters, the areas
    searched, the grouping) reaches it."""
    response = await search.ha_search(**arguments)

    assert set(response) <= _documented_keys(), set(response) - _documented_keys()
    assert surfaced <= set(response)


@pytest.mark.parametrize(
    ("query", "entities_more", "configs_more"),
    [
        (QUERY, False, False),
        ("porch_b", True, False),
        ("porch_a", False, True),
        ("porch", True, True),
    ],
    ids=["neither", "entities", "configs", "both"],
)
async def test_flat_pagination_pages_whichever_surface_has_more(
    search: SearchTools, query: str, entities_more: bool, configs_more: bool
) -> None:
    """Both surfaces page with the caller's offset and limit, so iterating
    ``offset = next_offset`` until ``has_more`` is false reaches the end of
    each."""
    response = await search.ha_search(query=query, limit=1)

    assert response["entity_has_more"] is entities_more
    assert response["config_has_more"] is configs_more
    assert response["has_more"] is (entities_more or configs_more)
    assert response["next_offset"] == (1 if entities_more or configs_more else None)


async def test_incomplete_config_search_is_mirrored_into_warnings(
    incomplete_search: SearchTools,
) -> None:
    """Agents read ``warnings`` but drop ``partial``: the reason must be there too."""
    response = await incomplete_search.ha_search(query=QUERY)

    assert response["partial"] is True
    assert "YAML-defined" in response["partial_reason"]
    assert response["warnings"].count(MIRRORED + response["partial_reason"]) == 1


async def test_complete_search_carries_no_incompleteness_warning(
    search: SearchTools,
) -> None:
    response = await search.ha_search(query=QUERY)

    assert response["partial"] is False, response.get("partial_reason")
    assert not any(w.startswith(MIRRORED) for w in response.get("warnings", []))


async def test_fields_projection_never_hides_incompleteness_or_paging(
    incomplete_search: SearchTools,
) -> None:
    """``fields=["entities"]`` narrows the buckets; the partial state, the
    totals, the paging and the echoes stay."""
    full = await incomplete_search.ha_search(query=QUERY)

    projected = await incomplete_search.ha_search(query=QUERY, fields=["entities"])

    assert projected["entities"] == full["entities"]
    for bucket in ("automations", "scripts", "scenes", "helpers"):
        assert bucket not in projected
    for key in (
        "success",
        "query",
        "search_types",
        "warnings",
        "errors",
        "partial",
        "partial_reason",
        "entity_total_matches",
        "config_total_matches",
        "count",
        "offset",
        "limit",
        "has_more",
        "next_offset",
        "entity_has_more",
        "config_has_more",
        "search_type",
    ):
        assert projected[key] == full[key], key


async def test_fields_naming_an_absent_key_says_so(search: SearchTools) -> None:
    """A typo in ``fields=`` gets a warning naming it, not a quietly empty response."""
    response = await search.ha_search(query=QUERY, fields=["entities", "entitis"])

    assert "entitis" not in response
    assert any("'entitis'" in w and "not found" in w for w in response["warnings"])


async def test_scenes_of_integrations_alone_are_not_fetched_or_reported_missing(
    hass: HomeAssistant,
    search: SearchTools,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """With no Home Assistant scene at all, every scene belongs to an
    integration and has no config to fetch: trying fetched 106 of 106 in the
    wild, each a 404 that marked the search incomplete."""
    for object_id in ("hue_relax", "hue_read"):
        entry = entity_registry.async_get_or_create(
            "scene", "hue", f"{object_id}-1", suggested_object_id=object_id
        )
        hass.states.async_set(entry.entity_id, "unknown", {"friendly_name": object_id})

    with patch.object(
        core_client, "get_scene_config", wraps=core_client.get_scene_config
    ) as fetch:
        response = await search.ha_search(query="hue", search_types=["scene"])

    assert sorted(_ids(response["scenes"])) == ["scene.hue_read", "scene.hue_relax"]
    fetch.assert_not_awaited()
    assert response["partial"] is False


async def test_failed_dashboards_leg_is_added_after_the_components_own_reason(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The component serves automations and reports the config domain it could
    not read (automation is never set up); the dashboards leg gets no answer
    from Core for the dashboard list. Both causes reach the caller: the leg's
    failure is added to the component's reason, not written over it."""
    assert await async_setup_component(hass, "lovelace", {})
    search = search_tools_on(component_core_client, tmp_path, monkeypatch)

    async with unanswered(
        hass, "lovelace/dashboards/list", monkeypatch, "ha_mcp_tools/dashboards"
    ):
        response = await search.ha_search(
            query=QUERY, search_types=["automation", "dashboard"]
        )

    assert response["partial"] is True, response
    component_part, dashboards_part = response["partial_reason"].split(" ; ")
    assert component_part.startswith("config components inaccessible"), component_part
    assert dashboards_part.startswith("dashboards: "), dashboards_part
    assert [error["surface"] for error in response["errors"]] == ["dashboards"]
