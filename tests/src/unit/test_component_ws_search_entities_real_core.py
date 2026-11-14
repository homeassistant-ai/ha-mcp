"""``ha_mcp_tools/search`` entity hits on a real ``hass``: joins, filters, scoring.

States, registry entries, areas, floors, labels and devices are Core's own; the
search runs through Core's WebSocket API (``component_ws``). The parity tests
compare the component with the server's own ``ha_search`` on the same ``hass``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import MockHAClientWebSocket, WebSocketGenerator

from custom_components.ha_mcp_tools import websocket_api
from custom_components.ha_mcp_tools.websocket_api.constants import (
    FUZZY_THRESHOLD,
    HIDDEN_SCORE_PENALTY,
    WS_SEARCH,
)
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.utils.fuzzy_search import FuzzyEntitySearcher, calculate_ratio

from ._component_core import ws_result
from ._search_tool import _build_ha_search, _setup_visibility_disabled


def _register(
    entity_registry: er.EntityRegistry, entity_id: str, name: str, **kwargs: Any
) -> er.RegistryEntry:
    """A registry entry for ``entity_id`` named ``name``, with a state to match."""
    domain, object_id = entity_id.split(".")
    return entity_registry.async_get_or_create(
        domain,
        "test",
        object_id,
        suggested_object_id=object_id,
        original_name=name,
        **kwargs,
    )


async def _entities(ws: MockHAClientWebSocket, **params: Any) -> list[dict[str, Any]]:
    result = await ws_result(ws, WS_SEARCH, search_types=["entity"], **params)
    entities: list[dict[str, Any]] = result["entities"]
    return entities


async def test_search_joins_and_matches_every_registry_dimension(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
    label_registry: lr.LabelRegistry,
) -> None:
    """An alias, the area, its floor, a label, the device maker and the name
    each find the lamp, and the hit carries the joined names."""
    upstairs = floor_registry.async_create("Upstairs")
    office = area_registry.async_create("Office", floor_id=upstairs.floor_id)
    garage = area_registry.async_create("Garage")
    favorites = label_registry.async_create("Favorites")
    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "lamp")},
        name="Lamp Device",
        manufacturer="Acme",
        model="X1",
    )
    device_registry.async_update_device(device.id, area_id=garage.id)
    lamp = _register(entity_registry, "light.lamp", "Desk Lamp", device_id=device.id)
    entity_registry.async_update_entity(
        lamp.entity_id,
        aliases=["reading light"],
        area_id=office.id,
        labels={favorites.label_id},
    )
    hass.states.async_set("light.lamp", "on", {"friendly_name": "Desk Lamp"})

    (hit,) = await _entities(component_ws, query="reading light")

    assert (hit["entity_id"], hit["score"]) == ("light.lamp", 100)
    assert (hit["area"], hit["floor"]) == ("Office", "Upstairs")
    assert "reading light" in hit["aliases"]
    assert "Favorites" in hit["labels"]
    for query in ("office", "upstairs", "favorites", "acme", "desk lamp"):
        hits = await _entities(component_ws, query=query)
        assert hits, f"expected a hit for {query!r}"
        assert hits[0]["entity_id"] == "light.lamp"


async def test_the_computed_name_alias_is_neither_listed_nor_matched(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
) -> None:
    """Core's aliases can hold the COMPUTED_NAME sentinel ("the computed name
    is an alias"). Stringified, it showed as a ``ComputedNameType._singleton``
    alias on every carrying entity and was a scored match text."""
    lamp = _register(entity_registry, "light.lamp", "Desk Lamp")
    entity_registry.async_update_entity(
        lamp.entity_id, aliases=[er.COMPUTED_NAME, "reading light"]
    )
    hass.states.async_set("light.lamp", "on", {"friendly_name": "Desk Lamp"})

    (hit,) = await _entities(component_ws, query="reading light")

    assert hit["aliases"] == ["reading light"]
    assert await _entities(component_ws, query="singleton") == []


async def test_domain_filter_keeps_only_that_domain(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen"})
    hass.states.async_set("switch.kitchen", "on", {"friendly_name": "Kitchen"})

    hits = await _entities(component_ws, query="kitchen", domain_filter="light")

    assert {hit["entity_id"] for hit in hits} == {"light.kitchen"}


async def test_state_filter_compares_without_case(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """An input_select holding "Vacation" matches ``state_filter="vacation"``."""
    hass.states.async_set("light.a", "on", {"friendly_name": "Lamp A"})
    hass.states.async_set("light.b", "off", {"friendly_name": "Lamp B"})
    hass.states.async_set("light.c", "Vacation", {"friendly_name": "Lamp C"})

    off = await _entities(component_ws, query="lamp", state_filter="off")
    vacation = await _entities(component_ws, query="lamp", state_filter="vacation")

    assert [hit["entity_id"] for hit in off] == ["light.b"]
    assert [hit["entity_id"] for hit in vacation] == ["light.c"]


async def test_area_filter_accepts_the_area_name_or_id(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    area_registry: ar.AreaRegistry,
) -> None:
    office = area_registry.async_create("Office")
    lamp = _register(entity_registry, "light.lamp", "Lamp")
    entity_registry.async_update_entity(lamp.entity_id, area_id=office.id)
    hass.states.async_set("light.lamp", "on", {"friendly_name": "Lamp"})

    assert await _entities(component_ws, query="lamp", area_filter="Office")
    assert await _entities(component_ws, query="lamp", area_filter=office.id)
    assert not await _entities(component_ws, query="lamp", area_filter="Garage")


async def test_a_hidden_entity_ranks_lower_or_is_left_out(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
) -> None:
    _register(
        entity_registry, "light.h", "Hidden One", hidden_by=er.RegistryEntryHider.USER
    )
    hass.states.async_set("light.v", "on", {"friendly_name": "Visible"})
    hass.states.async_set("light.h", "on", {"friendly_name": "Hidden One"})

    shown = await _entities(component_ws, query="light", include_hidden=True)
    left = await _entities(component_ws, query="light", include_hidden=False)

    scores = {hit["entity_id"]: hit["score"] for hit in shown}
    assert scores == {"light.v": 80, "light.h": 80 - HIDDEN_SCORE_PENALTY}
    assert [hit["entity_id"] for hit in left] == ["light.v"]


async def test_entity_hits_page_with_their_own_total(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    for i in range(5):
        hass.states.async_set(f"light.l{i}", "on", {"friendly_name": f"Lamp {i}"})

    first = await ws_result(
        component_ws, WS_SEARCH, search_types=["entity"], query="lamp", limit=2
    )
    last = await ws_result(
        component_ws,
        WS_SEARCH,
        search_types=["entity"],
        query="lamp",
        limit=2,
        offset=4,
    )

    assert (len(first["entities"]), first["entity_total_matches"]) == (2, 5)
    assert first["entity_has_more"] is True
    assert len(last["entities"]) == 1
    assert last["entity_has_more"] is False


async def test_underscore_and_space_queries_find_the_same_entities(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The server's BM25 tokenizes both sides, so ``input_boolean`` and
    ``input boolean`` are one fuzzy query (e2e
    test_fuzzy_search_underscore_space_equivalence); exact mode keeps raw
    substring semantics, where the spaced form matches nothing."""
    hass.states.async_set(
        "input_boolean.guests", "off", {"friendly_name": "We Have Guests"}
    )
    hass.states.async_set("input_boolean.dark_mode", "on", {"friendly_name": "Dark"})
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen"})

    underscore = await _entities(component_ws, query="input_boolean", exact=False)
    space = await _entities(component_ws, query="input boolean", exact=False)
    exact_space = await _entities(component_ws, query="input boolean", exact=True)

    ids = {hit["entity_id"] for hit in underscore}
    assert ids == {"input_boolean.guests", "input_boolean.dark_mode"}
    assert {hit["entity_id"] for hit in space} == ids
    assert exact_space == []


async def test_an_empty_query_matches_every_entity(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    hass.states.async_set("light.a", "on", {"friendly_name": "A"})
    hass.states.async_set("light.b", "on", {"friendly_name": "B"})

    result = await ws_result(component_ws, WS_SEARCH, search_types=["entity"])

    assert result["entity_total_matches"] == 2
    assert {(hit["match_type"], hit["score"]) for hit in result["entities"]} == {
        ("match_all", 100)
    }


_PARITY_CORPUS = [
    ("light.kitchen", "Kitchen Light"),
    ("light.kitchen_ceiling", "Kitchen Ceiling"),
    ("switch.kitchen", "Kitchen Switch"),
    ("sensor.temperature", "Temperature"),
    ("light.bedroom", "Bedroom Light"),
]
_PARITY_HIDDEN = {"light.bedroom"}
_PARITY_QUERIES = [
    "kitchen",
    "light.kitchen",
    "temperature",
    "bedroom",
    "kitchen light",
]


async def test_exact_ranking_matches_the_servers_own_search(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_ws_client: WebSocketGenerator,
    entity_registry: er.EntityRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same ids and scores, in the same order, as ``ha_search``'s exact
    path on an install without the component, hidden entity included."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    assert await async_setup_component(hass, "config", {})
    for entity_id, name in _PARITY_CORPUS:
        hidden = er.RegistryEntryHider.USER if entity_id in _PARITY_HIDDEN else None
        _register(entity_registry, entity_id, name, hidden_by=hidden)
        hass.states.async_set(entity_id, "on", {"friendly_name": name})
    search = _build_ha_search(core_client)
    server: dict[str, list[tuple[str, int]]] = {}
    for query in _PARITY_QUERIES:
        result = await search(query=query, exact_match=True, limit=50)
        server[query] = [(hit["entity_id"], hit["score"]) for hit in result["entities"]]

    websocket_api.async_register_commands(hass)
    ws = await hass_ws_client(hass)
    for query in _PARITY_QUERIES:
        hits = await _entities(ws, query=query, exact=True, limit=50)
        component = [(hit["entity_id"], hit["score"]) for hit in hits]
        assert component == server[query], f"parity broke for query={query!r}"


async def test_fuzzy_tiers_follow_the_servers_ratio(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """Exact token 100, substring 80, a typo the server's ``calculate_ratio``
    value, and nothing below the threshold or for a typo in exact mode."""
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen"})

    async def score(query: str, *, exact: bool = False) -> int | None:
        hits = await _entities(component_ws, query=query, exact=exact)
        return hits[0]["score"] if hits else None

    typo = await score("kitchne")
    assert await score("kitchen") == 100
    assert await score("kit") == 80
    assert typo == calculate_ratio("kitchne", "kitchen")
    assert typo is not None and typo >= FUZZY_THRESHOLD
    assert await score("zzzzzzzz") is None
    assert await score("kitchne", exact=True) is None


async def _match_type(
    hass: HomeAssistant,
    ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    query: str,
    *,
    exact: bool,
    alias: str | None = None,
) -> str:
    """The match_type the component gives ``light.kitchen`` for ``query``."""
    entry = _register(entity_registry, "light.kitchen", "Kitchen Light")
    if alias is not None:
        entity_registry.async_update_entity(entry.entity_id, aliases=[alias])
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen Light"})
    hits = await _entities(ws, query=query, exact=exact)
    assert hits, f"expected a hit for {query!r}"
    match_type: str = hits[0]["match_type"]
    return match_type


async def test_an_alias_driven_fuzzy_hit_is_labeled_alias_match(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
) -> None:
    """e2e test_search_finds_entity_by_alias_issue_1170: a fuzzy query equal to
    an alias the id and name don't carry."""
    match_type = await _match_type(
        hass,
        component_ws,
        entity_registry,
        "e2e1170aliasabcd",
        exact=False,
        alias="e2e1170aliasabcd",
    )

    assert match_type == "alias_match"


async def test_exact_mode_labels_every_hit_exact_match(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
) -> None:
    match_type = await _match_type(
        hass, component_ws, entity_registry, "kitchen", exact=True
    )

    assert match_type == "exact_match"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("light.kitchen", "exact_id"),
        ("kitchen light", "exact_name"),
        ("light", "exact_domain"),
        ("kitch", "partial_id"),
        ("chen ligh", "partial_name"),
    ],
)
async def test_fuzzy_hits_use_the_servers_match_type_tiers(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    query: str,
    expected: str,
) -> None:
    match_type = await _match_type(
        hass, component_ws, entity_registry, query, exact=False
    )

    assert match_type == expected


async def test_alias_match_agrees_with_the_servers_fuzzy_engine(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
) -> None:
    """Both label the alias hit ``alias_match``, so the taxonomies cannot drift."""
    alias = "e2e1170aliasabcd"
    component = await _match_type(
        hass, component_ws, entity_registry, alias, exact=False, alias=alias
    )
    state = hass.states.get("light.kitchen")
    assert state is not None

    matches, _ = FuzzyEntitySearcher().search_entities(
        [{**state.as_dict(), "_aliases": [alias]}], alias
    )

    server = next(m["match_type"] for m in matches if m["entity_id"] == "light.kitchen")
    assert component == server == "alias_match"
