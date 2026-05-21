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
    """Test _project_entity helper.

    _project_entity now returns (record, warning | None).  Tests unpack the
    tuple; the warning value is checked where relevant.
    """

    def test_none_fields_and_none_attribute_keys_returns_unchanged(self):
        result, warn = _project_entity(dict(_ENTITY_RECORD), None, None)
        assert warn is None
        assert set(result.keys()) == {
            "entity_id",
            "state",
            "attributes",
            "last_changed",
            "last_updated",
            "context",
        }

    def test_fields_keeps_only_specified_keys(self):
        result, warn = _project_entity(dict(_ENTITY_RECORD), ["state"], None)
        assert warn is None
        assert set(result.keys()) == {"state"}
        assert result["state"] == "on"

    def test_fields_multiple_keys(self):
        result, warn = _project_entity(
            dict(_ENTITY_RECORD), ["state", "entity_id"], None
        )
        assert warn is None
        assert set(result.keys()) == {"state", "entity_id"}

    def test_fields_unknown_key_silently_omitted(self):
        result, warn = _project_entity(dict(_ENTITY_RECORD), ["nonexistent"], None)
        assert warn is None
        assert result == {}

    def test_attribute_keys_filters_attributes_subdict(self):
        result, warn = _project_entity(dict(_ENTITY_RECORD), None, ["brightness"])
        assert warn is None
        assert result["attributes"] == {"brightness": 200}
        assert "color_temp" not in result["attributes"]
        assert "friendly_name" not in result["attributes"]

    def test_attribute_keys_unknown_emits_warning(self):
        """Unknown attribute_keys returns empty attrs AND a warning string."""
        result, warn = _project_entity(dict(_ENTITY_RECORD), None, ["nonexistent_attr"])
        assert result["attributes"] == {}
        assert warn is not None, "Expected warning when attribute filter empties attrs"
        assert "attribute_keys" in warn
        assert "brightness" in warn  # available-keys hint

    def test_attribute_keys_empty_list_no_warning(self):
        """attribute_keys=[] — original attrs is non-empty but user asked for nothing.

        The empty list explicitly requests zero keys; this is not a typo.
        The guard fires only when the *original* attrs was non-empty AND the
        *filter* produced empty — but an empty attribute_keys set means the
        caller deliberately asked for an empty sub-dict, so no warning.
        """
        result, warn = _project_entity(dict(_ENTITY_RECORD), None, [])
        assert result["attributes"] == {}
        # Empty attribute_keys is an explicit "keep nothing" request — no warning
        assert warn is None

    def test_fields_and_attribute_keys_combined(self):
        result, warn = _project_entity(
            dict(_ENTITY_RECORD), ["state", "attributes"], ["brightness"]
        )
        assert warn is None
        assert set(result.keys()) == {"state", "attributes"}
        assert result["attributes"] == {"brightness": 200}

    def test_attribute_keys_no_effect_when_attributes_not_in_fields(self):
        result, warn = _project_entity(dict(_ENTITY_RECORD), ["state"], ["brightness"])
        # attribute_keys is set but attributes is not in fields — no attrs to filter
        # (no typo-guard warning here; the outer attribute_keys_no_effect path handles it)
        assert warn is None
        assert set(result.keys()) == {"state"}
        assert "attributes" not in result

    def test_does_not_mutate_original(self):
        original = dict(_ENTITY_RECORD)
        _project_entity(original, ["state"], ["brightness"])
        assert "last_changed" in original
        assert original["attributes"]["color_temp"] == 3500

    def test_non_dict_record_returned_unchanged(self):
        # Defensive: error paths may pass None/non-dict; helper must not raise.
        result, warn = _project_entity(None, ["state"], None)  # type: ignore[arg-type]
        assert result is None
        assert warn is None

    def test_mixed_known_unknown_attribute_keys_no_warning(self):
        """Partial match (brightness found) — guard does not fire."""
        result, warn = _project_entity(
            dict(_ENTITY_RECORD), None, ["brightness", "typo_key"]
        )
        assert warn is None
        assert result["attributes"] == {"brightness": 200}
