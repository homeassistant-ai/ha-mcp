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
