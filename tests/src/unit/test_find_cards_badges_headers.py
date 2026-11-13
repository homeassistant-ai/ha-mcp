"""View-badge and sections-view header-card matches in the card search (#801).

Each config is stored as a storage dashboard in a real ``hass`` and searched
with ``ha_config_get_dashboard``; ``find`` returns its matches and ``search``
the whole result, whose warnings report where the search could not look.
"""

from collections.abc import Awaitable, Callable
from typing import Any, ClassVar

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_dashboards import MAX_CARD_DEPTH

from ._dashboard_search_helpers import search_cards
from ._tool_errors import error_of

Search = Callable[..., Awaitable[dict[str, Any]]]
Find = Callable[..., Awaitable[list[dict[str, Any]]]]


@pytest.fixture
def search(hass: HomeAssistant, core_client: HomeAssistantClient) -> Search:
    async def run(config: dict[str, Any], **criteria: str) -> dict[str, Any]:
        return await search_cards(hass, core_client, config, **criteria)

    return run


@pytest.fixture
def find(search: Search) -> Find:
    async def run(config: dict[str, Any], **criteria: str) -> list[dict[str, Any]]:
        return list((await search(config, **criteria))["matches"])

    return run


class TestBadgeSearch:
    """Badge matches in the dashboard card search."""

    DASHBOARD_WITH_BADGES: ClassVar[dict[str, Any]] = {
        "views": [
            {
                "title": "Home",
                "badges": [
                    "sensor.temperature",
                    {"entity": "sensor.humidity"},
                    {"type": "entity", "entity": "binary_sensor.motion"},
                ],
                "cards": [
                    {"type": "tile", "entity": "light.living_room"},
                ],
            }
        ]
    }

    async def test_finds_string_badge(self, find: Find) -> None:
        """String badges (bare entity IDs) should be found."""
        matches = await find(self.DASHBOARD_WITH_BADGES, entity_id="sensor.temperature")
        badge_matches = [m for m in matches if m["card_type"] == "badge"]
        assert len(badge_matches) == 1
        assert badge_matches[0]["badge_index"] == 0
        assert badge_matches[0]["jq_path"] == ".views[0].badges[0]"

    async def test_finds_dict_badge(self, find: Find) -> None:
        """Dict-style badges with 'entity' field should be found."""
        matches = await find(self.DASHBOARD_WITH_BADGES, entity_id="sensor.humidity")
        badge_matches = [m for m in matches if m["card_type"] == "badge"]
        assert len(badge_matches) == 1
        assert badge_matches[0]["badge_index"] == 1

    async def test_finds_typed_dict_badge(self, find: Find) -> None:
        """Dict badges with type and entity fields should be found."""
        matches = await find(
            self.DASHBOARD_WITH_BADGES, entity_id="binary_sensor.motion"
        )
        badge_matches = [m for m in matches if m["card_type"] == "badge"]
        assert len(badge_matches) == 1
        assert badge_matches[0]["badge_index"] == 2

    async def test_badge_wildcard_match(self, find: Find) -> None:
        """Wildcard entity_id should match badges."""
        matches = await find(self.DASHBOARD_WITH_BADGES, entity_id="sensor.*")
        badge_matches = [m for m in matches if m["card_type"] == "badge"]
        assert len(badge_matches) == 2  # temperature + humidity

    async def test_badge_no_match(self, find: Find) -> None:
        """Non-matching entity_id should not find badges."""
        matches = await find(self.DASHBOARD_WITH_BADGES, entity_id="light.nonexistent")
        badge_matches = [m for m in matches if m["card_type"] == "badge"]
        assert len(badge_matches) == 0

    async def test_badge_search_with_card_type_badge(self, find: Find) -> None:
        """card_type='badge' keeps only badge matches."""
        matches = await find(
            self.DASHBOARD_WITH_BADGES,
            entity_id="sensor.temperature",
            card_type="badge",
        )
        assert len(matches) == 1
        assert matches[0]["card_type"] == "badge"

    async def test_badge_search_skipped_with_other_card_type(self, find: Find) -> None:
        """card_type other than 'badge' should not return badges."""
        matches = await find(
            self.DASHBOARD_WITH_BADGES,
            entity_id="sensor.temperature",
            card_type="tile",
        )
        badge_matches = [m for m in matches if m["card_type"] == "badge"]
        assert len(badge_matches) == 0

    async def test_badge_and_card_returned_together(self, find: Find) -> None:
        """Entity search should return both card and badge matches."""
        config = {
            "views": [
                {
                    "title": "Test",
                    "badges": ["light.living_room"],
                    "cards": [
                        {"type": "tile", "entity": "light.living_room"},
                    ],
                }
            ]
        }
        matches = await find(config, entity_id="light.living_room")
        assert {"badge", "tile"} <= {m["card_type"] for m in matches}


class TestHeaderCardSearch:
    """Sections-view header-card matches in the dashboard card search."""

    DASHBOARD_WITH_HEADER: ClassVar[dict[str, Any]] = {
        "views": [
            {
                "title": "Sections View",
                "type": "sections",
                "header": {
                    "card": {
                        "type": "markdown",
                        "entity": "sensor.temperature",
                        "content": "Current: {{ states('sensor.temperature') }}",
                    }
                },
                "sections": [
                    {
                        "cards": [
                            {"type": "tile", "entity": "light.bedroom"},
                        ]
                    }
                ],
            }
        ]
    }

    async def test_finds_header_card_by_entity(self, find: Find) -> None:
        """Header card with entity reference should be found."""
        matches = await find(self.DASHBOARD_WITH_HEADER, entity_id="sensor.temperature")
        header_matches = [m for m in matches if m["jq_path"].endswith(".header.card")]
        assert len(header_matches) == 1
        assert header_matches[0]["card_type"] == "markdown"
        assert header_matches[0]["jq_path"] == ".views[0].header.card"

    async def test_finds_header_card_by_type(self, find: Find) -> None:
        """Header card should be found by card_type filter."""
        matches = await find(self.DASHBOARD_WITH_HEADER, card_type="markdown")
        header_matches = [m for m in matches if m["jq_path"].endswith(".header.card")]
        assert len(header_matches) == 1

    async def test_no_header_returns_nothing(self, find: Find) -> None:
        """Views without header should not produce header matches."""
        config = {
            "views": [
                {
                    "title": "No Header",
                    "type": "sections",
                    "sections": [{"cards": [{"type": "tile", "entity": "light.test"}]}],
                }
            ]
        }
        matches = await find(config, entity_id="light.test")
        header_matches = [m for m in matches if "header.card" in m.get("jq_path", "")]
        assert len(header_matches) == 0

    async def test_empty_header_ignored(self, find: Find) -> None:
        """Empty header dict should not crash."""
        config = {
            "views": [
                {
                    "title": "Empty Header",
                    "type": "sections",
                    "header": {},
                    "sections": [],
                }
            ]
        }
        matches = await find(config, entity_id="sensor.test")
        assert len(matches) == 0


class TestStrategyDashboard:
    """A strategy dashboard has no stored cards, so its search is refused."""

    async def test_strategy_dashboard_search_is_refused(self, search: Search) -> None:
        config = {"strategy": {"type": "home"}, "views": []}
        with pytest.raises(ToolError) as exc:
            await search(config, entity_id="light.test")
        assert error_of(exc)["code"] == "VALIDATION_FAILED"


class TestNestedCardSearch:
    """Cards nested in stacks/grids/conditional cards must be found (issue #1599)."""

    # A sections view whose section holds a vertical-stack; the stack holds a
    # gauge (only nested, never top-level) and a horizontal-stack with a tile.
    DASHBOARD_NESTED: ClassVar[dict[str, Any]] = {
        "views": [
            {
                "type": "sections",
                "sections": [
                    {
                        "cards": [
                            {"type": "heading", "heading": "Top"},
                            {
                                "type": "vertical-stack",
                                "cards": [
                                    {"type": "gauge", "entity": "sensor.cpu"},
                                    {
                                        "type": "horizontal-stack",
                                        "cards": [
                                            {"type": "tile", "entity": "light.deep"},
                                        ],
                                    },
                                ],
                            },
                        ]
                    }
                ],
            }
        ]
    }

    async def test_finds_nested_card_by_type(self, find: Find) -> None:
        """A gauge nested in a vertical-stack is found (the reported bug)."""
        matches = await find(self.DASHBOARD_NESTED, card_type="gauge")
        assert len(matches) == 1
        assert matches[0]["card_type"] == "gauge"
        assert matches[0]["jq_path"] == ".views[0].sections[0].cards[1].cards[0]"
        assert (
            matches[0]["python_path"]
            == "['views'][0]['sections'][0]['cards'][1]['cards'][0]"
        )

    async def test_finds_doubly_nested_card_by_entity(self, find: Find) -> None:
        """A tile two levels deep (stack in stack) is found by entity_id."""
        matches = await find(self.DASHBOARD_NESTED, entity_id="light.deep")
        assert len(matches) == 1
        assert (
            matches[0]["python_path"]
            == "['views'][0]['sections'][0]['cards'][1]['cards'][1]['cards'][0]"
        )

    async def test_nested_flat_indices_point_at_top_level_container(
        self, find: Find
    ) -> None:
        """Flat *_index fields locate the top-level container, not the depth."""
        matches = await find(self.DASHBOARD_NESTED, card_type="gauge")
        m = matches[0]
        assert m["view_index"] == 0
        assert m["section_index"] == 0
        assert m["card_index"] == 1  # the vertical-stack's index in the section

    async def test_finds_card_nested_in_conditional(self, find: Find) -> None:
        """Conditional cards nest via `card` (dict), not `cards`."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "conditional",
                            "conditions": [],
                            "card": {"type": "tile", "entity": "light.cond"},
                        }
                    ]
                }
            ]
        }
        matches = await find(config, entity_id="light.cond")
        assert len(matches) == 1
        assert matches[0]["jq_path"] == ".views[0].cards[0].card"
        assert matches[0]["python_path"] == "['views'][0]['cards'][0]['card']"

    async def test_top_level_card_still_found_and_has_python_path(
        self, find: Find
    ) -> None:
        """Top-level matches are unchanged and gain a python_path."""
        config = {"views": [{"cards": [{"type": "tile", "entity": "light.top"}]}]}
        matches = await find(config, card_type="tile")
        assert len(matches) == 1
        assert matches[0]["jq_path"] == ".views[0].cards[0]"
        assert matches[0]["python_path"] == "['views'][0]['cards'][0]"

    async def test_python_path_is_usable_in_safe_execute(self, find: Find) -> None:
        """python_path must splice into a working python_transform expression.

        This is the end-to-end guarantee: the returned locator, concatenated
        after `config`, mutates exactly the matched (nested) card.
        """
        from ha_mcp.utils.python_sandbox import safe_execute

        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "vertical-stack",
                            "cards": [{"type": "gauge", "entity": "sensor.cpu"}],
                        }
                    ]
                }
            ]
        }
        match = (await find(config, card_type="gauge"))[0]
        expr = f"config{match['python_path']}['name'] = 'Renamed'"
        result = safe_execute(expr, config)
        assert result["views"][0]["cards"][0]["cards"][0]["name"] == "Renamed"

    async def test_leaf_at_depth_bound_is_found(self, search: Search) -> None:
        """Boundary-inside: a leaf exactly at MAX_CARD_DEPTH is still returned."""
        # Top-level card is depth 0; MAX wrappers put the leaf at depth==MAX.
        leaf: dict[str, Any] = {"type": "tile", "entity": "light.at_bound"}
        node = leaf
        for _ in range(MAX_CARD_DEPTH):
            node = {"type": "vertical-stack", "cards": [node]}
        config = {"views": [{"cards": [node]}]}
        result = await search(config, entity_id="light.at_bound")
        assert len(result["matches"]) == 1
        assert result["matches"][0]["card_type"] == "tile"
        assert "warnings" not in result  # nothing truncated exactly at the bound

    async def test_leaf_past_depth_bound_dropped_flagged_and_warned(
        self, search: Search, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Boundary-outside + H2: a leaf one level past the bound is dropped, the
        caller is warned that the search was incomplete, and the log says so."""
        import logging

        leaf: dict[str, Any] = {"type": "tile", "entity": "light.too_deep"}
        node = leaf
        for _ in range(MAX_CARD_DEPTH + 1):
            node = {"type": "vertical-stack", "cards": [node]}
        config = {"views": [{"cards": [node]}]}
        with caplog.at_level(logging.WARNING):
            result = await search(config, entity_id="light.too_deep")
        assert result["matches"] == []  # past the bound, not found, no exception
        assert any("depth bound" in w for w in result["warnings"])
        assert any("depth bound" in r.message.lower() for r in caplog.records)

    async def test_finds_nested_heading_card(self, find: Find) -> None:
        """The heading search axis also reaches cards nested in a stack."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "vertical-stack",
                            "cards": [
                                {"type": "heading", "heading": "Nested Section"},
                            ],
                        }
                    ]
                }
            ]
        }
        matches = await find(config, heading="nested")
        assert len(matches) == 1
        assert matches[0]["card_type"] == "heading"
        assert matches[0]["jq_path"] == ".views[0].cards[0].cards[0]"

    async def test_finds_card_nested_inside_header_card(self, find: Find) -> None:
        """A card nested inside a sections-view header card is found."""
        config = {
            "views": [
                {
                    "type": "sections",
                    "header": {
                        "card": {
                            "type": "vertical-stack",
                            "cards": [
                                {"type": "tile", "entity": "light.header_nested"},
                            ],
                        }
                    },
                    "sections": [],
                }
            ]
        }
        matches = await find(config, entity_id="light.header_nested")
        assert len(matches) == 1
        assert matches[0]["jq_path"] == ".views[0].header.card.cards[0]"
        assert matches[0]["python_path"] == "['views'][0]['header']['card']['cards'][0]"

    async def test_python_path_round_trips_sections_prefix(self, find: Find) -> None:
        """python_path for a sections-nested match executes in safe_execute."""
        from ha_mcp.utils.python_sandbox import safe_execute

        config = {
            "views": [
                {
                    "type": "sections",
                    "sections": [
                        {
                            "cards": [
                                {
                                    "type": "vertical-stack",
                                    "cards": [{"type": "gauge", "entity": "sensor.x"}],
                                }
                            ]
                        }
                    ],
                }
            ]
        }
        m = (await find(config, card_type="gauge"))[0]
        result = safe_execute(f"config{m['python_path']}['name'] = 'S'", config)
        assert result["views"][0]["sections"][0]["cards"][0]["cards"][0]["name"] == "S"

    async def test_python_path_round_trips_header_prefix(self, find: Find) -> None:
        """python_path for a header-nested match executes in safe_execute."""
        from ha_mcp.utils.python_sandbox import safe_execute

        config = {
            "views": [
                {
                    "type": "sections",
                    "header": {
                        "card": {
                            "type": "vertical-stack",
                            "cards": [{"type": "tile", "entity": "light.h"}],
                        }
                    },
                    "sections": [],
                }
            ]
        }
        m = (await find(config, entity_id="light.h"))[0]
        result = safe_execute(f"config{m['python_path']}['name'] = 'H'", config)
        assert result["views"][0]["header"]["card"]["cards"][0]["name"] == "H"

    async def test_multiple_siblings_in_one_stack(self, find: Find) -> None:
        """Two matching cards in one stack → two matches with distinct paths."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "vertical-stack",
                            "cards": [
                                {"type": "tile", "entity": "light.a"},
                                {"type": "tile", "entity": "light.b"},
                            ],
                        }
                    ]
                }
            ]
        }
        matches = await find(config, card_type="tile")
        assert len(matches) == 2
        assert {m["python_path"] for m in matches} == {
            "['views'][0]['cards'][0]['cards'][0]",
            "['views'][0]['cards'][0]['cards'][1]",
        }

    async def test_container_and_nested_container_both_match(self, find: Find) -> None:
        """A container that matches AND has a matching descendant → both returned
        (pins the match-self-then-recurse order)."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "vertical-stack",
                            "cards": [
                                {
                                    "type": "vertical-stack",
                                    "cards": [{"type": "tile", "entity": "light.x"}],
                                }
                            ],
                        }
                    ]
                }
            ]
        }
        matches = await find(config, card_type="vertical-stack")
        paths = {m["python_path"] for m in matches}
        assert paths == {
            "['views'][0]['cards'][0]",
            "['views'][0]['cards'][0]['cards'][0]",
        }

    async def test_node_with_both_cards_and_card_keys(self, find: Find) -> None:
        """A single node carrying both `cards` (list) and `card` (dict) descends
        into each."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "custom:weird-wrapper",
                            "cards": [{"type": "tile", "entity": "light.in_cards"}],
                            "card": {"type": "tile", "entity": "light.in_card"},
                        }
                    ]
                }
            ]
        }
        matches = await find(config, card_type="tile")
        assert {m["card_config"]["entity"] for m in matches} == {
            "light.in_cards",
            "light.in_card",
        }
        assert {m["python_path"] for m in matches} == {
            "['views'][0]['cards'][0]['cards'][0]",
            "['views'][0]['cards'][0]['card']",
        }

    async def test_finds_card_nested_in_button_card_custom_fields(
        self, find: Find
    ) -> None:
        """custom:button-card embeds sub-cards under custom_fields.<name>.card."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "custom:button-card",
                            "custom_fields": {
                                "content": {
                                    "card": {
                                        "type": "vertical-stack",
                                        "cards": [
                                            {
                                                "type": "tile",
                                                "entity": "light.bc_nested",
                                            }
                                        ],
                                    }
                                }
                            },
                        }
                    ]
                }
            ]
        }
        matches = await find(config, entity_id="light.bc_nested")
        assert len(matches) == 1
        assert matches[0]["python_path"] == (
            "['views'][0]['cards'][0]['custom_fields']['content']['card']['cards'][0]"
        )

    async def test_typeless_dict_under_cards_not_matched(self, find: Find) -> None:
        """A non-card dict reached under `cards` (no `type`) must not match —
        precision guard against editing a non-card via a structurally valid path."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "vertical-stack",
                            "cards": [{"entity": "light.no_type"}],  # no `type`
                        }
                    ]
                }
            ]
        }
        assert await find(config, entity_id="light.no_type") == []

    async def test_state_switch_states_traversed(self, find: Find) -> None:
        """custom:state-switch nests a card per source state under `states`
        (name->card); each is traversed like a top-level card (issue #1599
        review round 2, item 2)."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "custom:state-switch",
                            "entity": "input_select.x",
                            "states": {
                                "on": {"type": "tile", "entity": "light.ss"},
                            },
                        }
                    ]
                }
            ]
        }
        matches = await find(config, entity_id="light.ss")
        assert len(matches) == 1
        assert matches[0]["card_type"] == "tile"
        assert matches[0]["jq_path"] == ".views[0].cards[0].states.on"
        assert matches[0]["python_path"] == "['views'][0]['cards'][0]['states']['on']"

    async def test_state_switch_states_python_path_round_trips(
        self, find: Find
    ) -> None:
        """A state-switch nested card's python_path mutates exactly that card."""
        from ha_mcp.utils.python_sandbox import safe_execute

        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "custom:state-switch",
                            "states": {
                                "on": {"type": "tile", "entity": "light.ss"},
                            },
                        }
                    ]
                }
            ]
        }
        m = (await find(config, card_type="tile"))[0]
        result = safe_execute(f"config{m['python_path']}['name'] = 'SS'", config)
        assert result["views"][0]["cards"][0]["states"]["on"]["name"] == "SS"

    async def test_characterization_picture_elements_not_traversed(
        self, find: Find
    ) -> None:
        """Gap (M2): picture-elements nests *elements*, not cards — not traversed.
        Pins the boundary; flip if element traversal is ever added."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "picture-elements",
                            "image": "/local/x.png",
                            "elements": [
                                {"type": "state-badge", "entity": "light.pe"},
                            ],
                        }
                    ]
                }
            ]
        }
        assert await find(config, entity_id="light.pe") == []

    # ---- Quote/dot-safe key interpolation (issue #1599 review round 2, item 3) ----

    @staticmethod
    def _button_card_with_field(field_name: str) -> dict[str, Any]:
        """A button-card wrapping one tile under custom_fields.<field_name>.card."""
        return {
            "views": [
                {
                    "cards": [
                        {
                            "type": "custom:button-card",
                            "custom_fields": {
                                field_name: {
                                    "card": {"type": "tile", "entity": "light.x"}
                                }
                            },
                        }
                    ]
                }
            ]
        }

    async def test_apostrophe_custom_field_key_python_path_round_trips(
        self, find: Find
    ) -> None:
        """A custom_fields key with an apostrophe yields a usable python_path
        (raw interpolation used to splice an unterminated string literal)."""
        from ha_mcp.utils.python_sandbox import safe_execute

        config = self._button_card_with_field("o'brien")
        m = (await find(config, card_type="tile"))[0]
        assert m["python_path"] == (
            "['views'][0]['cards'][0]['custom_fields'][\"o'brien\"]['card']"
        )
        # jq path quotes the non-identifier key so the apostrophe is inert.
        assert m["jq_path"] == ('.views[0].cards[0].custom_fields["o\'brien"].card')
        result = safe_execute(f"config{m['python_path']}['name'] = 'OB'", config)
        assert (
            result["views"][0]["cards"][0]["custom_fields"]["o'brien"]["card"]["name"]
            == "OB"
        )

    async def test_dot_custom_field_key_jq_path_bracketed(self, find: Find) -> None:
        """A custom_fields key with a dot must be bracketed in jq_path so the dot
        is not read as further nesting; python_path stays valid too."""
        from ha_mcp.utils.python_sandbox import safe_execute

        config = self._button_card_with_field("weird.dotkey")
        m = (await find(config, card_type="tile"))[0]
        assert m["jq_path"] == ('.views[0].cards[0].custom_fields["weird.dotkey"].card')
        assert m["python_path"] == (
            "['views'][0]['cards'][0]['custom_fields']['weird.dotkey']['card']"
        )
        result = safe_execute(f"config{m['python_path']}['name'] = 'D'", config)
        assert (
            result["views"][0]["cards"][0]["custom_fields"]["weird.dotkey"]["card"][
                "name"
            ]
            == "D"
        )

    async def test_apostrophe_state_switch_key_round_trips(self, find: Find) -> None:
        """The same key-safety applies to state-switch state names (e.g. on'hold)."""
        from ha_mcp.utils.python_sandbox import safe_execute

        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "custom:state-switch",
                            "states": {
                                "on'hold": {"type": "tile", "entity": "light.x"}
                            },
                        }
                    ]
                }
            ]
        }
        m = (await find(config, card_type="tile"))[0]
        assert m["python_path"] == ("['views'][0]['cards'][0]['states'][\"on'hold\"]")
        result = safe_execute(f"config{m['python_path']}['name'] = 'H'", config)
        assert result["views"][0]["cards"][0]["states"]["on'hold"]["name"] == "H"

    # ---- custom_fields edge cases (issue #1599 review round 2, item 6) ----

    async def test_custom_field_that_is_itself_a_card_matches(self, find: Find) -> None:
        """A custom_fields value that is itself a card (has `type`) matches at
        custom_fields.<name> directly, not only via a nested `card`."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "custom:button-card",
                            "custom_fields": {
                                "badge": {"type": "tile", "entity": "light.cf"}
                            },
                        }
                    ]
                }
            ]
        }
        matches = await find(config, entity_id="light.cf")
        assert len(matches) == 1
        assert matches[0]["python_path"] == (
            "['views'][0]['cards'][0]['custom_fields']['badge']"
        )

    async def test_template_string_custom_field_skipped_no_raise(
        self, find: Find
    ) -> None:
        """A non-dict custom_fields value (a template string) is skipped without
        matching and without raising."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "custom:button-card",
                            "custom_fields": {"content": "{{ states('sensor.x') }}"},
                        }
                    ]
                }
            ]
        }
        assert await find(config, entity_id="sensor.x") == []

    async def test_multiple_custom_fields_distinct_paths(self, find: Find) -> None:
        """Two card-bearing custom_fields produce two matches with distinct paths."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "custom:button-card",
                            "custom_fields": {
                                "left": {"type": "tile", "entity": "light.a"},
                                "right": {"type": "tile", "entity": "light.b"},
                            },
                        }
                    ]
                }
            ]
        }
        matches = await find(config, card_type="tile")
        assert {m["python_path"] for m in matches} == {
            "['views'][0]['cards'][0]['custom_fields']['left']",
            "['views'][0]['cards'][0]['custom_fields']['right']",
        }

    async def test_doubly_nested_custom_fields(self, find: Find) -> None:
        """A button-card whose custom_fields.card is itself a button-card with its
        own custom_fields is descended to the inner tile."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "custom:button-card",
                            "custom_fields": {
                                "outer": {
                                    "card": {
                                        "type": "custom:button-card",
                                        "custom_fields": {
                                            "inner": {
                                                "card": {
                                                    "type": "tile",
                                                    "entity": "light.deep",
                                                }
                                            }
                                        },
                                    }
                                }
                            },
                        }
                    ]
                }
            ]
        }
        matches = await find(config, entity_id="light.deep")
        assert len(matches) == 1
        assert matches[0]["python_path"] == (
            "['views'][0]['cards'][0]['custom_fields']['outer']['card']"
            "['custom_fields']['inner']['card']"
        )

    # ---- Badge python_path presence (issue #1599 review round 2, item 6) ----

    async def test_string_badge_omits_python_path(self, find: Find) -> None:
        """A bare-string badge is not subscript-assignable, so no python_path."""
        config = {"views": [{"badges": ["light.string_badge"], "cards": []}]}
        matches = await find(config, entity_id="light.string_badge")
        assert len(matches) == 1
        assert matches[0]["card_type"] == "badge"
        assert "python_path" not in matches[0]

    async def test_dict_badge_includes_python_path(self, find: Find) -> None:
        """A dict badge is subscript-assignable, so it carries a python_path."""
        config = {"views": [{"badges": [{"entity": "light.dict_badge"}], "cards": []}]}
        matches = await find(config, entity_id="light.dict_badge")
        assert len(matches) == 1
        assert matches[0]["card_type"] == "badge"
        assert matches[0]["python_path"] == "['views'][0]['badges'][0]"

    # ---- Un-coverable-shape detection (issue #1599 review round 2, item 1) ----

    async def test_uncovered_collects_picture_elements_path(
        self, search: Search
    ) -> None:
        """A picture-elements card is disclosed at its path — independent of
        whether the search matched."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "picture-elements",
                            "image": "/local/x.png",
                            "elements": [{"type": "state-badge", "entity": "light.pe"}],
                        }
                    ]
                }
            ]
        }
        # A matching search on the container itself must still disclose the
        # shape (the suppressible-warning bug: a match used to hide it).
        result = await search(config, card_type="picture-elements")
        assert result["match_count"] == 1
        [warning] = result["warnings"]
        assert "present at: .views[0].cards[0].elements." in warning

    async def test_uncovered_empty_without_untraversed_shape(
        self, search: Search
    ) -> None:
        """A fully-coverable dashboard warns of nothing — no cry-wolf on a true
        negative."""
        config = {"views": [{"cards": [{"type": "tile", "entity": "light.plain"}]}]}
        result = await search(config, card_type="nonexistent")
        assert "warnings" not in result

    # ---- Unread-entry breadcrumb (issue #1599 review round 2, item 4) ----

    async def test_non_dict_card_entry_skipped_without_raise(
        self, search: Search
    ) -> None:
        """A non-dict entry under `cards` is skipped (no match, no raise) and
        reported as a search gap rather than silently dropped."""
        config = {
            "views": [
                {
                    "cards": [
                        "not-a-card",
                        {"type": "tile", "entity": "light.ok"},
                    ]
                }
            ]
        }
        result = await search(config, card_type="tile")
        assert result["match_count"] == 1
        [warning] = result["warnings"]
        assert warning.startswith("Not searched as cards or badges")
        assert ": .views[0].cards[0]." in warning

    async def test_special_character_custom_field_keys_round_trip(
        self, find: Find
    ) -> None:
        """repr()/json.dumps key rendering yields usable paths for any key shape —
        backslash, double-quote, unicode — not just the apostrophe/dot cases."""
        from ha_mcp.utils.python_sandbox import safe_execute

        for key in ("back\\slash", 'double"quote', "unicode_фъå", "emoji_🎉"):
            config = self._button_card_with_field(key)
            m = (await find(config, card_type="tile"))[0]
            result = safe_execute(f"config{m['python_path']}['name'] = 'X'", config)
            assert (
                result["views"][0]["cards"][0]["custom_fields"][key]["card"]["name"]
                == "X"
            ), key

    async def test_state_switch_no_matching_card_returns_empty(
        self, search: Search
    ) -> None:
        """A state-switch whose nested cards don't match yields no matches and no
        un-coverable warning (states is traversed, not disclosed)."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "custom:state-switch",
                            "states": {"on": {"type": "tile", "entity": "light.x"}},
                        }
                    ]
                }
            ]
        }
        result = await search(config, entity_id="sensor.nonexistent")
        assert result["matches"] == []
        assert "warnings" not in result

    async def test_non_card_state_value_skipped(self, find: Find) -> None:
        """A states value with no `type` (or not a dict) is traversed but does not
        match — the type gate holds under states."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "custom:state-switch",
                            "states": {
                                "on": {"entity": "light.x"},  # no `type`
                                "off": "not-a-dict",
                            },
                        }
                    ]
                }
            ]
        }
        assert await find(config, entity_id="light.x") == []

    async def test_uncovered_collected_for_nested_picture_elements(
        self, search: Search
    ) -> None:
        """A picture-elements nested inside a stack is still disclosed at its
        path (detection runs at every walked node, not just top level)."""
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "vertical-stack",
                            "cards": [
                                {
                                    "type": "picture-elements",
                                    "elements": [{"type": "state-badge"}],
                                }
                            ],
                        }
                    ]
                }
            ]
        }
        result = await search(config, card_type="tile")
        [warning] = result["warnings"]
        assert "present at: .views[0].cards[0].cards[0].elements." in warning
