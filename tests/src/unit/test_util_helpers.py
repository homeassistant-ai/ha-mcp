"""Unit tests for util_helpers module."""

import pytest

from ha_mcp.tools.util_helpers import parse_json_param, parse_string_list_param


class TestParseStringListParam:
    """Test parse_string_list_param function."""

    def test_none_returns_none(self):
        """None input returns None."""
        assert parse_string_list_param(None) is None

    def test_list_of_strings_returns_as_is(self):
        """A list of strings is returned as-is."""
        input_list = ["automation", "script"]
        result = parse_string_list_param(input_list)
        assert result == ["automation", "script"]

    def test_empty_list_returns_empty(self):
        """An empty list is returned as-is."""
        assert parse_string_list_param([]) == []

    def test_json_array_string_parsed(self):
        """A JSON array string is parsed into a list."""
        result = parse_string_list_param('["automation", "script"]')
        assert result == ["automation", "script"]

    def test_json_array_single_item(self):
        """A JSON array with single item is parsed."""
        result = parse_string_list_param('["automation"]')
        assert result == ["automation"]

    def test_json_array_empty(self):
        """An empty JSON array is parsed."""
        result = parse_string_list_param("[]")
        assert result == []

    def test_invalid_json_raises_error(self):
        """Invalid JSON string raises ValueError."""
        with pytest.raises(ValueError, match="Invalid JSON"):
            parse_string_list_param("not valid json")

    def test_json_object_raises_error(self):
        """JSON object (not array) raises ValueError."""
        with pytest.raises(ValueError, match="must be a JSON array"):
            parse_string_list_param('{"key": "value"}')

    def test_json_number_raises_error(self):
        """JSON number raises ValueError."""
        with pytest.raises(ValueError, match="must be a JSON array"):
            parse_string_list_param("123")

    def test_json_array_with_non_strings_raises_error(self):
        """JSON array with non-string elements raises ValueError."""
        with pytest.raises(ValueError, match="must be a JSON array of strings"):
            parse_string_list_param('[1, 2, 3]')

    def test_list_with_non_strings_raises_error(self):
        """List with non-string elements raises ValueError."""
        with pytest.raises(ValueError, match="must be a list of strings"):
            parse_string_list_param([1, 2, 3])

    def test_mixed_list_raises_error(self):
        """Mixed list (strings and non-strings) raises ValueError."""
        with pytest.raises(ValueError, match="must be a list of strings"):
            parse_string_list_param(["valid", 123])

    def test_param_name_in_error(self):
        """Custom param_name appears in error messages."""
        with pytest.raises(ValueError, match="search_types"):
            parse_string_list_param('{"bad": "json"}', "search_types")


class TestParseJsonParam:
    """Test parse_json_param function."""

    def test_none_returns_none(self):
        """None input returns None."""
        assert parse_json_param(None) is None

    def test_dict_returns_as_is(self):
        """A dict is returned as-is."""
        input_dict = {"key": "value"}
        result = parse_json_param(input_dict)
        assert result == {"key": "value"}

    def test_list_returns_as_is(self):
        """A list is returned as-is."""
        input_list = ["a", "b"]
        result = parse_json_param(input_list)
        assert result == ["a", "b"]

    def test_json_object_string_parsed(self):
        """A JSON object string is parsed into a dict."""
        result = parse_json_param('{"key": "value"}')
        assert result == {"key": "value"}

    def test_json_array_string_parsed(self):
        """A JSON array string is parsed into a list."""
        result = parse_json_param('["a", "b"]')
        assert result == ["a", "b"]

    def test_invalid_json_raises_error(self):
        """Invalid JSON string raises ValueError."""
        with pytest.raises(ValueError, match="Invalid JSON"):
            parse_json_param("not valid json")

    def test_json_primitive_raises_error(self):
        """JSON primitive (number/string) raises ValueError."""
        with pytest.raises(ValueError, match="must be a JSON object or array"):
            parse_json_param('"just a string"')

    def test_param_name_in_error(self):
        """Custom param_name appears in error messages."""
        with pytest.raises(ValueError, match="config"):
            parse_json_param("invalid", "config")
