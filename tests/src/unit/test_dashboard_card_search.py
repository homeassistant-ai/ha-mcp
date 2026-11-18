"""Nested-card reach and the ``query`` criterion of the dashboard card search.

Cards that custom cards nest under keys of their own (issue #2694), and the
text criterion the single search applies alongside entity_id / card_type /
heading.
"""

from typing import Any, ClassVar

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools

from ._dashboard_search_helpers import SEARCH_URL_PATH, store_dashboard


@pytest.fixture
def search_dashboard(hass: HomeAssistant, core_client: HomeAssistantClient):

    async def search(config, *, url_path=SEARCH_URL_PATH, **criteria):
        await store_dashboard(hass, core_client, config)
        return await DashboardConfigTools(core_client).ha_config_get_dashboard(
            url_path=SEARCH_URL_PATH if url_path is not None else None,
            include_config=False,
            **criteria,
        )

    return search


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

    async def test_finds_cards_under_custom_keys(self, search_dashboard):
        matches = (await search_dashboard(self.CONFIG, entity_id="light.x"))["matches"]
        assert [(m["jq_path"], m["card_type"]) for m in matches] == [
            (".views[0].cards[0].groups[0].cards[0].card", "tile"),
            (".views[0].cards[0].groups[0].cards[1].card", "entities"),
            (".views[0].cards[0].groups[0].cards[2]", "tile"),
            (".views[0].cards[1].tabs[0].card", "tile"),
        ]
        assert (
            matches[0]["python_path"]
            == "['views'][0]['cards'][0]['groups'][0]['cards'][0]['card']"
        )

    async def test_finds_custom_card_type_by_type(self, search_dashboard):
        matches = (await search_dashboard(self.CONFIG, card_type="tile"))["matches"]
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

    async def test_query_matches_any_value_the_card_holds(self, search_dashboard):
        matches = (await search_dashboard(self.CONFIG, query="input_number.limit"))[
            "matches"
        ]
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

    async def test_query_does_not_attribute_nested_card_strings_to_the_parent(
        self, search_dashboard
    ):
        matches = (await search_dashboard(self.CONFIG, query="climate"))["matches"]
        assert [m["jq_path"] for m in matches] == [".views[0].cards[0]"]

    async def test_query_reads_non_card_objects_inside_a_card(self, search_dashboard):
        matches = (await search_dashboard(self.CONFIG, query="color: red"))["matches"]
        assert matches[0]["jq_path"] == ".views[0].cards[1]"
        assert matches[0]["matched"] == [
            {"field": "style", "value": "ha-card { color: red; }"}
        ]

    async def test_query_and_card_type_are_combined(self, search_dashboard):
        matches = (
            await search_dashboard(
                self.CONFIG, card_type="tile", query="input_number.limit"
            )
        )["matches"]
        assert [m["jq_path"] for m in matches] == [".views[0].cards[0].cards[1]"]

    async def test_query_and_entity_id_are_combined(self, search_dashboard):
        assert (await search_dashboard(self.CONFIG, entity_id="light.a", query="desk"))[
            "matches"
        ]
        assert not (
            await search_dashboard(self.CONFIG, entity_id="light.a", query="kitchen")
        )["matches"]

    async def test_query_matches_badges(self, search_dashboard):
        matches = (await search_dashboard(self.CONFIG, query="sensor."))["matches"]
        badge_matches = [m for m in matches if m["card_type"] == "badge"]
        assert [m["matched"] for m in badge_matches] == [
            [{"field": "badges", "value": "sensor.outdoor"}],
            [{"field": "entity", "value": "sensor.hall"}],
        ]

    async def test_no_query_leaves_out_matched(self, search_dashboard):
        matches = (await search_dashboard(self.CONFIG, card_type="tile"))["matches"]
        assert "matched" not in matches[0]


class TestSearchSurvivesUnusualConfigs:
    """Shapes HA stores that must neither hide cards nor fail a search."""

    async def test_untyped_view_with_sections_is_searched(self, search_dashboard):
        config = {
            "views": [
                {"sections": [{"cards": [{"type": "tile", "entity": "light.a"}]}]}
            ]
        }
        matches = (await search_dashboard(config, entity_id="light.a"))["matches"]
        assert [m["jq_path"] for m in matches] == [".views[0].sections[0].cards[0]"]

    async def test_wildcard_entity_search_skips_rows_without_an_entity(
        self, search_dashboard
    ):
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
        assert (
            len((await search_dashboard(config, entity_id="light.*"))["matches"]) == 1
        )

    async def test_heading_search_reads_non_string_titles(self, search_dashboard):
        config = {
            "views": [{"cards": [{"type": "entities", "title": 5, "entities": []}]}]
        }
        assert len((await search_dashboard(config, heading="5"))["matches"]) == 1
        assert (await search_dashboard(config, heading="x"))["matches"] == []

    async def test_null_badges_and_sections_do_not_fail_the_search(
        self, search_dashboard
    ):
        config = {
            "views": [
                {
                    "badges": None,
                    "sections": None,
                    "cards": [{"type": "tile", "entity": "light.a"}],
                }
            ]
        }
        assert len((await search_dashboard(config, query="light.a"))["matches"]) == 1

    async def test_typed_objects_outside_card_slots_are_not_cards(
        self, search_dashboard
    ):
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
        assert (await search_dashboard(config, card_type="light-brightness"))[
            "matches"
        ] == []
        assert (await search_dashboard(config, card_type="attribute"))["matches"] == []
        row_owner = (await search_dashboard(config, entity_id="light.b"))["matches"]
        assert [m["jq_path"] for m in row_owner] == [".views[0].cards[1]"]
        feature_owner = (await search_dashboard(config, query="light-brightness"))[
            "matches"
        ]
        assert [m["jq_path"] for m in feature_owner] == [".views[0].cards[0]"]

    async def test_view_cards_and_section_cards_are_both_searched_once(
        self, search_dashboard
    ):
        config = {
            "views": [
                {
                    "type": "sections",
                    "cards": [{"type": "tile", "entity": "light.a"}],
                    "sections": [{"cards": [{"type": "tile", "entity": "light.a"}]}],
                }
            ]
        }
        matches = (await search_dashboard(config, entity_id="light.a"))["matches"]
        assert [m["jq_path"] for m in matches] == [
            ".views[0].cards[0]",
            ".views[0].sections[0].cards[0]",
        ]

    async def test_badge_with_a_non_string_entity_is_skipped(self, search_dashboard):
        config = {"views": [{"badges": [{"type": "entity", "entity": 5}], "cards": []}]}
        assert (await search_dashboard(config, entity_id="sensor.*"))["matches"] == []

    async def test_config_without_views_has_no_cards(self, search_dashboard):
        assert (await search_dashboard({"views": None}, card_type="tile"))[
            "matches"
        ] == []


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

    async def _search(self, search_dashboard, url_path: str | None) -> dict[str, Any]:
        return await search_dashboard(
            self.CONFIG, card_type="picture-elements", url_path=url_path
        )

    async def test_scoped_search_locations_name_no_dashboard(self, search_dashboard):
        result = await self._search(search_dashboard, "only")
        assert any(".views[0].cards[1].elements" in w for w in result["warnings"])
        assert not any("card-search:" in w for w in result["warnings"])

    async def test_search_across_dashboards_names_the_dashboard_in_locations(
        self, search_dashboard
    ):
        result = await self._search(search_dashboard, None)
        assert result["config_hash"] is None
        assert any(
            "card-search:.views[0].cards[1].elements" in w for w in result["warnings"]
        )


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

    async def test_suffix_pattern_must_match_to_the_end(self, search_dashboard):
        assert (await search_dashboard(self.CONFIG, entity_id="sensor.*_temp"))[
            "matches"
        ] == []
        matches = (
            await search_dashboard(self.CONFIG, entity_id="sensor.*_temperature")
        )["matches"]
        assert [m["card_type"] for m in matches] == ["badge", "tile"]

    async def test_star_never_matches_an_empty_entity(self, search_dashboard):
        config = {"views": [{"cards": [{"type": "tile", "entity": ""}]}]}
        assert (await search_dashboard(config, entity_id="*"))["matches"] == []

    async def test_regex_characters_are_literal(self, search_dashboard):
        assert (await search_dashboard(self.CONFIG, entity_id="sensor.(*"))[
            "matches"
        ] == []
        assert (await search_dashboard(self.CONFIG, entity_id="sensor.x+*"))[
            "matches"
        ] == []


class TestBlankCriteria:
    """A blank criterion counts as not given."""

    async def test_blank_criterion_beside_a_real_one_is_ignored(self, search_dashboard):
        config = {
            "views": [
                {
                    "badges": ["light.a"],
                    "cards": [{"type": "tile", "entity": "light.a"}],
                }
            ]
        }
        assert (await search_dashboard(config, entity_id="light.a", heading=""))[
            "matches"
        ] == (await search_dashboard(config, entity_id="light.a"))["matches"]


class TestBadgeCriteria:
    """card_type='badge' alone lists every well-formed badge; a heading excludes them."""

    CONFIG: ClassVar[dict[str, Any]] = {
        "views": [
            {
                "badges": ["light.a", {"type": "entity", "entity": "sensor.b"}],
                "cards": [{"type": "tile", "entity": "light.a", "title": "T"}],
            }
        ]
    }

    async def test_badge_card_type_alone_lists_every_badge(self, search_dashboard):
        matches = (await search_dashboard(self.CONFIG, card_type="badge"))["matches"]
        assert [m["jq_path"] for m in matches] == [
            ".views[0].badges[0]",
            ".views[0].badges[1]",
        ]
        assert all("matched" not in m for m in matches)

    UNREAD: ClassVar[dict[str, Any]] = {
        "views": [{"badges": [None, "  ", "sensor.ok"], "cards": []}]
    }

    async def _unread_search(
        self, search_dashboard, url_path: str | None, **criteria: str
    ) -> dict[str, Any]:
        return await search_dashboard(self.UNREAD, **criteria, url_path=url_path)

    @pytest.mark.parametrize(
        ("url_path", "criteria", "prefix"),
        [
            ("d", {"card_type": "badge"}, ""),
            (None, {"query": "sensor"}, "card-search:"),
        ],
    )
    async def test_unread_badge_entries_are_reported_not_listed(
        self,
        url_path: str | None,
        criteria: dict[str, str],
        prefix: str,
        search_dashboard,
    ):
        result = await self._unread_search(search_dashboard, url_path, **criteria)
        assert [m["jq_path"] for m in result["matches"]] == [".views[0].badges[2]"]
        [warning] = [
            item
            for item in result["warnings"]
            if item.startswith("Not searched as cards or badges")
        ]
        if url_path is None:
            assert any(
                "default dashboard was not searched" in item
                for item in result["warnings"]
            )
        assert f"{prefix}.views[0].badges[0]" in warning
        assert f"{prefix}.views[0].badges[1]" in warning

    async def test_out_of_scope_badges_are_not_reported(self, search_dashboard):
        result = await self._unread_search(search_dashboard, "d", card_type="tile")
        assert "warnings" not in result

    async def test_heading_excludes_badges(self, search_dashboard):
        matches = (
            await search_dashboard(self.CONFIG, entity_id="light.a", heading="t")
        )["matches"]
        assert [m["card_type"] for m in matches] == ["tile"]


class TestUnreadContainers:
    """Containers in a shape the search does not read are reported, never
    silently skipped."""

    CONFIG: ClassVar[dict[str, Any]] = {
        "views": [
            "not-a-view",
            {
                "badges": "light.a",
                "cards": ["not-a-card", {"type": "tile", "entity": "light.a"}],
                "sections": [5, {"cards": {"type": "tile"}}],
                "header": "not-a-header",
            },
        ]
    }

    async def test_every_unreadable_container_is_named(self, search_dashboard):
        result = await search_dashboard(self.CONFIG, entity_id="light.a", url_path="d")
        assert [m["jq_path"] for m in result["matches"]] == [".views[1].cards[1]"]
        [warning] = result["warnings"]
        for path in (
            ".views[0]",
            ".views[1].badges",
            ".views[1].header",
            ".views[1].cards[0]",
            ".views[1].sections[0]",
            ".views[1].sections[1].cards",
        ):
            assert path in warning

    async def test_badge_search_reports_only_badge_containers(self, search_dashboard):
        result = await search_dashboard(self.CONFIG, card_type="badge", url_path="d")
        [warning] = result["warnings"]
        assert ".views[1].badges" in warning
        assert ".views[1].cards[0]" not in warning


class TestPictureElementsDisclosure:
    """query reads picture-elements text; the card criteria are warned about."""

    CONFIG: ClassVar[dict[str, Any]] = {
        "views": [
            {
                "badges": ["light.a"],
                "cards": [
                    {
                        "type": "picture-elements",
                        "elements": [{"type": "state-badge", "entity": "sensor.pe"}],
                    }
                ],
            }
        ]
    }

    async def _search(self, search_dashboard, **criteria: str) -> dict[str, Any]:
        return await search_dashboard(self.CONFIG, **criteria)

    async def test_query_finds_element_text_without_a_warning(self, search_dashboard):
        result = await self._search(search_dashboard, query="sensor.pe")
        assert result["matches"][0]["matched"] == [
            {"field": "entity", "value": "sensor.pe"}
        ]
        assert "warnings" not in result

    @pytest.mark.parametrize(
        "criteria",
        [{"card_type": "badge"}, {"card_type": "badge", "entity_id": "light.a"}],
    )
    async def test_badge_search_does_not_warn(
        self, criteria: dict[str, str], search_dashboard
    ):
        result = await self._search(search_dashboard, **criteria)
        assert result["match_count"] == 1
        assert "warnings" not in result

    async def test_card_criteria_warn_that_elements_are_not_matched(
        self, search_dashboard
    ):
        result = await self._search(
            search_dashboard, entity_id="sensor.pe", query="sensor.pe"
        )
        assert result["match_count"] == 0
        assert any("picture-elements" in w for w in result["warnings"])


class TestReviewedSearchEdges:
    """Search edges where a result was wrong or a failure went unreported."""

    @staticmethod
    async def _search(
        search_dashboard, config: dict[str, Any], **criteria: str
    ) -> dict[str, Any]:
        return await search_dashboard(config, **criteria, url_path="d")

    async def test_deeply_nested_card_options_do_not_fail_the_search(
        self, search_dashboard
    ) -> None:
        options: dict[str, Any] = {"entity": "light.deep"}
        for _ in range(150):
            options = {"n": options}
        config = {
            "views": [
                {
                    "cards": [
                        {"type": "custom:x", "options": options},
                        {"type": "tile", "entity": "light.a"},
                    ]
                }
            ]
        }
        result = await self._search(search_dashboard, config, entity_id="light.a")
        assert [m["jq_path"] for m in result["matches"]] == [".views[0].cards[1]"]
        assert any("depth bound" in w for w in result["warnings"])

    async def test_badge_search_does_not_list_header_cards(
        self, search_dashboard
    ) -> None:
        config = {
            "views": [
                {
                    "badges": ["light.a"],
                    "header": {"card": {"type": "badge", "entity": "light.a"}},
                }
            ]
        }
        result = await self._search(search_dashboard, config, card_type="badge")
        assert [m["jq_path"] for m in result["matches"]] == [".views[0].badges[0]"]

    async def test_cards_mapping_is_read_as_text_without_a_warning(
        self, search_dashboard
    ) -> None:
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "vertical-stack",
                            "cards": {"type": "tile", "entity": "light.a"},
                        }
                    ]
                }
            ]
        }
        result = await self._search(search_dashboard, config, entity_id="light.a")
        assert result["match_count"] == 0
        assert "warnings" not in result
        assert (await self._search(search_dashboard, config, query="light.a"))[
            "match_count"
        ] == 1

    async def test_alarm_panel_states_list_is_read_as_text(
        self, search_dashboard
    ) -> None:
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "alarm-panel",
                            "entity": "alarm_control_panel.home",
                            "states": ["arm_home", "arm_away"],
                        }
                    ]
                }
            ]
        }
        result = await self._search(search_dashboard, config, card_type="alarm-panel")
        assert result["match_count"] == 1
        assert "warnings" not in result

    async def test_typed_cards_in_a_card_list_are_found(self, search_dashboard) -> None:
        config = {
            "views": [
                {
                    "cards": [
                        {
                            "type": "conditional",
                            "card": [{"type": "tile", "entity": "light.a"}, "x"],
                        }
                    ]
                }
            ]
        }
        result = await self._search(search_dashboard, config, entity_id="light.a")
        assert [m["jq_path"] for m in result["matches"]] == [
            ".views[0].cards[0].card[0]"
        ]
        assert "warnings" not in result
        assert (await self._search(search_dashboard, config, query="x"))[
            "match_count"
        ] == 1

    async def test_query_matches_number_and_boolean_options(
        self, search_dashboard
    ) -> None:
        config = {
            "views": [{"cards": [{"type": "grid", "columns": 3, "square": False}]}]
        }
        assert (await self._search(search_dashboard, config, query="3"))["matches"][0][
            "matched"
        ] == [{"field": "columns", "value": "3"}]
        assert (await self._search(search_dashboard, config, query="false"))[
            "match_count"
        ] == 1

    async def test_untyped_top_level_card_is_reported_as_not_searched(
        self, search_dashboard
    ) -> None:
        config = {"views": [{"cards": [{"entity": "light.a"}]}]}
        result = await self._search(search_dashboard, config, query="light.a")
        assert result["match_count"] == 0
        [warning] = result["warnings"]
        assert warning.startswith("Not searched as cards or badges")
        assert ".views[0].cards[0]" in warning
        assert "repair" not in warning

    async def test_query_reads_a_card_nested_in_a_badge(self, search_dashboard) -> None:
        config = {
            "views": [
                {
                    "badges": [
                        {
                            "type": "entity",
                            "entity": "sensor.door",
                            "card": {"type": "markdown", "content": "light.garage"},
                        }
                    ]
                }
            ]
        }
        result = await self._search(search_dashboard, config, query="light.garage")
        assert [m["jq_path"] for m in result["matches"]] == [".views[0].badges[0]"]

    async def test_depth_cut_inside_a_badge_is_reported(self, search_dashboard) -> None:
        options: dict[str, Any] = {"entity": "light.deep"}
        for _ in range(150):
            options = {"n": options}
        badge = {"type": "entity", "entity": "sensor.a", "options": options}
        result = await self._search(
            search_dashboard, {"views": [{"badges": [badge]}]}, query="light.deep"
        )
        assert any("depth bound" in w for w in result["warnings"])

    async def test_card_list_in_a_badge_is_read_as_text_without_a_warning(
        self, search_dashboard
    ) -> None:
        badge = {
            "type": "entity",
            "entity": "sensor.d",
            "card": [{"type": "markdown", "content": "light.a"}],
        }
        result = await self._search(
            search_dashboard, {"views": [{"badges": [badge]}]}, query="light.a"
        )
        assert [m["jq_path"] for m in result["matches"]] == [".views[0].badges[0]"]
        assert "warnings" not in result

    @pytest.mark.parametrize(
        "options",
        [
            {"styles": {"card": [{"height": "80px"}], "name": [{"color": "red"}]}},
            {"state": [{"value": "on", "styles": {"card": [{"height": "80px"}]}}]},
            {"custom_fields": {"styles": {"card": [{"height": "80px"}]}}},
            {"card": [{"height": "80px"}]},
            {"cards": {"height": "80px"}},
        ],
        ids=[
            "styles-card",
            "state-styles-card",
            "custom-field-styles-card",
            "bare-card-list",
            "cards-mapping",
        ],
    )
    async def test_card_keys_in_other_shapes_are_options_not_warnings(
        self, options: dict[str, Any], search_dashboard
    ) -> None:
        """A custom card's ``card``/``cards`` in another shape is its own option
        (button-card ``styles.card`` CSS, #2729): no warning, text searchable."""
        card = {"type": "custom:button-card", "entity": "light.a", **options}
        config = {"views": [{"cards": [card]}]}
        for criteria in ({"query": "80px"}, {"entity_id": "light.a"}):
            result = await self._search(search_dashboard, config, **criteria)
            assert [m["jq_path"] for m in result["matches"]] == [".views[0].cards[0]"]
            assert "warnings" not in result
