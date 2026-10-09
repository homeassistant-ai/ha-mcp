"""Card search reach and the ``query`` criterion of ``_find_cards_in_config``.

Cards that custom cards nest under keys of their own (issue #2694), and the
text criterion the single search applies alongside entity_id / card_type /
heading.
"""

from typing import Any, ClassVar

from ha_mcp.tools.tools_config_dashboards import _find_cards_in_config


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
        assert len(matches) == 3


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
