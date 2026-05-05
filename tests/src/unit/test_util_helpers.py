"""Unit tests for util_helpers module."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.util_helpers import (
    build_pagination_metadata,
    coerce_int_param,
    get_logger_levels,
    normalize_log_level,
    parse_json_param,
    parse_string_list_param,
)


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
            parse_string_list_param("[1, 2, 3]")

    def test_csv_rejected_without_allow_csv(self):
        """Comma-separated string raises ValueError without allow_csv."""
        with pytest.raises(ValueError, match="Invalid JSON"):
            parse_string_list_param("light,sensor")

    def test_csv_accepted_with_allow_csv(self):
        """Comma-separated string parsed when allow_csv=True."""
        result = parse_string_list_param("light,sensor", allow_csv=True)
        assert result == ["light", "sensor"]

    def test_csv_with_spaces_trimmed(self):
        """Comma-separated string with spaces is trimmed when allow_csv=True."""
        result = parse_string_list_param("light , sensor , switch", allow_csv=True)
        assert result == ["light", "sensor", "switch"]

    def test_csv_single_value(self):
        """Single value without commas returns single-element list when allow_csv=True."""
        result = parse_string_list_param("light", allow_csv=True)
        assert result == ["light"]

    def test_json_array_still_works_with_allow_csv(self):
        """JSON arrays still work when allow_csv=True."""
        result = parse_string_list_param('["light", "sensor"]', allow_csv=True)
        assert result == ["light", "sensor"]

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


class TestBuildPaginationMetadata:
    """Test build_pagination_metadata function."""

    def test_first_page_has_more(self):
        """First page with more results available."""
        result = build_pagination_metadata(
            total_count=100, offset=0, limit=10, count=10
        )
        assert result["total_count"] == 100
        assert result["offset"] == 0
        assert result["limit"] == 10
        assert result["count"] == 10
        assert result["has_more"] is True
        assert result["next_offset"] == 10

    def test_last_page_no_more(self):
        """Last page — no more results."""
        result = build_pagination_metadata(total_count=25, offset=20, limit=10, count=5)
        assert result["has_more"] is False
        assert result["next_offset"] is None

    def test_exact_boundary(self):
        """Offset + count == total_count means no more."""
        result = build_pagination_metadata(
            total_count=20, offset=10, limit=10, count=10
        )
        assert result["has_more"] is False
        assert result["next_offset"] is None

    def test_empty_results(self):
        """No matching items."""
        result = build_pagination_metadata(total_count=0, offset=0, limit=10, count=0)
        assert result["has_more"] is False
        assert result["next_offset"] is None
        assert result["count"] == 0

    def test_offset_beyond_total(self):
        """Offset past the end returns empty page."""
        result = build_pagination_metadata(total_count=5, offset=10, limit=10, count=0)
        assert result["has_more"] is False
        assert result["count"] == 0

    def test_zero_limit_raises(self):
        """limit=0 raises ValueError to prevent infinite pagination loops."""
        with pytest.raises(ValueError, match="limit must be positive"):
            build_pagination_metadata(total_count=10, offset=0, limit=0, count=0)

    def test_negative_limit_raises(self):
        """Negative limit raises ValueError."""
        with pytest.raises(ValueError, match="limit must be positive"):
            build_pagination_metadata(total_count=10, offset=0, limit=-1, count=0)


class TestCoerceIntParam:
    """Test coerce_int_param function."""

    def test_none_returns_default(self):
        assert coerce_int_param(None, default=42) == 42

    def test_none_returns_none_when_no_default(self):
        assert coerce_int_param(None) is None

    def test_int_passthrough(self):
        assert coerce_int_param(10, default=0) == 10

    def test_string_coercion(self):
        assert coerce_int_param("100", default=0) == 100

    def test_float_string_coercion(self):
        assert coerce_int_param("100.0", default=0) == 100

    def test_empty_string_returns_default(self):
        assert coerce_int_param("", default=5) == 5

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError, match="must be a valid integer"):
            coerce_int_param("abc", "limit")

    def test_below_min_raises(self):
        with pytest.raises(ValueError, match="must be at least"):
            coerce_int_param(-1, "offset", default=0, min_value=0)

    def test_above_max_clamped(self):
        """Values above max_value are clamped (soft cap for oversized requests)."""
        assert coerce_int_param(500, "limit", default=50, max_value=200) == 200

    def test_exact_min_value_allowed(self):
        assert coerce_int_param(0, "offset", default=0, min_value=0) == 0

    def test_exact_max_value_allowed(self):
        assert coerce_int_param(200, "limit", default=50, max_value=200) == 200


class TestNormalizeLogLevel:
    """Test normalize_log_level function (shared by ha_get_logs and enrichment helpers)."""

    @pytest.mark.parametrize(
        "numeric,expected",
        [
            (0, "NOTSET"),
            (10, "DEBUG"),
            (20, "INFO"),
            (30, "WARNING"),
            (40, "ERROR"),
            (50, "CRITICAL"),
        ],
    )
    def test_known_numeric_levels(self, numeric, expected):
        assert normalize_log_level(numeric) == expected

    def test_unknown_numeric_level_is_labelled(self):
        """Non-standard integers should be preserved verbatim (not discarded)."""
        assert normalize_log_level(25) == "LEVEL_25"

    def test_string_is_uppercased(self):
        assert normalize_log_level("debug") == "DEBUG"

    def test_string_is_trimmed(self):
        assert normalize_log_level("  warning  ") == "WARNING"

    def test_empty_string_returns_none(self):
        assert normalize_log_level("") is None
        assert normalize_log_level("   ") is None

    def test_bool_rejected(self):
        """bool is an int subclass — must not round-trip as a log level."""
        assert normalize_log_level(True) is None
        assert normalize_log_level(False) is None

    def test_none_returns_none(self):
        assert normalize_log_level(None) is None

    def test_other_types_return_none(self):
        assert normalize_log_level(3.14) is None
        assert normalize_log_level([]) is None


class TestGetLoggerLevels:
    """Test get_logger_levels helper — wraps logger/log_info WS call."""

    @pytest.mark.asyncio
    async def test_parses_numeric_levels_to_names_and_raws(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {"domain": "mqtt", "level": 10},
                    {"domain": "automation", "level": 20},
                    {"domain": "ollama", "level": 40},
                ],
            }
        )
        levels = await get_logger_levels(client)
        assert levels == {
            "mqtt": {"name": "DEBUG", "raw": 10},
            "automation": {"name": "INFO", "raw": 20},
            "ollama": {"name": "ERROR", "raw": 40},
        }

    @pytest.mark.asyncio
    async def test_string_levels_have_none_raw(self):
        """When HA returns the level as a string already, raw is None."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [{"domain": "mqtt", "level": "warning"}],
            }
        )
        assert await get_logger_levels(client) == {
            "mqtt": {"name": "WARNING", "raw": None},
        }

    @pytest.mark.asyncio
    async def test_non_standard_int_level_preserved_raw(self):
        """Non-standard ints (e.g. 25) keep the raw int alongside a LEVEL_<n> name."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [{"domain": "weird", "level": 25}],
            }
        )
        assert await get_logger_levels(client) == {
            "weird": {"name": "LEVEL_25", "raw": 25},
        }

    @pytest.mark.asyncio
    async def test_returns_empty_on_ws_failure_response(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": "logger not loaded"}
        )
        assert await get_logger_levels(client) == {}

    @pytest.mark.asyncio
    async def test_returns_empty_on_io_exception(self):
        """Connection/IO errors should degrade to an empty map, not propagate."""
        client = MagicMock()
        # ConnectionError is a subclass of OSError — the narrowed catch handles it.
        client.send_websocket_message = AsyncMock(
            side_effect=ConnectionError("websocket gone")
        )
        assert await get_logger_levels(client) == {}

    @pytest.mark.asyncio
    async def test_programming_errors_propagate(self):
        """TypeError/KeyError (bugs in this helper) should surface, not be swallowed."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(side_effect=TypeError("bad call"))
        with pytest.raises(TypeError):
            await get_logger_levels(client)

    @pytest.mark.asyncio
    async def test_skips_malformed_entries(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": True,
                "result": [
                    {"domain": "ok", "level": 10},
                    {"domain": "", "level": 20},  # empty domain
                    {"level": 30},  # missing domain
                    "not a dict",
                    {"domain": "bad_level", "level": None},
                ],
            }
        )
        assert await get_logger_levels(client) == {
            "ok": {"name": "DEBUG", "raw": 10},
        }

    @pytest.mark.asyncio
    async def test_non_list_result_returns_empty(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": {"unexpected": "shape"}}
        )
        assert await get_logger_levels(client) == {}
