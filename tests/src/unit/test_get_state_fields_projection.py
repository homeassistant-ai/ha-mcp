"""Unit tests for fields= and attribute_keys= projection in ha_get_state (issue #1199)."""

from ha_mcp.tools.tools_search import _project_entity


_ENTITY_RECORD = {
    "entity_id": "light.kitchen",
    "state": "on",
    "attributes": {
        "brightness": 200,
        "color_temp": 3500,
        "friendly_name": "Kitchen Light",
    },
    "last_changed": "2025-01-01T00:00:00+00:00",
    "last_updated": "2025-01-01T00:00:00+00:00",
    "context": {"id": "abc", "parent_id": None, "user_id": None},
}


class TestProjectEntity:
    """Test _project_entity helper."""

    def test_none_fields_and_none_attribute_keys_returns_unchanged(self):
        result = _project_entity(dict(_ENTITY_RECORD), None, None)
        assert set(result.keys()) == {"entity_id", "state", "attributes", "last_changed", "last_updated", "context"}

    def test_fields_keeps_only_specified_keys(self):
        result = _project_entity(dict(_ENTITY_RECORD), ["state"], None)
        assert set(result.keys()) == {"state"}
        assert result["state"] == "on"

    def test_fields_multiple_keys(self):
        result = _project_entity(dict(_ENTITY_RECORD), ["state", "entity_id"], None)
        assert set(result.keys()) == {"state", "entity_id"}

    def test_fields_unknown_key_silently_omitted(self):
        result = _project_entity(dict(_ENTITY_RECORD), ["nonexistent"], None)
        assert result == {}

    def test_attribute_keys_filters_attributes_subdict(self):
        result = _project_entity(dict(_ENTITY_RECORD), None, ["brightness"])
        assert result["attributes"] == {"brightness": 200}
        assert "color_temp" not in result["attributes"]
        assert "friendly_name" not in result["attributes"]

    def test_attribute_keys_unknown_silently_absent(self):
        result = _project_entity(dict(_ENTITY_RECORD), None, ["nonexistent_attr"])
        assert result["attributes"] == {}

    def test_fields_and_attribute_keys_combined(self):
        result = _project_entity(dict(_ENTITY_RECORD), ["state", "attributes"], ["brightness"])
        assert set(result.keys()) == {"state", "attributes"}
        assert result["attributes"] == {"brightness": 200}

    def test_attribute_keys_no_effect_when_attributes_not_in_fields(self):
        result = _project_entity(dict(_ENTITY_RECORD), ["state"], ["brightness"])
        assert set(result.keys()) == {"state"}
        assert "attributes" not in result

    def test_attribute_keys_empty_list_returns_empty_attributes(self):
        result = _project_entity(dict(_ENTITY_RECORD), None, [])
        assert result["attributes"] == {}

    def test_does_not_mutate_original(self):
        original = dict(_ENTITY_RECORD)
        _project_entity(original, ["state"], ["brightness"])
        assert "last_changed" in original
        assert original["attributes"]["color_temp"] == 3500


