"""Unit tests for compact ha_call_service result projection (issue #1446).

The OP reported that ha_call_service returns a state record for every entity
affected by a service call — for nested HA-native groups with a WLED member,
that's 16 state objects with a ~250-entry effect_list emitted four times.
``compact_service_result`` trims this to the targeted entity's record with
heavy attributes stripped; the tool exposes ``verbose=True`` and
``result_fields``/``result_attribute_keys`` for explicit control.
"""

from ha_mcp.tools.tools_service import ServiceTools
from ha_mcp.tools.util_helpers import compact_service_result


def _make_record(entity_id: str, *, effect_list_size: int = 0) -> dict:
    record: dict = {
        "entity_id": entity_id,
        "state": "on",
        "attributes": {
            "brightness": 51,
            "color_mode": "hs",
            "rgb_color": [255, 121, 0],
            "friendly_name": entity_id.split(".", 1)[-1].replace("_", " "),
        },
        "last_changed": "2026-05-25T20:57:04.183180+00:00",
        "last_reported": "2026-05-25T21:18:31.413215+00:00",
        "last_updated": "2026-05-25T21:18:31.413215+00:00",
        "context": {"id": "01KSGG1VB9", "parent_id": None, "user_id": "abc"},
    }
    if effect_list_size:
        record["attributes"]["effect_list"] = [
            f"Effect {i}" for i in range(effect_list_size)
        ]
    return record


class TestCompactServiceResult:
    """Compaction strips metadata + heavy lists and filters to target entity."""

    def test_filters_propagation_chain_to_target(self):
        """OP case: leaf entity targeted, parent groups propagated — keep only target."""
        result = [
            _make_record("light.bedroom_desk_inner_left"),
            _make_record("light.bedroom_desk"),  # parent group
            _make_record("light.bedroom_desk_lights"),  # grandparent
            _make_record("light.lights_in_bedroom", effect_list_size=250),  # WLED
        ]
        compacted = compact_service_result(result, "light.bedroom_desk_inner_left")
        assert isinstance(compacted, list)
        assert len(compacted) == 1
        assert compacted[0]["entity_id"] == "light.bedroom_desk_inner_left"

    def test_strips_context_and_timestamps(self):
        result = [_make_record("light.kitchen")]
        compacted = compact_service_result(result, "light.kitchen")
        record = compacted[0]
        assert "context" not in record
        assert "last_changed" not in record
        assert "last_reported" not in record
        assert "last_updated" not in record
        # state + attributes preserved
        assert record["state"] == "on"
        assert record["attributes"]["brightness"] == 51

    def test_strips_effect_list_from_attributes(self):
        """WLED's ~250-entry effect_list is the worst offender — drop it by default."""
        result = [_make_record("light.wled", effect_list_size=250)]
        compacted = compact_service_result(result, "light.wled")
        attrs = compacted[0]["attributes"]
        assert "effect_list" not in attrs
        # Other useful attrs preserved
        assert attrs["brightness"] == 51
        assert attrs["rgb_color"] == [255, 121, 0]

    def test_keeps_full_list_when_target_unmatched(self):
        """If HA only returned propagated parents, keep them all (don't return [])."""
        result = [
            _make_record("light.parent_group"),
            _make_record("light.grandparent_group"),
        ]
        compacted = compact_service_result(result, "light.nonexistent_target")
        assert len(compacted) == 2

    def test_keeps_full_list_when_entity_id_is_none(self):
        """Domain-wide / list-target calls — agent expects all affected entities."""
        result = [_make_record("light.a"), _make_record("light.b")]
        compacted = compact_service_result(result, None)
        assert len(compacted) == 2
        # Per-record compaction still applies to each
        for record in compacted:
            assert "context" not in record

    def test_empty_list_passthrough(self):
        assert compact_service_result([], "light.kitchen") == []

    def test_non_list_passthrough(self):
        """``return_response=True`` services return dicts — leave them alone."""
        as_dict = {"service_response": {"foo": "bar"}}
        assert compact_service_result(as_dict, "light.kitchen") is as_dict
        assert compact_service_result(None, "light.kitchen") is None

    def test_non_dict_records_passthrough(self):
        """Malformed entries (e.g. a stray string) are kept unchanged."""
        result = ["unexpected", _make_record("light.kitchen")]
        compacted = compact_service_result(result, "light.kitchen")
        # entity_id filter only matches the dict record, so result has just one
        assert len(compacted) == 1
        assert compacted[0]["entity_id"] == "light.kitchen"


class TestProjectServiceResult:
    """ServiceTools._project_service_result orchestrates verbose / explicit / default."""

    def test_verbose_returns_raw_result_unchanged(self):
        result = [_make_record("light.kitchen", effect_list_size=250)]
        projected, warnings = ServiceTools._project_service_result(
            result,
            entity_id="light.kitchen",
            verbose=True,
            fields=None,
            attribute_keys=None,
        )
        assert projected is result
        assert warnings == []
        # context + effect_list intact in verbose mode
        assert projected[0]["context"]["id"] == "01KSGG1VB9"
        assert len(projected[0]["attributes"]["effect_list"]) == 250

    def test_default_applies_compaction(self):
        result = [
            _make_record("light.target"),
            _make_record("light.parent_group", effect_list_size=250),
        ]
        projected, warnings = ServiceTools._project_service_result(
            result,
            entity_id="light.target",
            verbose=False,
            fields=None,
            attribute_keys=None,
        )
        assert len(projected) == 1
        assert projected[0]["entity_id"] == "light.target"
        assert "context" not in projected[0]
        assert warnings == []

    def test_explicit_fields_applies_per_record_projection(self):
        """fields=['entity_id', 'state'] keeps only those keys; no compaction."""
        result = [
            _make_record("light.target"),
            _make_record("light.parent_group"),
        ]
        projected, warnings = ServiceTools._project_service_result(
            result,
            entity_id="light.target",
            verbose=False,
            fields=["entity_id", "state"],
            attribute_keys=None,
        )
        # Both records retained (no compaction filter when fields explicit)
        assert len(projected) == 2
        for record in projected:
            assert set(record.keys()) == {"entity_id", "state"}
        assert warnings == []

    def test_explicit_attribute_keys_typo_emits_single_warning(self):
        """Same typo across N records should emit one warning, not N."""
        result = [
            _make_record("light.a"),
            _make_record("light.b"),
            _make_record("light.c"),
        ]
        projected, warnings = ServiceTools._project_service_result(
            result,
            entity_id=None,
            verbose=False,
            fields=None,
            attribute_keys=["nonexistent_attr"],
        )
        assert len(projected) == 3
        for record in projected:
            assert record["attributes"] == {}
        # Deduplicated — exactly one warning, not three
        assert len(warnings) == 1
        assert "nonexistent_attr" in warnings[0]

    def test_non_list_result_passthrough(self):
        """Dict result (return_response services) passes through every mode."""
        result = {"service_response": {"forecast": []}}
        projected, warnings = ServiceTools._project_service_result(
            result,
            entity_id=None,
            verbose=False,
            fields=None,
            attribute_keys=None,
        )
        assert projected is result
        assert warnings == []
