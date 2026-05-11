"""Unit tests for _project_fields helper in tools_search (issue #1199)."""

from ha_mcp.tools.tools_search import _project_fields


class TestProjectFields:
    """Test the _project_fields module-level helper."""

    def test_none_fields_returns_data_unchanged(self):
        data = {"success": True, "results": [1, 2], "count": 2}
        result = _project_fields(data, None)
        assert result is data

    def test_single_field_plus_success_retained(self):
        data = {"success": True, "results": [1, 2], "count": 2, "query": "light"}
        result = _project_fields(data, ["results"])
        assert set(result.keys()) == {"success", "results"}
        assert result["results"] == [1, 2]

    def test_multiple_fields_retained(self):
        data = {"success": True, "results": [], "count": 0, "query": "x", "has_more": False}
        result = _project_fields(data, ["results", "count"])
        assert set(result.keys()) == {"success", "results", "count"}

    def test_success_always_included_even_if_not_in_fields(self):
        data = {"success": True, "results": [], "count": 0}
        result = _project_fields(data, ["count"])
        assert "success" in result
        assert result["success"] is True

    def test_unknown_field_silently_omitted(self):
        data = {"success": True, "results": []}
        result = _project_fields(data, ["nonexistent"])
        assert set(result.keys()) == {"success"}

    def test_empty_fields_list_returns_only_success(self):
        data = {"success": True, "results": [], "count": 0}
        result = _project_fields(data, [])
        assert set(result.keys()) == {"success"}

    def test_success_in_fields_not_duplicated(self):
        data = {"success": True, "results": []}
        result = _project_fields(data, ["success", "results"])
        assert list(result.keys()).count("success") == 1

    def test_empty_data_with_none_fields(self):
        data: dict = {}
        result = _project_fields(data, None)
        assert result == {}

    def test_projection_does_not_mutate_original(self):
        data = {"success": True, "results": [1], "count": 1}
        _project_fields(data, ["results"])
        assert "count" in data
