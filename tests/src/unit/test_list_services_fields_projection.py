"""Unit tests for fields= projection in ha_list_services (issue #1199)."""

from ha_mcp.tools.util_helpers import project_fields

_SERVICES_RESULT = {
    "success": True,
    "domains": ["light", "switch"],
    "services": {
        "light.turn_on": {"name": "Turn on", "description": "Turn on a light."},
        "light.turn_off": {"name": "Turn off", "description": "Turn off a light."},
    },
    "total_count": 2,
    "count": 2,
    "offset": 0,
    "limit": 50,
    "has_more": False,
    "next_offset": None,
    "detail_level": "summary",
    "filters_applied": {"domain": None, "query": None},
}


class TestListServicesProjection:
    """Test fields= projection applied to ha_list_services responses."""

    def test_none_fields_returns_full_response(self):
        result = project_fields(dict(_SERVICES_RESULT), None)
        assert set(result.keys()) == set(_SERVICES_RESULT.keys())

    def test_single_field_services_only(self):
        result = project_fields(dict(_SERVICES_RESULT), ["services"])
        assert set(result.keys()) == {"success", "services"}
        assert result["services"] == _SERVICES_RESULT["services"]

    def test_multiple_fields_retained(self):
        result = project_fields(dict(_SERVICES_RESULT), ["services", "domains"])
        assert set(result.keys()) == {"success", "services", "domains"}

    def test_success_always_retained(self):
        result = project_fields(dict(_SERVICES_RESULT), ["domains"])
        assert "success" in result
        assert result["success"] is True

    def test_unknown_field_silently_dropped(self):
        result = project_fields(dict(_SERVICES_RESULT), ["nonexistent"])
        assert result == {"success": True}

    def test_csv_string_input(self):
        result = project_fields(dict(_SERVICES_RESULT), "services,domains")
        assert set(result.keys()) == {"success", "services", "domains"}

    def test_json_array_string_input(self):
        result = project_fields(dict(_SERVICES_RESULT), '["services"]')
        assert set(result.keys()) == {"success", "services"}

    def test_empty_list_returns_only_success(self):
        result = project_fields(dict(_SERVICES_RESULT), [])
        assert set(result.keys()) == {"success"}

    def test_does_not_mutate_original(self):
        original = dict(_SERVICES_RESULT)
        project_fields(original, ["services"])
        assert "domains" in original
        assert "detail_level" in original
