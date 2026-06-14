"""Unit tests for badge and header card search in _find_cards_in_config.

Validates that _find_cards_in_config finds view-level badges and
sections-view header cards, addressing issue #801.
"""

from typing import Any, ClassVar

from ha_mcp.tools.tools_config_dashboards import _find_cards_in_config


class TestBadgeSearch:
    """Test badge search in _find_cards_in_config."""

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

    def test_finds_string_badge(self):
        """String badges (bare entity IDs) should be found."""
        matches = _find_cards_in_config(
            self.DASHBOARD_WITH_BADGES, entity_id="sensor.temperature"
        )
        badge_matches = [m for m in matches if m["card_type"] == "badge"]
        assert len(badge_matches) == 1
        assert badge_matches[0]["badge_index"] == 0
        assert badge_matches[0]["jq_path"] == ".views[0].badges[0]"

    def test_finds_dict_badge(self):
        """Dict-style badges with 'entity' field should be found."""
        matches = _find_cards_in_config(
            self.DASHBOARD_WITH_BADGES, entity_id="sensor.humidity"
        )
        badge_matches = [m for m in matches if m["card_type"] == "badge"]
        assert len(badge_matches) == 1
        assert badge_matches[0]["badge_index"] == 1

    def test_finds_typed_dict_badge(self):
        """Dict badges with type and entity fields should be found."""
        matches = _find_cards_in_config(
            self.DASHBOARD_WITH_BADGES, entity_id="binary_sensor.motion"
        )
        badge_matches = [m for m in matches if m["card_type"] == "badge"]
        assert len(badge_matches) == 1
        assert badge_matches[0]["badge_index"] == 2

    def test_badge_wildcard_match(self):
        """Wildcard entity_id should match badges."""
        matches = _find_cards_in_config(
            self.DASHBOARD_WITH_BADGES, entity_id="sensor.*"
        )
        badge_matches = [m for m in matches if m["card_type"] == "badge"]
        assert len(badge_matches) == 2  # temperature + humidity

    def test_badge_no_match(self):
        """Non-matching entity_id should not find badges."""
        matches = _find_cards_in_config(
            self.DASHBOARD_WITH_BADGES, entity_id="light.nonexistent"
        )
        badge_matches = [m for m in matches if m["card_type"] == "badge"]
        assert len(badge_matches) == 0

    def test_badge_search_with_card_type_badge(self):
        """card_type='badge' should trigger badge search."""
        matches = _find_cards_in_config(
            self.DASHBOARD_WITH_BADGES,
            entity_id="sensor.temperature",
            card_type="badge",
        )
        assert len(matches) == 1
        assert matches[0]["card_type"] == "badge"

    def test_badge_search_skipped_with_other_card_type(self):
        """card_type other than 'badge' should not return badges."""
        matches = _find_cards_in_config(
            self.DASHBOARD_WITH_BADGES,
            entity_id="sensor.temperature",
            card_type="tile",
        )
        badge_matches = [m for m in matches if m["card_type"] == "badge"]
        assert len(badge_matches) == 0

    def test_badge_and_card_returned_together(self):
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
        matches = _find_cards_in_config(config, entity_id="light.living_room")
        card_types = [m["card_type"] for m in matches]
        assert "badge" in card_types
        assert "tile" in card_types


class TestHeaderCardSearch:
    """Test sections-view header card search in _find_cards_in_config."""

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

    def test_finds_header_card_by_entity(self):
        """Header card with entity reference should be found."""
        matches = _find_cards_in_config(
            self.DASHBOARD_WITH_HEADER, entity_id="sensor.temperature"
        )
        header_matches = [m for m in matches if m["jq_path"].endswith(".header.card")]
        assert len(header_matches) == 1
        assert header_matches[0]["card_type"] == "markdown"
        assert header_matches[0]["jq_path"] == ".views[0].header.card"

    def test_finds_header_card_by_type(self):
        """Header card should be found by card_type filter."""
        matches = _find_cards_in_config(
            self.DASHBOARD_WITH_HEADER, card_type="markdown"
        )
        header_matches = [m for m in matches if m["jq_path"].endswith(".header.card")]
        assert len(header_matches) == 1

    def test_no_header_returns_nothing(self):
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
        matches = _find_cards_in_config(config, entity_id="light.test")
        header_matches = [m for m in matches if "header.card" in m.get("jq_path", "")]
        assert len(header_matches) == 0

    def test_empty_header_ignored(self):
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
        matches = _find_cards_in_config(config, entity_id="sensor.test")
        assert len(matches) == 0


class TestStrategyDashboard:
    """Ensure strategy dashboards return empty results."""

    def test_strategy_dashboard_returns_empty(self):
        config = {"strategy": {"type": "home"}, "views": []}
        matches = _find_cards_in_config(config, entity_id="light.test")
        assert matches == []


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

    def test_finds_nested_card_by_type(self):
        """A gauge nested in a vertical-stack is found (the reported bug)."""
        matches = _find_cards_in_config(self.DASHBOARD_NESTED, card_type="gauge")
        assert len(matches) == 1
        assert matches[0]["card_type"] == "gauge"
        assert matches[0]["jq_path"] == ".views[0].sections[0].cards[1].cards[0]"
        assert (
            matches[0]["python_path"]
            == "['views'][0]['sections'][0]['cards'][1]['cards'][0]"
        )

    def test_finds_doubly_nested_card_by_entity(self):
        """A tile two levels deep (stack in stack) is found by entity_id."""
        matches = _find_cards_in_config(self.DASHBOARD_NESTED, entity_id="light.deep")
        assert len(matches) == 1
        assert (
            matches[0]["python_path"]
            == "['views'][0]['sections'][0]['cards'][1]['cards'][1]['cards'][0]"
        )

    def test_nested_flat_indices_point_at_top_level_container(self):
        """Flat *_index fields locate the top-level container, not the depth."""
        matches = _find_cards_in_config(self.DASHBOARD_NESTED, card_type="gauge")
        m = matches[0]
        assert m["view_index"] == 0
        assert m["section_index"] == 0
        assert m["card_index"] == 1  # the vertical-stack's index in the section

    def test_finds_card_nested_in_conditional(self):
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
        matches = _find_cards_in_config(config, entity_id="light.cond")
        assert len(matches) == 1
        assert matches[0]["jq_path"] == ".views[0].cards[0].card"
        assert matches[0]["python_path"] == "['views'][0]['cards'][0]['card']"

    def test_top_level_card_still_found_and_has_python_path(self):
        """Top-level matches are unchanged and gain a python_path."""
        config = {"views": [{"cards": [{"type": "tile", "entity": "light.top"}]}]}
        matches = _find_cards_in_config(config, card_type="tile")
        assert len(matches) == 1
        assert matches[0]["jq_path"] == ".views[0].cards[0]"
        assert matches[0]["python_path"] == "['views'][0]['cards'][0]"

    def test_python_path_is_usable_in_safe_execute(self):
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
        match = _find_cards_in_config(config, card_type="gauge")[0]
        expr = f"config{match['python_path']}['name'] = 'Renamed'"
        result = safe_execute(expr, config)
        assert result["views"][0]["cards"][0]["cards"][0]["name"] == "Renamed"

    def test_leaf_at_depth_bound_is_found(self):
        """Boundary-inside: a leaf exactly at _MAX_CARD_DEPTH is still returned."""
        from ha_mcp.tools.tools_config_dashboards import _MAX_CARD_DEPTH

        # Top-level card is depth 0; _MAX wrappers put the leaf at depth==_MAX.
        leaf: dict[str, Any] = {"type": "tile", "entity": "light.at_bound"}
        node = leaf
        for _ in range(_MAX_CARD_DEPTH):
            node = {"type": "vertical-stack", "cards": [node]}
        config = {"views": [{"cards": [node]}]}
        trunc: list[str] = []
        matches = _find_cards_in_config(
            config, entity_id="light.at_bound", truncation=trunc
        )
        assert len(matches) == 1
        assert matches[0]["card_type"] == "tile"
        assert trunc == []  # nothing truncated exactly at the bound

    def test_leaf_past_depth_bound_dropped_flagged_and_warned(self, caplog):
        """Boundary-outside + H2: a leaf one level past the bound is dropped, the
        truncation accumulator is populated, and the warning fires."""
        import logging

        from ha_mcp.tools.tools_config_dashboards import _MAX_CARD_DEPTH

        leaf: dict[str, Any] = {"type": "tile", "entity": "light.too_deep"}
        node = leaf
        for _ in range(_MAX_CARD_DEPTH + 1):
            node = {"type": "vertical-stack", "cards": [node]}
        config = {"views": [{"cards": [node]}]}
        trunc: list[str] = []
        with caplog.at_level(logging.WARNING):
            matches = _find_cards_in_config(
                config, entity_id="light.too_deep", truncation=trunc
            )
        assert matches == []  # past the bound, not found, no exception
        assert trunc, "depth-bound truncation must be reported to the caller"
        assert any("depth bound" in r.message.lower() for r in caplog.records)

    def test_finds_nested_heading_card(self):
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
        matches = _find_cards_in_config(config, heading="nested")
        assert len(matches) == 1
        assert matches[0]["card_type"] == "heading"
        assert matches[0]["jq_path"] == ".views[0].cards[0].cards[0]"

    def test_finds_card_nested_inside_header_card(self):
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
        matches = _find_cards_in_config(config, entity_id="light.header_nested")
        assert len(matches) == 1
        assert matches[0]["jq_path"] == ".views[0].header.card.cards[0]"
        assert matches[0]["python_path"] == "['views'][0]['header']['card']['cards'][0]"

    def test_python_path_round_trips_sections_prefix(self):
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
        m = _find_cards_in_config(config, card_type="gauge")[0]
        result = safe_execute(f"config{m['python_path']}['name'] = 'S'", config)
        assert result["views"][0]["sections"][0]["cards"][0]["cards"][0]["name"] == "S"

    def test_python_path_round_trips_header_prefix(self):
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
        m = _find_cards_in_config(config, entity_id="light.h")[0]
        result = safe_execute(f"config{m['python_path']}['name'] = 'H'", config)
        assert result["views"][0]["header"]["card"]["cards"][0]["name"] == "H"

    def test_multiple_siblings_in_one_stack(self):
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
        matches = _find_cards_in_config(config, card_type="tile")
        assert len(matches) == 2
        assert {m["python_path"] for m in matches} == {
            "['views'][0]['cards'][0]['cards'][0]",
            "['views'][0]['cards'][0]['cards'][1]",
        }

    def test_container_and_nested_container_both_match(self):
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
        matches = _find_cards_in_config(config, card_type="vertical-stack")
        paths = {m["python_path"] for m in matches}
        assert paths == {
            "['views'][0]['cards'][0]",
            "['views'][0]['cards'][0]['cards'][0]",
        }

    def test_node_with_both_cards_and_card_keys(self):
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
        matches = _find_cards_in_config(config, card_type="tile")
        assert {m["card_config"]["entity"] for m in matches} == {
            "light.in_cards",
            "light.in_card",
        }
        assert {m["python_path"] for m in matches} == {
            "['views'][0]['cards'][0]['cards'][0]",
            "['views'][0]['cards'][0]['card']",
        }

    def test_finds_card_nested_in_button_card_custom_fields(self):
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
        matches = _find_cards_in_config(config, entity_id="light.bc_nested")
        assert len(matches) == 1
        assert matches[0]["python_path"] == (
            "['views'][0]['cards'][0]['custom_fields']['content']['card']['cards'][0]"
        )

    def test_typeless_dict_under_cards_not_matched(self):
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
        assert _find_cards_in_config(config, entity_id="light.no_type") == []

    def test_characterization_state_switch_states_not_traversed(self):
        """Gap (M3): custom:state-switch nests cards under `states` (name->card),
        which is not a traversed key. Pins the boundary; flip when supported."""
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
        assert _find_cards_in_config(config, entity_id="light.ss") == []

    def test_characterization_picture_elements_not_traversed(self):
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
        assert _find_cards_in_config(config, entity_id="light.pe") == []
