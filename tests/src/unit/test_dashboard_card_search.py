"""Nested-card reach and the ``query`` criterion of the dashboard card search.

Cards that custom cards nest under keys of their own (issue #2694), and the
text criterion the single search applies alongside entity_id / card_type /
heading.
"""

from typing import Any, ClassVar

from ha_mcp.tools.tools_config_dashboards import (
    DashboardConfigTools,
    _SearchCriteria,
)

from ._dashboard_search_helpers import _find_cards_in_config


class TestCardsNestedUnderCustomKeys:
    """Cards held under a custom card's own keys at any depth (issue #2694).

    Custom cards nest real card configs under keys of their own choosing
    (``groups[].cards[].card``, ``tabs[].card``); every card slot below a card is
    searched, not only the card's own top-level ``cards``/``card``.
    """

    CONFIG: ClassVar[dict[str, Any]] = {
        "views": [
            {
                "cards": [
                    {
                        "type": "custom:page-card",
                        "groups": [
                            {
                                "cards": [
                                    {
                                        "card": {"type": "tile", "entity": "light.x"},
                                        "width": 12,
                                    },
                                    {
                                        "card": {
                                            "type": "entities",
                                            "entities": ["light.x"],
                                        }
                                    },
                                    {"type": "tile", "entity": "light.x"},
                                ]
                            }
                        ],
                    },
                    {
                        "type": "custom:tabbed-card",
                        "tabs": [{"card": {"type": "tile", "entity": "light.x"}}],
                    },
                ]
            }
        ]
    }

    def test_finds_cards_under_custom_keys(self):
        matches = _find_cards_in_config(self.CONFIG, entity_id="light.x")
        assert [(m["jq_path"], m["card_type"]) for m in matches] == [
            (".views[0].cards[0].groups[0].cards[0].card", "tile"),
            (".views[0].cards[0].groups[0].cards[1].card", "entities"),
            (".views[0].cards[0].groups[0].cards[2]", "tile"),
            (".views[0].cards[1].tabs[0].card", "tile"),
        ]
        assert matches[0]["python_path"] == (
            "['views'][0]['cards'][0]['groups'][0]['cards'][0]['card']"
        )

    def test_finds_custom_card_type_by_type(self):
        matches = _find_cards_in_config(self.CONFIG, card_type="tile")
        assert [m["jq_path"] for m in matches] == [
            ".views[0].cards[0].groups[0].cards[0].card",
            ".views[0].cards[0].groups[0].cards[2]",
            ".views[0].cards[1].tabs[0].card",
        ]


class TestQueryCriterion:
    """``query`` matches strings the card itself holds; criteria are AND-ed."""

    CONFIG: ClassVar[dict[str, Any]] = {
        "views": [
            {
                "badges": [
                    "sensor.outdoor",
                    {"type": "entity", "entity": "sensor.hall"},
                ],
                "cards": [
                    {
                        "type": "vertical-stack",
                        "title": "Climate",
                        "cards": [
                            {
                                "type": "custom:button-card",
                                "name": "[[[ return states['input_number.limit'].state ]]]",
                            },
                            {"type": "tile", "entity": "input_number.limit"},
                        ],
                    },
                    {
                        "type": "entities",
                        "entities": [{"entity": "light.a", "name": "Desk"}],
                        "card_mod": {"style": "ha-card { color: red; }"},
                    },
                ],
            }
        ]
    }

    def test_query_matches_any_value_the_card_holds(self):
        matches = _find_cards_in_config(self.CONFIG, query="input_number.limit")
        assert [(m["jq_path"], m["matched"]) for m in matches] == [
            (
                ".views[0].cards[0].cards[0]",
                [
                    {
                        "field": "name",
                        "value": "[[[ return states['input_number.limit'].state ]]]",
                    }
                ],
            ),
            (
                ".views[0].cards[0].cards[1]",
                [{"field": "entity", "value": "input_number.limit"}],
            ),
        ]

    def test_query_does_not_attribute_nested_card_strings_to_the_parent(self):
        matches = _find_cards_in_config(self.CONFIG, query="climate")
        assert [m["jq_path"] for m in matches] == [".views[0].cards[0]"]

    def test_query_reads_non_card_objects_inside_a_card(self):
        matches = _find_cards_in_config(self.CONFIG, query="color: red")
        assert matches[0]["jq_path"] == ".views[0].cards[1]"
        assert matches[0]["matched"] == [
            {"field": "style", "value": "ha-card { color: red; }"}
        ]

    def test_query_and_card_type_are_combined(self):
        matches = _find_cards_in_config(
            self.CONFIG, card_type="tile", query="input_number.limit"
        )
        assert [m["jq_path"] for m in matches] == [".views[0].cards[0].cards[1]"]

    def test_query_and_entity_id_are_combined(self):
        assert _find_cards_in_config(self.CONFIG, entity_id="light.a", query="desk")
        assert not _find_cards_in_config(
            self.CONFIG, entity_id="light.a", query="kitchen"
        )

    def test_query_matches_badges(self):
        matches = _find_cards_in_config(self.CONFIG, query="sensor.")
        badge_matches = [m for m in matches if m["card_type"] == "badge"]
        assert [m["matched"] for m in badge_matches] == [
            [{"field": "badges", "value": "sensor.outdoor"}],
            [{"field": "entity", "value": "sensor.hall"}],
        ]

    def test_no_query_leaves_out_matched(self):
        matches = _find_cards_in_config(self.CONFIG, card_type="tile")
        assert "matched" not in matches[0]


class TestSearchSurvivesUnusualConfigs:
    """Shapes HA stores that must neither hide cards nor fail a search."""

    def test_untyped_view_with_sections_is_searched(self):
        # The frontend renders a view with `sections` and no `type` as sections.
        config = {
            "views": [
                {"sections": [{"cards": [{"type": "tile", "entity": "light.a"}]}]}
            ]
        }
        matches = _find_cards_in_config(config, entity_id="light.a")
        assert [m["jq_path"] for m in matches] == [".views[0].sections[0].cards[0]"]

    def test_wildcard_entity_search_skips_rows_without_an_entity(self):
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "entities",
                            "entities": [{"type": "divider"}, "light.a"],
                        }
                    ]
                }
            ]
        }
        assert len(_find_cards_in_config(config, entity_id="light.*")) == 1

    def test_heading_search_reads_non_string_titles(self):
        config = {
            "views": [{"cards": [{"type": "entities", "title": 5, "entities": []}]}]
        }
        assert len(_find_cards_in_config(config, heading="5")) == 1
        assert _find_cards_in_config(config, heading="x") == []

    def test_null_badges_and_sections_do_not_fail_the_search(self):
        config = {
            "views": [
                {
                    "badges": None,
                    "sections": None,
                    "cards": [{"type": "tile", "entity": "light.a"}],
                }
            ]
        }
        assert len(_find_cards_in_config(config, query="light.a")) == 1

    def test_typed_objects_outside_card_slots_are_not_cards(self):
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "tile",
                            "entity": "light.a",
                            "features": [{"type": "light-brightness"}],
                        },
                        {
                            "type": "entities",
                            "entities": [
                                {
                                    "type": "attribute",
                                    "entity": "light.b",
                                    "attribute": "x",
                                }
                            ],
                        },
                    ]
                }
            ]
        }
        assert _find_cards_in_config(config, card_type="light-brightness") == []
        assert _find_cards_in_config(config, card_type="attribute") == []
        row_owner = _find_cards_in_config(config, entity_id="light.b")
        assert [m["jq_path"] for m in row_owner] == [".views[0].cards[1]"]
        feature_owner = _find_cards_in_config(config, query="light-brightness")
        assert [m["jq_path"] for m in feature_owner] == [".views[0].cards[0]"]

    def test_view_cards_and_section_cards_are_both_searched_once(self):
        config = {
            "views": [
                {
                    "type": "sections",
                    "cards": [{"type": "tile", "entity": "light.a"}],
                    "sections": [{"cards": [{"type": "tile", "entity": "light.a"}]}],
                }
            ]
        }
        matches = _find_cards_in_config(config, entity_id="light.a")
        assert [m["jq_path"] for m in matches] == [
            ".views[0].cards[0]",
            ".views[0].sections[0].cards[0]",
        ]

    def test_badge_with_a_non_string_entity_is_skipped(self):
        config = {"views": [{"badges": [{"type": "entity", "entity": 5}], "cards": []}]}
        assert _find_cards_in_config(config, entity_id="sensor.*") == []

    def test_config_without_views_has_no_cards(self):
        assert _find_cards_in_config({"views": None}, card_type="tile") == []


class TestSearchResultScope:
    """Scope of a search result: dashboard names in warning locations, top-level hash.

    Only a search across dashboards names the dashboard in warning locations,
    and its top-level config_hash is null.
    """

    CONFIG: ClassVar[dict[str, Any]] = {
        "views": [
            {
                "cards": [
                    {"type": "picture-elements", "elements": []},
                    {"type": "picture-elements", "elements": [{"type": "icon"}]},
                ]
            }
        ]
    }

    def _search(self, url_path: str | None) -> dict[str, Any]:
        return DashboardConfigTools._build_search_result(
            [{"url_path": "only", "config": self.CONFIG}],
            criteria=_SearchCriteria(card_type="picture-elements"),
            include_config=False,
            url_path=url_path,
        )

    def test_scoped_search_locations_name_no_dashboard(self):
        result = self._search("only")
        assert any(".views[0].cards[1].elements" in w for w in result["warnings"])
        assert not any("only:" in w for w in result["warnings"])

    def test_search_across_dashboards_names_the_dashboard_in_locations(self):
        result = self._search(None)
        assert result["config_hash"] is None
        assert any("only:.views[0].cards[1].elements" in w for w in result["warnings"])


class TestEntityWildcards:
    """``*`` matches any run of characters across the whole entity id."""

    CONFIG: ClassVar[dict[str, Any]] = {
        "views": [
            {
                "badges": ["sensor.x_temperature"],
                "cards": [{"type": "tile", "entity": "sensor.x_temperature"}],
            }
        ]
    }

    def test_suffix_pattern_must_match_to_the_end(self):
        assert _find_cards_in_config(self.CONFIG, entity_id="sensor.*_temp") == []
        matches = _find_cards_in_config(self.CONFIG, entity_id="sensor.*_temperature")
        assert [m["card_type"] for m in matches] == ["badge", "tile"]

    def test_star_never_matches_an_empty_entity(self):
        config = {"views": [{"cards": [{"type": "tile", "entity": ""}]}]}
        assert _find_cards_in_config(config, entity_id="*") == []

    def test_regex_characters_are_literal(self):
        assert _find_cards_in_config(self.CONFIG, entity_id="sensor.(*") == []
        assert _find_cards_in_config(self.CONFIG, entity_id="sensor.x+*") == []


class TestBlankCriteria:
    """A blank criterion counts as not given."""

    def test_blank_criterion_beside_a_real_one_is_ignored(self):
        config = {
            "views": [
                {
                    "badges": ["light.a"],
                    "cards": [{"type": "tile", "entity": "light.a"}],
                }
            ]
        }
        assert _find_cards_in_config(
            config, entity_id="light.a", heading=""
        ) == _find_cards_in_config(config, entity_id="light.a")


class TestBadgeCriteria:
    """card_type='badge' alone lists every badge; a heading excludes them."""

    CONFIG: ClassVar[dict[str, Any]] = {
        "views": [
            {
                "badges": ["light.a", {"type": "entity", "entity": "sensor.b"}],
                "cards": [{"type": "tile", "entity": "light.a", "title": "T"}],
            }
        ]
    }

    def test_badge_card_type_alone_lists_every_badge(self):
        matches = _find_cards_in_config(self.CONFIG, card_type="badge")
        assert [m["jq_path"] for m in matches] == [
            ".views[0].badges[0]",
            ".views[0].badges[1]",
        ]
        assert all("matched" not in m for m in matches)

    def test_malformed_badge_entries_are_not_listed(self):
        config = {"views": [{"badges": [None, 5, "", "sensor.ok"], "cards": []}]}
        matches = _find_cards_in_config(config, card_type="badge")
        assert [m["jq_path"] for m in matches] == [".views[0].badges[3]"]

    def test_badge_search_does_not_warn_about_picture_elements(self):
        config = {
            "views": [
                {
                    "badges": ["light.a"],
                    "cards": [
                        {"type": "picture-elements", "elements": [{"type": "icon"}]}
                    ],
                }
            ]
        }
        result = DashboardConfigTools._build_search_result(
            [{"url_path": "d", "config": config}],
            criteria=_SearchCriteria(card_type="badge"),
            include_config=False,
            url_path="d",
        )
        assert result["match_count"] == 1
        assert "warnings" not in result

    def test_heading_excludes_badges(self):
        matches = _find_cards_in_config(self.CONFIG, entity_id="light.a", heading="t")
        assert [m["card_type"] for m in matches] == ["tile"]


class TestPictureElementsDisclosure:
    """query reads picture-elements text; the card criteria are warned about."""

    CONFIG: ClassVar[dict[str, Any]] = {
        "views": [
            {
                "cards": [
                    {
                        "type": "picture-elements",
                        "elements": [{"type": "state-badge", "entity": "sensor.pe"}],
                    }
                ]
            }
        ]
    }

    def _search(self, **criteria: str) -> dict[str, Any]:
        return DashboardConfigTools._build_search_result(
            [{"url_path": "d", "config": self.CONFIG}],
            criteria=_SearchCriteria(**criteria),
            include_config=False,
        )

    def test_query_finds_element_text_without_a_warning(self):
        result = self._search(query="sensor.pe")
        assert result["matches"][0]["matched"] == [
            {"field": "entity", "value": "sensor.pe"}
        ]
        assert "warnings" not in result

    def test_card_criteria_warn_that_elements_are_not_matched(self):
        result = self._search(entity_id="sensor.pe", query="sensor.pe")
        assert result["match_count"] == 0
        assert any("picture-elements" in w for w in result["warnings"])
