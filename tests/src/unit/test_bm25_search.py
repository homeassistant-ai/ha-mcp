"""BM25 fuzzy search (issue #851) on entities and configs from real Core.

The tokenizer and the BM25 scorer are pure and get token lists. Entity search
runs ``smart_entity_search`` on the server's own client against a real
``hass``: states come from Core's state machine, aliases and ``hidden_by``
from its entity registry. Config scoring runs ``deep_search`` over
automations Core loaded.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.utils.fuzzy_search import (
    HIDDEN_SCORE_PENALTY,
    BM25Scorer,
    apply_hidden_penalty,
    tokenize,
)

from ._deep_search_core import automation, load_automations, search_tools

ROOMS = {
    "light.kitchen_ceiling": "Kitchen Ceiling Light",
    "light.living_room": "Living Room Light",
    "sensor.kitchen_temperature": "Kitchen Temperature",
    "light.bedroom": "Bedroom Light",
    "binary_sensor.garage_door": "Garage Door",
}
LIGHTS = {
    "light.bed_light": "Bed Light",
    "light.ceiling_lights": "Ceiling Lights",
    "light.kitchen_lights": "Kitchen Lights",
    "cover.garage_door": "Garage Door",
}


async def _add_states(hass: HomeAssistant, names: dict[str, str]) -> None:
    """``names`` as states, on a Core whose registry commands answer."""
    assert await async_setup_component(hass, "config", {})
    for entity_id, name in names.items():
        hass.states.async_set(entity_id, "on", {"friendly_name": name})


async def _register(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_id: str,
    name: str,
    **changes: Any,
) -> None:
    """``entity_id`` in Core's registry with ``changes``, and its state."""
    domain, object_id = entity_id.split(".", 1)
    entry = entity_registry.async_get_or_create(
        domain, "bm25_test", object_id, suggested_object_id=object_id
    )
    assert entry.entity_id == entity_id
    if changes:
        entity_registry.async_update_entity(entity_id, **changes)
    await _add_states(hass, {entity_id: name})


async def _search(
    client: HomeAssistantClient,
    query: str,
    *,
    threshold: int = 60,
    limit: int = 10,
    offset: int = 0,
) -> dict[str, Any]:
    return await SmartSearchTools(
        client=client, fuzzy_threshold=threshold
    ).smart_entity_search(query, limit=limit, offset=offset)


def _ids(result: dict[str, Any]) -> list[str]:
    return [match["entity_id"] for match in result["matches"]]


# ---------------------------------------------------------------------------
# tokenize()
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_entity_id_splits_on_dot_and_underscore(self) -> None:
        assert tokenize("light.kitchen_ceiling") == ["light", "kitchen", "ceiling"]

    def test_friendly_name_splits_on_spaces(self) -> None:
        assert tokenize("Kitchen Ceiling Light") == ["kitchen", "ceiling", "light"]

    def test_mixed_delimiters(self) -> None:
        assert tokenize("sensor.living_room-temp 2") == [
            "sensor",
            "living",
            "room",
            "temp",
            "2",
        ]

    def test_empty_string(self) -> None:
        assert tokenize("") == []

    def test_single_token(self) -> None:
        assert tokenize("light") == ["light"]


# ---------------------------------------------------------------------------
# BM25Scorer
# ---------------------------------------------------------------------------


class TestBM25Scorer:
    @pytest.fixture
    def simple_corpus(self) -> list[list[str]]:
        return [
            ["kitchen", "ceiling", "light"],
            ["living", "room", "light"],
            ["kitchen", "temperature", "sensor"],
            ["bedroom", "light"],
            ["garage", "door"],
        ]

    def test_rarer_terms_weigh_more(self, simple_corpus: list[list[str]]) -> None:
        """'light' is in 3/5 docs, 'kitchen' in 2/5, 'garage' in 1/5."""
        scorer = BM25Scorer()
        scorer.fit(simple_corpus)
        light, kitchen, garage = (
            scorer.max_possible_score([token])
            for token in ("light", "kitchen", "garage")
        )
        assert garage > kitchen > light

    def test_score_prefers_rare_term(self, simple_corpus: list[list[str]]) -> None:
        """'kitchen light' ranks the kitchen ceiling light above the living
        room light, because 'kitchen' is rarer than 'light'."""
        scorer = BM25Scorer()
        scorer.fit(simple_corpus)
        scores = scorer.score_all(["kitchen", "light"])
        assert scores[0] > scores[1]

    def test_score_zero_for_no_match(self, simple_corpus: list[list[str]]) -> None:
        scorer = BM25Scorer()
        scorer.fit(simple_corpus)
        assert all(s == 0.0 for s in scorer.score_all(["nonexistent"]))

    def test_multi_word_non_adjacent(self, simple_corpus: list[list[str]]) -> None:
        """Query terms that exist but are not adjacent still score: the
        'dryer override' case from issue #851."""
        scorer = BM25Scorer()
        scorer.fit(simple_corpus)
        assert scorer.score(["kitchen", "sensor"], 2) > 0

    def test_single_doc(self) -> None:
        """Config scoring fits a one-document corpus; its terms still score."""
        scorer = BM25Scorer()
        scorer.fit([["hello", "world"]])
        assert scorer.score_all(["hello"])[0] > 0


# ---------------------------------------------------------------------------
# Entity search with BM25
# ---------------------------------------------------------------------------


class TestEntitySearchBM25:
    async def test_multi_word_query_ranks_correctly(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """'kitchen light' puts the kitchen ceiling light first."""
        await _add_states(hass, ROOMS)
        result = await _search(core_client, "kitchen light", threshold=30, limit=5)
        assert _ids(result)[0] == "light.kitchen_ceiling"

    async def test_production_threshold_passes_full_match(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """A match holding every query token clears the production threshold."""
        await _add_states(hass, ROOMS)
        result = await _search(core_client, "kitchen ceiling", limit=5)
        assert _ids(result)[0] == "light.kitchen_ceiling"

    async def test_production_threshold_filters_partial_match(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """A query sharing only the common token 'light' does not reach the
        production threshold: scores are normalised against the query's
        theoretical maximum, not inflated to 100 for the best hit."""
        await _add_states(hass, ROOMS)
        result = await _search(core_client, "light nonexistent", limit=5)
        assert result["matches"] == []

    async def test_single_word_query(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await _add_states(hass, ROOMS)
        result = await _search(core_client, "garage", threshold=30, limit=5)
        assert "binary_sensor.garage_door" in _ids(result)

    async def test_no_match_returns_empty(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await _add_states(hass, ROOMS)
        result = await _search(
            core_client, "microcontroller zebra", threshold=30, limit=5
        )
        assert (result["total_matches"], result["matches"]) == (0, [])

    async def test_empty_query_returns_empty(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await _add_states(hass, ROOMS)
        result = await _search(core_client, "", limit=5)
        assert result["total_matches"] == 0

    async def test_no_entities_returns_empty(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await _add_states(hass, {})
        result = await _search(core_client, "kitchen", limit=5)
        assert result["total_matches"] == 0

    async def test_pages_do_not_overlap(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await _add_states(hass, ROOMS)
        first = await _search(core_client, "light", threshold=30, limit=2)
        second = await _search(core_client, "light", threshold=30, limit=2, offset=2)
        assert len(_ids(first)) == 2
        assert _ids(second)
        assert not set(_ids(first)) & set(_ids(second))

    async def test_underscore_space_equivalence(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """'tesla_ble' and 'tesla ble' find the same entity."""
        await _add_states(
            hass, {"number.tesla_ble_charging_amps": "Tesla BLE Charging Amps"}
        )
        underscore = await _search(core_client, "tesla_ble", threshold=30, limit=5)
        space = await _search(core_client, "tesla ble", threshold=30, limit=5)
        assert _ids(underscore) == _ids(space) == ["number.tesla_ble_charging_amps"]


# ---------------------------------------------------------------------------
# Config scoring in deep_search
# ---------------------------------------------------------------------------

DRYER_LOAD_SHARING = automation(
    "uid_dryer",
    "Tesla Mobile Connector Dryer Load Sharing",
    triggers=[{"trigger": "state", "entity_id": "sensor.dryer_power"}],
    actions=[
        {
            "action": "input_boolean.toggle",
            "target": {"entity_id": "input_boolean.emporia_vehicle_tesla_override"},
        }
    ],
)


class TestDeepSearchConfigScoring:
    async def test_fuzzy_finds_non_adjacent_terms_in_config(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """The 'dryer override' case: both terms are in the config, apart."""
        await load_automations(hass, [DRYER_LOAD_SHARING])
        result = await search_tools(core_client).deep_search(
            "dryer override", search_types=["automation"], exact_match=False
        )
        [found] = result["automations"]
        assert found["match_in_config"] is True

    async def test_exact_match_requires_contiguous_substring(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await load_automations(hass, [DRYER_LOAD_SHARING])
        result = await search_tools(core_client).deep_search(
            "dryer override", search_types=["automation"], exact_match=True
        )
        assert result["automations"] == []

    async def test_exact_match_finds_contiguous_substring(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        await load_automations(
            hass,
            [
                automation(
                    "uid_laundry",
                    "Laundry Helper",
                    description="Turn on dryer override mode",
                )
            ],
        )
        result = await search_tools(core_client).deep_search(
            "dryer override", search_types=["automation"], exact_match=True
        )
        [found] = result["automations"]
        assert (found["match_in_name"], found["match_in_config"]) == (False, True)


# ---------------------------------------------------------------------------
# Issue #1170: elided separators, typo fallback gates, aliases
# ---------------------------------------------------------------------------


class TestFuzzySearcherIssue1170:
    async def test_finding_2_elided_separator_query_finds_target_uniquely(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """``bedlight`` (no separator) matches only ``light.bed_light``, not
        every light at one tied score."""
        await _add_states(hass, LIGHTS)
        result = await _search(core_client, "bedlight")
        assert _ids(result) == ["light.bed_light"]
        assert result["matches"][0]["score"] >= 75

    async def test_finding_5_multi_token_garbage_rejected(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """A 3-token nonsense query whose one token grazes ``garage``
        (``garbage``) does not surface the garage door."""
        await _add_states(hass, LIGHTS)
        result = await _search(core_client, "xyz_irrelevant_garbage")
        assert result["total_matches"] == 0

    async def test_finding_5_single_token_typo_still_recalls(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """A single-token typo (``ligth``) still finds the lights."""
        await _add_states(hass, LIGHTS)
        result = await _search(core_client, "ligth")
        assert _ids(result)
        assert all(eid.startswith("light.") for eid in _ids(result))

    async def test_typo_fallback_short_single_token_returns_empty(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """A 3-character query (``lit``) surfaced ``u7_lite_*`` and
        ``shopping_list`` through partial overlap; it finds nothing."""
        await _add_states(
            hass,
            {
                "button.u7_lite_restart": "U7 Lite Restart",
                "light.u7_lite_led": "U7 Lite LED",
                "todo.shopping_list": "Shopping List",
            },
        )
        result = await _search(core_client, "lit")
        assert result["total_matches"] == 0

    async def test_typo_fallback_four_char_single_token_still_fires(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """The length gate stops at 3 characters: the 4-character typo
        ``gats`` still finds the gates."""
        await _add_states(hass, {"light.gates": "Gates"})
        result = await _search(core_client, "gats")
        assert [(m["entity_id"], m["match_type"]) for m in result["matches"]] == [
            ("light.gates", "typo_fallback")
        ]

    async def test_finding_8_alias_search_with_match_type_label(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
    ) -> None:
        """A registry alias that neither the id nor the name holds finds the
        entity, labelled ``alias_match``."""
        await _register(
            hass,
            entity_registry,
            "light.bed_light",
            "Bed Light",
            aliases=["lullaby lamp"],
        )
        await _add_states(hass, LIGHTS)
        result = await _search(core_client, "lullaby")
        assert [(m["entity_id"], m["match_type"]) for m in result["matches"]] == [
            ("light.bed_light", "alias_match")
        ]

    async def test_finding_8_no_alias_no_match(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
    ) -> None:
        await _register(hass, entity_registry, "light.bed_light", "Bed Light")
        await _add_states(hass, LIGHTS)
        result = await _search(core_client, "lullaby")
        assert result["total_matches"] == 0

    async def test_finding_8_alias_does_not_overshadow_name_match(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
    ) -> None:
        """A query token in both the id and an alias is labelled by the id
        match, not ``alias_match``."""
        await _register(
            hass,
            entity_registry,
            "light.bed_light",
            "Bed Light",
            aliases=["bed lullaby"],
        )
        await _add_states(hass, LIGHTS)
        result = await _search(core_client, "bed")
        target = next(
            m for m in result["matches"] if m["entity_id"] == "light.bed_light"
        )
        assert target["match_type"] == "partial_id"


class TestSmartEntitySearchPropagation:
    async def test_get_states_failure_propagates_as_tool_error(
        self, core_client: HomeAssistantClient
    ) -> None:
        """No answer to the states request is an error, not a zero-result
        success the caller would accept."""
        with (
            patch.object(
                core_client,
                "get_states",
                AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
            ),
            pytest.raises(ToolError),
        ):
            await _search(core_client, "anything", limit=5)


# ---------------------------------------------------------------------------
# Hidden entities: penalised, not filtered
# ---------------------------------------------------------------------------


class TestHiddenScorePenalty:
    def test_apply_hidden_penalty_subtracts_for_hidden(self) -> None:
        user, integration = (
            er.RegistryEntryHider.USER,
            er.RegistryEntryHider.INTEGRATION,
        )
        assert apply_hidden_penalty(100, user) == 100 - HIDDEN_SCORE_PENALTY
        assert apply_hidden_penalty(80, integration) == 80 - HIDDEN_SCORE_PENALTY

    def test_apply_hidden_penalty_passthrough_for_visible(self) -> None:
        assert apply_hidden_penalty(100, None) == 100
        assert apply_hidden_penalty(60, None) == 60

    def test_apply_hidden_penalty_clamps_to_zero(self) -> None:
        assert apply_hidden_penalty(10, er.RegistryEntryHider.USER) == 0

    async def test_visible_outranks_hidden_on_same_query(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
    ) -> None:
        await _add_states(hass, {"light.kitchen_main": "Kitchen Main"})
        await _register(
            hass,
            entity_registry,
            "light.kitchen_diag",
            "Kitchen Diag",
            hidden_by=er.RegistryEntryHider.INTEGRATION,
        )
        result = await _search(core_client, "kitchen")
        visible, hidden = result["matches"]
        assert (visible["entity_id"], hidden["entity_id"]) == (
            "light.kitchen_main",
            "light.kitchen_diag",
        )
        assert hidden["score"] < visible["score"]

    async def test_hidden_typo_fallback_penalised(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
    ) -> None:
        """A typo that matches a hidden and a visible entity equally ranks
        the hidden one lower by the penalty."""
        await _add_states(hass, {"light.kitchen": "Kitchen"})
        await _register(
            hass,
            entity_registry,
            "switch.kitchen_fan",
            "Kitchen Fan",
            hidden_by=er.RegistryEntryHider.INTEGRATION,
        )
        result = await _search(core_client, "kitchn")
        visible, hidden = result["matches"]
        assert [visible["entity_id"], hidden["entity_id"]] == [
            "light.kitchen",
            "switch.kitchen_fan",
        ]
        assert {visible["match_type"], hidden["match_type"]} == {"typo_fallback"}
        assert hidden["score"] == visible["score"] - HIDDEN_SCORE_PENALTY

    async def test_hidden_borderline_raw_score_threshold_edge(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
    ) -> None:
        """The threshold gates the raw score before the penalty: a hidden
        entity scoring exactly the threshold still surfaces, penalised."""
        await _register(
            hass,
            entity_registry,
            "sensor.borderline",
            "borderline",
            hidden_by=er.RegistryEntryHider.INTEGRATION,
        )
        result = await _search(core_client, "borderline", threshold=100)
        assert [(m["entity_id"], m["score"]) for m in result["matches"]] == [
            ("sensor.borderline", 100 - HIDDEN_SCORE_PENALTY)
        ]

    async def test_score_ties_break_on_entity_id_ascending(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """Tied scores page in a stable order: entity_id ascending."""
        await _add_states(
            hass,
            {"light.tie_b": "match", "light.tie_a": "match", "light.tie_c": "match"},
        )
        result = await _search(core_client, "match")
        assert _ids(result) == ["light.tie_a", "light.tie_b", "light.tie_c"]
