"""Unit tests for compact logbook entry filtering (issue #683)."""

from ha_mcp.tools.tools_utility import COMPACT_LOGBOOK_FIELDS, _compact_logbook_entries


class TestCompactLogbookEntries:
    """Test _compact_logbook_entries function."""

    def test_strips_attribute_dictionaries(self):
        """Full attribute dicts should be removed in compact mode."""
        entries = [
            {
                "when": "2026-02-28T12:00:00Z",
                "entity_id": "light.living_room",
                "state": "on",
                "name": "Living Room Light",
                "message": "turned on",
                "domain": "light",
                "context_id": "abc123",
                # These bulky fields should be stripped:
                "attributes": {"brightness": 255, "color_temp": 400, "supported_features": 63},
                "icon": "mdi:lightbulb",
                "source": "automation.motion",
            },
        ]

        result = _compact_logbook_entries(entries)

        assert len(result) == 1
        entry = result[0]
        # Essential fields preserved
        assert entry["when"] == "2026-02-28T12:00:00Z"
        assert entry["entity_id"] == "light.living_room"
        assert entry["state"] == "on"
        assert entry["name"] == "Living Room Light"
        assert entry["message"] == "turned on"
        assert entry["domain"] == "light"
        assert entry["context_id"] == "abc123"
        # Bulky fields stripped
        assert "attributes" not in entry
        assert "icon" not in entry
        # source preserved (causality info for debugging)
        assert entry["source"] == "automation.motion"

    def test_preserves_all_fields_when_only_essential(self):
        """Entries with only essential fields should be unchanged."""
        entries = [
            {
                "when": "2026-02-28T12:00:00Z",
                "entity_id": "sensor.temp",
                "state": "22.5",
                "name": "Temperature",
            },
        ]

        result = _compact_logbook_entries(entries)

        assert len(result) == 1
        assert result[0] == entries[0]

    def test_handles_empty_list(self):
        """Empty list should return empty list."""
        assert _compact_logbook_entries([]) == []

    def test_filters_non_dict_entries(self):
        """Non-dict entries should be filtered out."""
        entries = [
            {"when": "2026-02-28T12:00:00Z", "entity_id": "light.test", "state": "on"},
            "not a dict",
            42,
            None,
            {"when": "2026-02-28T12:01:00Z", "entity_id": "light.test", "state": "off"},
        ]

        result = _compact_logbook_entries(entries)

        assert len(result) == 2
        assert result[0]["state"] == "on"
        assert result[1]["state"] == "off"

    def test_multiple_entries_all_stripped(self):
        """Multiple entries should all be stripped consistently."""
        entries = [
            {
                "when": f"2026-02-28T12:00:0{i}Z",
                "entity_id": "binary_sensor.motion",
                "state": "on" if i % 2 == 0 else "off",
                "name": "Motion Sensor",
                "attributes": {"device_class": "motion", "friendly_name": "Motion Sensor"},
                "old_state": {"state": "off"},
                "new_state": {"state": "on"},
            }
            for i in range(10)
        ]

        result = _compact_logbook_entries(entries)

        assert len(result) == 10
        for entry in result:
            assert "attributes" not in entry
            assert "old_state" not in entry
            assert "new_state" not in entry
            assert "when" in entry
            assert "entity_id" in entry

    def test_compact_fields_constant_contains_expected_fields(self):
        """Verify the constant has the expected essential fields."""
        expected = {"when", "entity_id", "state", "name", "message", "domain", "context_id", "source"}
        assert expected == COMPACT_LOGBOOK_FIELDS

    def test_missing_essential_fields_not_fabricated(self):
        """Entries missing some essential fields should not have them added."""
        entries = [
            {"when": "2026-02-28T12:00:00Z", "extra_field": "should_be_stripped"},
        ]

        result = _compact_logbook_entries(entries)

        assert len(result) == 1
        assert result[0] == {"when": "2026-02-28T12:00:00Z"}
