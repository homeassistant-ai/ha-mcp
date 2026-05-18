"""Unit tests for util_helpers module."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.util_helpers import (
    DIAGNOSTICS_DEFAULT_TIMEOUT_SECONDS,
    _resolve_data_path,
    build_pagination_metadata,
    coerce_int_param,
    fetch_integration_diagnostics,
    filter_active_repairs,
    get_logger_levels,
    normalize_log_level,
    parse_diagnostics_fields,
    parse_json_param,
    parse_string_list_param,
    project_repair_fields,
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


class TestFilterActiveRepairs:
    """Default-filters user-dismissed repairs; opt-in returns the full set."""

    def test_filters_ignored_by_default(self):
        issues = [
            {"issue_id": "a", "ignored": False},
            {"issue_id": "b", "ignored": True},
            {"issue_id": "c"},  # missing key — treated as not-ignored
        ]
        assert [r["issue_id"] for r in filter_active_repairs(issues)] == ["a", "c"]

    def test_include_dismissed_returns_all(self):
        issues = [
            {"issue_id": "a", "ignored": False},
            {"issue_id": "b", "ignored": True},
        ]
        out = filter_active_repairs(issues, include_dismissed=True)
        assert [r["issue_id"] for r in out] == ["a", "b"]

    def test_empty_input(self):
        assert filter_active_repairs([]) == []
        assert filter_active_repairs([], include_dismissed=True) == []


class TestProjectRepairFields:
    """Projection keeps dismissal-state fields and drops verbose ones."""

    def test_includes_dismissal_state_fields(self):
        issue = {
            "issue_id": "x",
            "domain": "demo",
            "severity": "warning",
            "translation_key": "demo_issue",
            "ignored": True,
            "dismissed_version": "2026.4.0",
            "is_fixable": False,
            "breaks_in_ha_version": None,
            "created": "2026-04-01T00:00:00+00:00",
            "issue_domain": "automation",
            "translation_placeholders": {"x": "y"},
            "learn_more_url": "https://example",
        }
        out = project_repair_fields(issue)
        assert out["ignored"] is True
        assert out["dismissed_version"] == "2026.4.0"
        assert out["is_fixable"] is False
        assert out["issue_domain"] == "automation"
        # Verbose fields dropped to keep overview payloads compact
        assert "translation_placeholders" not in out
        assert "learn_more_url" not in out

    def test_missing_fields_omitted_not_none(self):
        """Only project fields that exist on the source dict."""
        out = project_repair_fields({"issue_id": "x", "domain": "demo"})
        assert out == {"issue_id": "x", "domain": "demo"}


class TestFetchIntegrationDiagnostics:
    """Test fetch_integration_diagnostics helper — wraps /api/diagnostics/config_entry/* REST call."""

    @pytest.mark.asyncio
    async def test_happy_path_config_entry_only(self):
        """Successful fetch returns config_entry_id + data, no error."""
        client = MagicMock()
        payload = {"home_assistant": {"version": "2026.5.0"}, "data": {"some": "blob"}}
        client._request = AsyncMock(return_value=payload)
        result = await fetch_integration_diagnostics(client, "entry_abc")
        assert result == {"config_entry_id": "entry_abc", "data": payload}
        client._request.assert_awaited_once()
        call = client._request.await_args
        assert call.args == ("GET", "/diagnostics/config_entry/entry_abc")
        assert call.kwargs["timeout"] == DIAGNOSTICS_DEFAULT_TIMEOUT_SECONDS

    @pytest.mark.asyncio
    async def test_happy_path_with_device_id(self):
        """device_id extends endpoint and is echoed in the response."""
        client = MagicMock()
        client._request = AsyncMock(return_value={"data": {}})
        result = await fetch_integration_diagnostics(
            client, "entry_abc", device_id="dev_xyz"
        )
        assert result["config_entry_id"] == "entry_abc"
        assert result["device_id"] == "dev_xyz"
        assert "error" not in result
        endpoint_arg = client._request.await_args.args[1]
        assert endpoint_arg == "/diagnostics/config_entry/entry_abc/device/dev_xyz"

    @pytest.mark.asyncio
    async def test_custom_timeout_propagates(self):
        client = MagicMock()
        client._request = AsyncMock(return_value={})
        await fetch_integration_diagnostics(
            client, "entry_abc", timeout_seconds=10.0
        )
        assert client._request.await_args.kwargs["timeout"] == 10.0

    @pytest.mark.asyncio
    async def test_empty_config_entry_id_short_circuits(self):
        client = MagicMock()
        client._request = AsyncMock()
        result = await fetch_integration_diagnostics(client, "")
        assert result["config_entry_id"] == ""
        assert "config_entry_id is required" in result["error"]
        assert "ha_get_integration" in result["error"]
        client._request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_none_config_entry_id_echoes_none_in_response(self):
        """None propagates as None in the echo field — not normalised to ''.

        Tool-layer callers pass through whatever the user supplied so the
        sub-dict reflects the actual input. Coercing to "" would mask the
        difference between "user didn't supply an id" (None) and "user
        supplied the empty string" — both error states, but distinguishable.
        """
        client = MagicMock()
        client._request = AsyncMock()
        result = await fetch_integration_diagnostics(client, None)
        assert result["config_entry_id"] is None
        assert "config_entry_id is required" in result["error"]
        client._request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_404_maps_to_unavailable_error(self):
        client = MagicMock()
        client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 404 - Not Found", status_code=404
            )
        )
        result = await fetch_integration_diagnostics(client, "missing_entry")
        assert result["config_entry_id"] == "missing_entry"
        assert "Diagnostics not available" in result["error"]
        assert "config entry" in result["error"]
        assert "ha_get_integration" in result["error"]

    @pytest.mark.asyncio
    async def test_404_device_scope_mentions_device_in_error(self):
        client = MagicMock()
        client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 404 - Not Found", status_code=404
            )
        )
        result = await fetch_integration_diagnostics(
            client, "entry_abc", device_id="dev_xyz"
        )
        assert "device" in result["error"]

    @pytest.mark.asyncio
    async def test_403_maps_to_admin_required(self):
        client = MagicMock()
        client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 403 - Forbidden", status_code=403
            )
        )
        result = await fetch_integration_diagnostics(client, "entry_abc")
        assert "admin scope required" in result["error"]

    @pytest.mark.asyncio
    async def test_auth_error_maps_to_token_validity_message(self):
        """401 = stale/invalid token, NOT admin scope.

        ``HomeAssistantAuthError`` only fires on HTTP 401 per
        ``rest_client.py``. The @require_admin gate rejects with 403, which
        is handled by ``HomeAssistantAPIError``. The 401 message must steer
        operators toward token validity, not admin scope.
        """
        client = MagicMock()
        client._request = AsyncMock(
            side_effect=HomeAssistantAuthError("Invalid authentication token")
        )
        result = await fetch_integration_diagnostics(client, "entry_abc")
        assert "HTTP 401" in result["error"]
        assert "invalid or expired" in result["error"]
        assert "long-lived access token" in result["error"]
        # The admin-scope hint belongs on the 403 branch only.
        assert "admin scope" not in result["error"]

    @pytest.mark.asyncio
    async def test_timeout_maps_to_timeout_message_with_duration(self):
        client = MagicMock()
        client._request = AsyncMock(
            side_effect=HomeAssistantConnectionError("Request timeout: read")
        )
        result = await fetch_integration_diagnostics(
            client, "entry_abc", timeout_seconds=45.0
        )
        assert "timed out after 45.0s" in result["error"]
        assert "ZHA" in result["error"]

    @pytest.mark.asyncio
    async def test_other_connection_error_distinct_from_timeout(self):
        client = MagicMock()
        client._request = AsyncMock(
            side_effect=HomeAssistantConnectionError("HTTP error: connection refused")
        )
        result = await fetch_integration_diagnostics(client, "entry_abc")
        assert "connection failed" in result["error"]
        assert "timed out" not in result["error"]

    @pytest.mark.asyncio
    async def test_generic_5xx_falls_through_to_http_status_message(self):
        client = MagicMock()
        client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 500 - Internal Server Error", status_code=500
            )
        )
        result = await fetch_integration_diagnostics(client, "entry_abc")
        assert "HTTP 500" in result["error"]

    @pytest.mark.asyncio
    async def test_api_error_without_status_code_falls_back_to_placeholder(self):
        """``HomeAssistantAPIError`` with ``status_code=None`` should not crash.

        ``status_code`` is optional on the exception. The fallback branch must
        emit a ``HTTP <status>`` placeholder so the response stays informative
        even when the wrapper omits the numeric code.
        """
        client = MagicMock()
        api_error = HomeAssistantAPIError("API error: unknown failure")
        # Verify the fixture's premise — status_code unset / None falls into
        # the generic branch, not the 404/403 specialised branches.
        assert getattr(api_error, "status_code", None) is None
        client._request = AsyncMock(side_effect=api_error)
        result = await fetch_integration_diagnostics(client, "entry_abc")
        assert "HTTP <status>" in result["error"]
        assert "API error: unknown failure" in result["error"]

    @pytest.mark.asyncio
    async def test_fields_projects_top_level_keys_when_data_is_dict(self):
        client = MagicMock()
        client._request = AsyncMock(
            return_value={
                "home_assistant": {"version": "2026.5.0"},
                "custom_components": {"hacs": "1.0"},
                "integration_manifest": {"domain": "hue"},
                "issues": [],
                "data": {"bridge": {"ip": "10.0.0.5"}},
            }
        )
        result = await fetch_integration_diagnostics(
            client, "entry_abc", fields=["home_assistant", "issues"]
        )
        assert set(result["data"].keys()) == {"home_assistant", "issues"}
        assert result["data"]["home_assistant"] == {"version": "2026.5.0"}
        assert "omitted_fields" not in result

    @pytest.mark.asyncio
    async def test_fields_unknown_keys_surface_via_omitted_fields(self):
        client = MagicMock()
        client._request = AsyncMock(return_value={"home_assistant": {}, "issues": []})
        result = await fetch_integration_diagnostics(
            client,
            "entry_abc",
            fields=["home_assistant", "made_up_key", "another_missing"],
        )
        assert set(result["data"].keys()) == {"home_assistant"}
        assert result["omitted_fields"] == ["made_up_key", "another_missing"]

    @pytest.mark.asyncio
    async def test_fields_noop_when_data_is_not_dict(self):
        """A non-dict ``data`` (string/list payload) is left untouched by fields."""
        client = MagicMock()
        client._request = AsyncMock(return_value=["a", "b", "c"])
        result = await fetch_integration_diagnostics(
            client, "entry_abc", fields=["unused"]
        )
        assert result["data"] == ["a", "b", "c"]
        assert "omitted_fields" not in result

    @pytest.mark.asyncio
    async def test_truncate_at_bytes_emits_truncated_flag_when_payload_oversize(self):
        client = MagicMock()
        big_payload = {"home_assistant": {"version": "1"}, "blob": "x" * 5000}
        client._request = AsyncMock(return_value=big_payload)
        result = await fetch_integration_diagnostics(
            client, "entry_abc", truncate_at_bytes=200
        )
        assert result["truncated"] is True
        assert result["byte_cap"] == 200
        assert result["bytes_total"] > 200
        assert result["available_fields"] == ["blob", "home_assistant"]
        assert "data" not in result

    @pytest.mark.asyncio
    async def test_truncate_at_bytes_no_op_when_payload_under_cap(self):
        client = MagicMock()
        client._request = AsyncMock(return_value={"small": "ok"})
        result = await fetch_integration_diagnostics(
            client, "entry_abc", truncate_at_bytes=10_000
        )
        assert result["data"] == {"small": "ok"}
        assert "truncated" not in result
        assert "bytes_total" not in result

    @pytest.mark.asyncio
    async def test_truncate_applied_after_fields_projection(self):
        """``fields`` runs first so ``truncate_at_bytes`` measures the projected payload."""
        client = MagicMock()
        client._request = AsyncMock(
            return_value={
                "small": "ok",
                "big": "x" * 5000,
            }
        )
        result = await fetch_integration_diagnostics(
            client,
            "entry_abc",
            fields=["small"],
            truncate_at_bytes=200,
        )
        assert "truncated" not in result
        assert result["data"] == {"small": "ok"}

    # --- Round-2 KP13 gap-tests --------------------------------------------

    @pytest.mark.asyncio
    async def test_fields_all_missing_keeps_empty_dict_and_records_all_omitted(self):
        """All requested fields absent → ``data`` is the empty dict and every
        requested key appears in ``omitted_fields`` (no silent fall-through to
        the full payload)."""
        client = MagicMock()
        client._request = AsyncMock(
            return_value={"home_assistant": {}, "issues": []}
        )
        result = await fetch_integration_diagnostics(
            client,
            "entry_abc",
            fields=["foo", "bar", "baz"],
        )
        assert result["data"] == {}
        assert result["omitted_fields"] == ["foo", "bar", "baz"]

    @pytest.mark.asyncio
    async def test_fields_dedupes_caller_supplied_duplicates_in_omitted_fields(self):
        """``omitted_fields`` deduplicates caller-supplied repeats so the model
        doesn't see ``["x", "x"]`` when it asked for ``fields=["x", "x"]``."""
        client = MagicMock()
        client._request = AsyncMock(return_value={"present": 1})
        result = await fetch_integration_diagnostics(
            client,
            "entry_abc",
            fields=["missing", "missing", "present", "also_missing"],
        )
        assert result["omitted_fields"] == ["missing", "also_missing"]

    @pytest.mark.asyncio
    async def test_fields_no_op_on_error_response(self):
        """When the fetch fails, ``fields`` must not project on a partial /
        absent ``data`` — error responses ship without ``omitted_fields``."""
        client = MagicMock()
        client._request = AsyncMock(
            side_effect=HomeAssistantAPIError(
                "API error: 404 - Not Found", status_code=404
            )
        )
        result = await fetch_integration_diagnostics(
            client,
            "entry_abc",
            fields=["home_assistant", "issues"],
        )
        assert "error" in result
        assert "data" not in result
        assert "omitted_fields" not in result

    @pytest.mark.asyncio
    async def test_truncate_on_non_dict_omits_available_fields(self):
        """Truncation on a list/string payload skips ``available_fields``
        (it's only meaningful for dict payloads). The truncated/bytes_total/
        byte_cap signals still fire so the model knows the cap engaged."""
        client = MagicMock()
        client._request = AsyncMock(return_value=["x" * 1000 for _ in range(50)])
        result = await fetch_integration_diagnostics(
            client, "entry_abc", truncate_at_bytes=200
        )
        assert result["truncated"] is True
        assert result["byte_cap"] == 200
        assert result["bytes_total"] > 200
        assert "available_fields" not in result
        assert "data" not in result

    @pytest.mark.asyncio
    async def test_empty_body_surfaces_explicit_error(self):
        """``_request`` returning ``None`` (empty body) ships an explicit error
        instead of ``{"data": null}``, which would be indistinguishable from a
        zero-payload success."""
        client = MagicMock()
        client._request = AsyncMock(return_value=None)
        result = await fetch_integration_diagnostics(client, "entry_abc")
        assert "empty body" in result["error"]
        assert "data" not in result

    # --- Round-2 KP13 pagination — data_path / data_offset / data_limit ----

    @pytest.mark.asyncio
    async def test_data_path_resolves_into_dict_sub_tree(self):
        """``data_path`` replaces ``data`` with the resolved sub-tree and
        records the path."""
        client = MagicMock()
        client._request = AsyncMock(
            return_value={
                "home_assistant": {"version": "2026.5.0"},
                "data": {"bridge": {"ip": "10.0.0.5"}},
            }
        )
        result = await fetch_integration_diagnostics(
            client, "entry_abc", data_path="home_assistant"
        )
        assert result["data"] == {"version": "2026.5.0"}
        assert result["data_path"] == "home_assistant"

    @pytest.mark.asyncio
    async def test_data_path_resolves_to_scalar(self):
        client = MagicMock()
        client._request = AsyncMock(
            return_value={"home_assistant": {"version": "2026.5.0"}}
        )
        result = await fetch_integration_diagnostics(
            client, "entry_abc", data_path="home_assistant.version"
        )
        assert result["data"] == "2026.5.0"
        assert result["data_path"] == "home_assistant.version"

    @pytest.mark.asyncio
    async def test_data_path_resolves_to_list_without_limit_returns_full_list(self):
        """No ``data_limit`` → return the resolved list as-is, no pagination
        envelope."""
        client = MagicMock()
        client._request = AsyncMock(
            return_value={"data": {"devices": [{"id": i} for i in range(5)]}}
        )
        result = await fetch_integration_diagnostics(
            client, "entry_abc", data_path="data.devices"
        )
        assert result["data"] == [{"id": 0}, {"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]
        assert result["data_path"] == "data.devices"

    @pytest.mark.asyncio
    async def test_data_path_with_limit_returns_pagination_envelope(self):
        client = MagicMock()
        client._request = AsyncMock(
            return_value={"data": {"devices": [{"id": i} for i in range(25)]}}
        )
        result = await fetch_integration_diagnostics(
            client,
            "entry_abc",
            data_path="data.devices",
            data_limit=10,
        )
        assert result["data"] == {
            "path": "data.devices",
            "items": [{"id": i} for i in range(10)],
            "offset": 0,
            "limit": 10,
            "total": 25,
            "has_more": True,
        }
        assert result["data_path"] == "data.devices"

    @pytest.mark.asyncio
    async def test_data_path_pagination_offset_skips_then_has_more_false_at_end(self):
        client = MagicMock()
        client._request = AsyncMock(
            return_value={"items": [str(i) for i in range(25)]}
        )
        result = await fetch_integration_diagnostics(
            client,
            "entry_abc",
            data_path="items",
            data_offset=20,
            data_limit=10,
        )
        page = result["data"]
        assert page["items"] == ["20", "21", "22", "23", "24"]
        assert page["offset"] == 20
        assert page["limit"] == 10
        assert page["total"] == 25
        assert page["has_more"] is False

    @pytest.mark.asyncio
    async def test_data_path_offset_ignored_when_limit_unset(self):
        """``data_offset`` only applies in pagination mode (when ``data_limit``
        is also set). Without ``data_limit``, the full list is returned and a
        ``data_pagination_warning`` is surfaced so the caller knows the offset
        was dropped."""
        client = MagicMock()
        client._request = AsyncMock(
            return_value={"items": [{"id": i} for i in range(5)]}
        )
        result = await fetch_integration_diagnostics(
            client, "entry_abc", data_path="items", data_offset=2
        )
        assert result["data"] == [{"id": i} for i in range(5)]
        assert "data_offset ignored" in result.get("data_pagination_warning", "")
        assert "data_limit not set" in result["data_pagination_warning"]

    @pytest.mark.asyncio
    async def test_data_path_missing_key_surfaces_data_path_error(self):
        client = MagicMock()
        client._request = AsyncMock(return_value={"home_assistant": {}})
        result = await fetch_integration_diagnostics(
            client, "entry_abc", data_path="data.devices"
        )
        assert result["data"] is None
        assert "data_path_error" in result
        assert "missing key 'data'" in result["data_path_error"]

    @pytest.mark.asyncio
    async def test_data_path_descent_into_non_dict_surfaces_error(self):
        client = MagicMock()
        client._request = AsyncMock(
            return_value={"devices": ["d1", "d2"]}
        )
        result = await fetch_integration_diagnostics(
            client, "entry_abc", data_path="devices.0"
        )
        assert result["data"] is None
        assert "data_path_error" in result
        assert "cannot descend into list" in result["data_path_error"]

    @pytest.mark.asyncio
    async def test_data_path_empty_string_treated_as_unset(self):
        """An empty / whitespace ``data_path`` is treated as ``None`` and does
        not engage the resolver. Whitespace-only inputs (incl. ``""``) surface
        a ``data_pagination_warning`` so the caller can tell their intent was
        swallowed instead of resolving silently."""
        client = MagicMock()
        client._request = AsyncMock(return_value={"foo": "bar"})
        result = await fetch_integration_diagnostics(
            client, "entry_abc", data_path=""
        )
        # Data passes through unchanged; resolver branch is skipped.
        assert result["data"] == {"foo": "bar"}
        assert "data_path" not in result
        assert "data_path_error" not in result
        # New: empty / whitespace-stripped inputs surface a warning.
        assert "empty or whitespace-only" in result.get(
            "data_pagination_warning", ""
        )

    @pytest.mark.asyncio
    async def test_data_path_combined_with_fields_projection(self):
        """``fields`` runs first, then ``data_path`` walks into the projected
        sub-tree. A path through a dropped key surfaces as resolution error."""
        client = MagicMock()
        client._request = AsyncMock(
            return_value={
                "home_assistant": {"version": "1"},
                "data": {"devices": [{"id": i} for i in range(3)]},
            }
        )
        # fields keeps only home_assistant, so data_path='data.devices' fails.
        result = await fetch_integration_diagnostics(
            client,
            "entry_abc",
            fields=["home_assistant"],
            data_path="data.devices",
        )
        assert result["data"] is None
        assert "data_path_error" in result
        assert "missing key 'data'" in result["data_path_error"]

    @pytest.mark.asyncio
    async def test_data_path_pagination_with_truncate_preserves_envelope_metadata(
        self,
    ):
        """``truncate_at_bytes`` runs last, measured on the paginated ``items``
        list. On cap-hit with a paginated payload, the envelope's metadata
        (``path``, ``offset``, ``limit``, ``total``, ``has_more``) is
        preserved sans ``items`` so the caller can issue a narrower
        follow-up; only the bulky ``items`` slice is dropped."""
        client = MagicMock()
        client._request = AsyncMock(
            return_value={"items": ["x" * 200 for _ in range(20)]}
        )
        result = await fetch_integration_diagnostics(
            client,
            "entry_abc",
            data_path="items",
            data_limit=10,
            truncate_at_bytes=500,
        )
        # 10 items × ~200 bytes each = ~2000 bytes serialized → exceeds cap.
        assert result["truncated"] is True
        assert result["byte_cap"] == 500
        assert result["data_path"] == "items"
        # Envelope metadata preserved without the items slice.
        envelope = result["data"]
        assert "items" not in envelope
        assert envelope["path"] == "items"
        assert envelope["offset"] == 0
        assert envelope["limit"] == 10
        assert envelope["total"] == 20
        assert envelope["has_more"] is True
        assert envelope["truncated"] is True

    # --- KP13 round-3 gap tests --------------------------------------------

    @pytest.mark.asyncio
    async def test_data_path_pagination_offset_past_total_returns_empty_page(
        self,
    ):
        """``data_offset >= total`` yields empty items with ``has_more=False``
        — boundary check that the slice doesn't wrap or error."""
        client = MagicMock()
        client._request = AsyncMock(
            return_value={"items": [{"id": i} for i in range(5)]}
        )
        result = await fetch_integration_diagnostics(
            client,
            "entry_abc",
            data_path="items",
            data_offset=10,
            data_limit=3,
        )
        page = result["data"]
        assert page["items"] == []
        assert page["offset"] == 10
        assert page["total"] == 5
        assert page["has_more"] is False

    @pytest.mark.asyncio
    async def test_data_limit_on_non_list_resolved_value_surfaces_warning(self):
        """``data_limit`` with a dict-resolved path can't paginate. Surface a
        structured ``data_pagination_warning`` so the caller doesn't mistake
        the raw value for ``page 1 of N``."""
        client = MagicMock()
        client._request = AsyncMock(
            return_value={"home_assistant": {"version": "2026.5.0"}}
        )
        result = await fetch_integration_diagnostics(
            client,
            "entry_abc",
            data_path="home_assistant",
            data_limit=10,
        )
        assert result["data"] == {"version": "2026.5.0"}
        assert "data_pagination_warning" in result
        assert "not a list" in result["data_pagination_warning"]
        assert "dict" in result["data_pagination_warning"]

    @pytest.mark.asyncio
    async def test_data_path_resolves_to_null_emits_dedicated_error(self):
        """A path landing on a ``None``-valued key surfaces the dedicated
        "sub-tree not present" message rather than the generic
        ``cannot descend into NoneType`` wording."""
        client = MagicMock()
        client._request = AsyncMock(
            return_value={"home_assistant": {"version": None}}
        )
        result = await fetch_integration_diagnostics(
            client,
            "entry_abc",
            data_path="home_assistant.version.minor",
        )
        assert result["data"] is None
        assert "sub-tree not present" in result["data_path_error"]
        assert "resolved to null" in result["data_path_error"]

    @pytest.mark.asyncio
    async def test_diagnostics_data_limit_zero_rejected_at_tool_layer(self):
        """``data_limit=0`` is rejected by ``coerce_int_param(min_value=1)``
        at the wire-up — caller sees a structured validation error rather
        than an empty page that silently swallows pagination intent."""
        from fastmcp.exceptions import ToolError as _ToolError

        from ha_mcp.tools.tools_integrations import IntegrationTools

        client = MagicMock()
        tools = IntegrationTools(client)
        with pytest.raises(_ToolError) as excinfo:
            await tools.ha_get_integration(
                entry_id="entry_abc",
                include_diagnostics=True,
                diagnostics_data_limit=0,
            )
        msg = str(excinfo.value)
        assert "diagnostics_data_limit" in msg
        assert "min" in msg.lower() or "must be" in msg.lower()

    # --- KP13 round-4 gap tests --------------------------------------------

    @pytest.mark.asyncio
    async def test_data_offset_without_data_path_surfaces_warning(self):
        """``data_offset > 0`` without a ``data_path`` skips the resolver
        entirely. Surfaces a ``data_pagination_warning`` so the caller knows
        the offset was dropped (rather than silently doing nothing)."""
        client = MagicMock()
        client._request = AsyncMock(return_value={"foo": "bar"})
        result = await fetch_integration_diagnostics(
            client, "entry_abc", data_offset=5
        )
        assert "data_pagination_warning" in result
        assert "data_path not set" in result["data_pagination_warning"]
        # Underlying data is unchanged — the warning is the only signal.
        assert result["data"] == {"foo": "bar"}

    @pytest.mark.asyncio
    async def test_data_path_whitespace_only_surfaces_warning(self):
        """Whitespace-only ``data_path`` (e.g. ``"   "``) normalizes to
        unset but surfaces a structured warning so the caller doesn't
        mistake the silent unwind for "path applied"."""
        client = MagicMock()
        client._request = AsyncMock(return_value={"foo": "bar"})
        result = await fetch_integration_diagnostics(
            client, "entry_abc", data_path="   "
        )
        assert result["data"] == {"foo": "bar"}
        assert "data_path" not in result
        assert "empty or whitespace-only" in result.get(
            "data_pagination_warning", ""
        )

    @pytest.mark.asyncio
    async def test_whitespace_path_plus_offset_keeps_whitespace_warning(self):
        """Both inputs landing together (``data_path="   "`` AND
        ``data_offset > 0``) must not clobber the whitespace warning —
        the whitespace input is the primary user-facing problem and the
        ``data_offset ignored: data_path not set`` warning is its
        downstream consequence. Guarded via the ``data_pagination_warning
        not in result`` check on the orphan-offset elif."""
        client = MagicMock()
        client._request = AsyncMock(return_value={"foo": "bar"})
        result = await fetch_integration_diagnostics(
            client, "entry_abc", data_path="   ", data_offset=5
        )
        assert "empty or whitespace-only" in result.get(
            "data_pagination_warning", ""
        )
        # Specifically NOT the downstream-consequence warning.
        assert "data_path not set" not in result["data_pagination_warning"]


class TestResolveDataPath:
    """Direct tests for the dotted-path resolver."""

    def test_resolves_single_segment(self):
        value, err = _resolve_data_path({"a": 1, "b": 2}, "a")
        assert value == 1
        assert err is None

    def test_resolves_multi_segment(self):
        value, err = _resolve_data_path({"a": {"b": {"c": "deep"}}}, "a.b.c")
        assert value == "deep"
        assert err is None

    def test_resolves_to_list(self):
        value, err = _resolve_data_path({"items": [1, 2, 3]}, "items")
        assert value == [1, 2, 3]
        assert err is None

    def test_missing_top_level_key(self):
        value, err = _resolve_data_path({"a": 1}, "missing")
        assert value is None
        assert err is not None
        assert "missing key 'missing'" in err
        # Available keys hint surfaces in the error to guide the caller.
        assert "['a']" in err

    def test_descent_into_non_dict_value(self):
        value, err = _resolve_data_path({"a": "scalar"}, "a.b")
        assert value is None
        assert err is not None
        assert "cannot descend into str" in err

    def test_descent_into_list_value(self):
        value, err = _resolve_data_path({"a": [1, 2]}, "a.0")
        assert value is None
        assert err is not None
        assert "cannot descend into list" in err

    def test_empty_path_returns_error(self):
        value, err = _resolve_data_path({"a": 1}, "")
        assert value is None
        assert err == "data_path must be a non-empty dotted path"

    def test_whitespace_path_returns_error(self):
        value, err = _resolve_data_path({"a": 1}, "   ")
        assert value is None
        assert err == "data_path must be a non-empty dotted path"

    def test_empty_segment_in_middle(self):
        value, err = _resolve_data_path({"a": {"b": 1}}, "a..b")
        assert value is None
        assert err is not None
        assert "empty segment" in err

    def test_missing_key_hint_suppressed_when_no_ambiguous_sibling(self):
        """A plain typo (e.g. ``data.versionz``) on a payload whose siblings
        contain no literal ``.`` must not mention the dotted-key limitation
        — otherwise the caller reads the hint as "your '.' is being
        mis-parsed", which is misleading when no sibling has one."""
        value, err = _resolve_data_path({"version": "1", "build": "abc"}, "versionz")
        assert value is None
        assert err is not None
        assert "missing key 'versionz'" in err
        # Hint substrings must not appear when no sibling contains '.'.
        assert "literal '.'" not in err
        assert "not addressable" not in err

    def test_missing_key_hint_surfaces_when_ambiguous_sibling_present(self):
        """When a sibling key actually contains a literal ``.`` (e.g.
        ``sensor.zha_temp_42``), surface the dotted-key hint so the caller
        sees why a plausible-looking path fails. Pins the wording so a
        future refactor doesn't drop the substring."""
        payload = {"sensor.zha_temp_42": 21.5, "binary_sensor.door": True}
        value, err = _resolve_data_path(payload, "sensor")
        assert value is None
        assert err is not None
        assert "missing key 'sensor'" in err
        assert "literal '.'" in err
        assert "not addressable" in err


class TestParseDiagnosticsFields:
    """Test parse_diagnostics_fields normalisation."""

    def test_none_returns_none(self):
        assert parse_diagnostics_fields(None) is None

    def test_empty_string_returns_none(self):
        assert parse_diagnostics_fields("") is None
        assert parse_diagnostics_fields("   ") is None

    def test_empty_list_returns_none(self):
        assert parse_diagnostics_fields([]) is None

    def test_native_list_passes_through(self):
        assert parse_diagnostics_fields(["a", "b"]) == ["a", "b"]

    def test_csv_string_split_and_trimmed(self):
        assert parse_diagnostics_fields("home_assistant, issues") == [
            "home_assistant",
            "issues",
        ]

    def test_json_array_string_parsed(self):
        assert parse_diagnostics_fields('["home_assistant", "issues"]') == [
            "home_assistant",
            "issues",
        ]

    def test_invalid_json_array_raises(self):
        with pytest.raises(ValueError, match="valid JSON list"):
            parse_diagnostics_fields("[broken")

    def test_json_non_list_raises(self):
        with pytest.raises(ValueError, match="decode to a list"):
            parse_diagnostics_fields('{"home_assistant": 1}')

    def test_non_string_non_list_raises(self):
        with pytest.raises(ValueError, match="must be list, string, or None"):
            parse_diagnostics_fields(42)  # type: ignore[arg-type]
