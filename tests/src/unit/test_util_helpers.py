"""Unit tests for the shared tool helpers.

The parsers and projections take the caller's own input. The helpers that read
Home Assistant (log levels, repairs, entity categories) run on the server's own
client connected to a real ``hass``, so every reply they handle is Core's.
Integration diagnostics are in test_integration_diagnostics_fetch.py and the
timestamp conversion in test_timezone_metadata_conversion.py.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.coercion import parse_json_param, parse_string_list_param
from ha_mcp.tools.config_write_helpers import apply_entity_category
from ha_mcp.tools.diagnostics_helpers import parse_diagnostics_fields
from ha_mcp.tools.response_helpers import build_pagination_metadata, project_fields
from ha_mcp.tools.util_helpers import (
    filter_active_repairs,
    get_logger_levels,
    is_single_entity_target,
    normalize_log_level,
    project_repair_fields,
)

from ._command_faults import dropped_on, pooled_socket


class TestIsSingleEntityTarget:
    """Test is_single_entity_target function."""

    def test_normal_entity_id_is_single_target(self):
        """A plain domain.object_id entity ID is a single target."""
        assert is_single_entity_target("light.kitchen") is True

    def test_comma_separated_is_not_single_target(self):
        """A comma-separated multi-target list is not a single target."""
        assert is_single_entity_target("light.a,light.b") is False

    def test_entity_match_all_is_not_single_target(self):
        """Home Assistant's ENTITY_MATCH_ALL broadcast target ("all") has no
        dot, so it is not treated as a single real entity."""
        assert is_single_entity_target("all") is False

    def test_entity_match_none_is_not_single_target(self):
        """Home Assistant's ENTITY_MATCH_NONE target ("none") is likewise
        excluded by the same dotless heuristic."""
        assert is_single_entity_target("none") is False

    def test_none_is_not_single_target(self):
        """A None entity_id (a service call with no target) is not a single
        target either."""
        assert is_single_entity_target(None) is False

    def test_dotless_typo_is_not_single_target(self):
        """A malformed, dotless entity_id degrades the same way as a magic
        target -- the fast-fail is skipped, not a hard failure, for any
        string this shape."""
        assert is_single_entity_target("lightlivingroom") is False


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

    def test_deeply_nested_json_raises_the_same_error(self):
        """A string nested past the recursion limit makes ``json.loads`` raise
        ``RecursionError``, which callers do not expect; it is refused as
        invalid JSON like any other unparseable string (Patch76, PR #2356)."""
        with pytest.raises(ValueError, match="Invalid JSON"):
            parse_string_list_param("[" * 20_000)

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
            parse_string_list_param([1, 2, 3])  # type: ignore[list-item]

    def test_mixed_list_raises_error(self):
        """Mixed list (strings and non-strings) raises ValueError."""
        with pytest.raises(ValueError, match="must be a list of strings"):
            parse_string_list_param(["valid", 123])  # type: ignore[list-item]

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


@pytest.fixture
async def logger_client(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The client on a ``hass`` whose ``logger`` answers ``logger/log_info``."""
    assert await async_setup_component(hass, "logger", {})
    return core_client


@pytest.fixture
def api_logger() -> Iterator[logging.Logger]:
    """The Python logger Core reports for the loaded ``api`` integration."""
    api = logging.getLogger("homeassistant.components.api")
    level = api.level
    yield api
    api.setLevel(level)


class TestGetLoggerLevels:
    """get_logger_levels reads Core's ``logger/log_info``."""

    async def test_numeric_levels_get_names_and_raws(
        self, logger_client: HomeAssistantClient
    ) -> None:
        for integration, level in (("mqtt", "DEBUG"), ("ollama", "ERROR")):
            reply = await logger_client.send_websocket_message(
                {
                    "type": "logger/integration_log_level",
                    "integration": integration,
                    "level": level,
                    "persistence": "once",
                }
            )
            assert reply["success"], reply

        levels = await get_logger_levels(logger_client)

        assert levels["mqtt"] == {"name": "DEBUG", "raw": 10}
        assert levels["ollama"] == {"name": "ERROR", "raw": 40}

    async def test_non_standard_level_keeps_its_raw_int(
        self, logger_client: HomeAssistantClient, api_logger: logging.Logger
    ) -> None:
        api_logger.setLevel(25)

        levels = await get_logger_levels(logger_client)

        assert levels["api"] == {"name": "LEVEL_25", "raw": 25}

    async def test_failed_command_is_an_empty_map_and_a_warning(
        self, core_client: HomeAssistantClient
    ) -> None:
        """Without the logger integration Core does not know the command."""
        warnings: list[str] = []

        assert await get_logger_levels(core_client, warnings) == {}
        assert warnings == ["log levels unavailable: logger/log_info failed"]

    async def test_no_answer_is_an_empty_map(
        self, logger_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lost connection degrades to an empty map instead of propagating."""
        monkeypatch.setattr(
            logger_client,
            "send_websocket_message",
            AsyncMock(side_effect=ConnectionError("websocket gone")),
        )

        assert await get_logger_levels(logger_client) == {}


def _create_issue(hass: HomeAssistant, issue_id: str, **fields: Any) -> None:
    ir.async_create_issue(
        hass,
        "demo",
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="demo_issue",
        **fields,
    )


async def _listed_issues(
    hass: HomeAssistant, client: HomeAssistantClient
) -> list[dict[str, Any]]:
    """Core's ``repairs/list_issues`` rows."""
    assert await async_setup_component(hass, "repairs", {})
    reply = await client.send_websocket_message({"type": "repairs/list_issues"})
    assert reply["success"], reply
    issues: list[dict[str, Any]] = reply["result"]["issues"]
    return issues


class TestFilterActiveRepairs:
    """Default-filters user-dismissed repairs; opt-in returns the full set."""

    async def test_dismissed_issue_is_filtered_unless_asked_for(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        _create_issue(hass, "a")
        _create_issue(hass, "b")
        ir.async_ignore_issue(hass, "demo", "b", True)
        issues = await _listed_issues(hass, core_client)

        assert [r["issue_id"] for r in filter_active_repairs(issues)] == ["a"]
        everything = filter_active_repairs(issues, include_dismissed=True)
        assert sorted(r["issue_id"] for r in everything) == ["a", "b"]

    async def test_no_issues(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        issues = await _listed_issues(hass, core_client)

        assert filter_active_repairs(issues) == []
        assert filter_active_repairs(issues, include_dismissed=True) == []


class TestProjectRepairFields:
    """Projection keeps dismissal-state fields and drops verbose ones."""

    async def test_keeps_dismissal_state_and_drops_verbose_fields(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        _create_issue(
            hass,
            "x",
            issue_domain="automation",
            learn_more_url="https://example.com",
            translation_placeholders={"x": "y"},
        )
        ir.async_ignore_issue(hass, "demo", "x", True)
        [issue] = await _listed_issues(hass, core_client)

        out = project_repair_fields(issue)

        assert out["ignored"] is True
        assert out["dismissed_version"] is not None
        assert out["dismissed_version"] == issue["dismissed_version"]
        assert out["is_fixable"] is False
        assert out["issue_domain"] == "automation"
        # Verbose fields dropped to keep overview payloads compact
        assert "translation_placeholders" not in out
        assert "learn_more_url" not in out


@pytest.fixture
async def kitchen(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> str:
    """``light.kitchen`` in Core's entity registry, which ``config`` serves."""
    assert await async_setup_component(hass, "config", {})
    return entity_registry.async_get_or_create(
        "light", "test", "kitchen", suggested_object_id="kitchen"
    ).entity_id


def _category(hass: HomeAssistant) -> str:
    return cr.async_get(hass).async_create(scope="light", name="Test Cat").category_id


async def _apply(
    client: HomeAssistantClient, entity_id: str, category: str, result: dict[str, Any]
) -> None:
    await apply_entity_category(
        client,
        entity_id=entity_id,
        category=category,
        scope="light",
        result_dict=result,
        entity_type="light",
    )


class TestApplyEntityCategoryWriter:
    """Writer-shape contract for ``apply_entity_category`` (#1355).

    A failure goes to the top-level ``warnings`` list, never the legacy
    singular ``category_warning`` key; the tool-level shape is pinned in
    test_helper_response_shape.py.
    """

    async def test_core_refusal_appends_to_warnings(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, kitchen: str
    ) -> None:
        result: dict[str, Any] = {}

        await _apply(core_client, "light.unknown", _category(hass), result)

        assert any("failed to set category" in w for w in result["warnings"])
        assert any("Entity not found" in w for w in result["warnings"])
        assert "category_warning" not in result
        assert "category" not in result

    async def test_no_answer_appends_to_warnings(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        kitchen: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        category = _category(hass)
        monkeypatch.setattr(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("ws disconnected")),
        )
        result: dict[str, Any] = {}

        await _apply(core_client, kitchen, category, result)

        assert any("failed to set category" in w for w in result["warnings"])
        assert any("ws disconnected" in w for w in result["warnings"])
        assert "category_warning" not in result

    async def test_success_sets_category_without_warnings(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
        kitchen: str,
    ) -> None:
        category = _category(hass)
        result: dict[str, Any] = {}

        await _apply(core_client, kitchen, category, result)

        assert result == {"category": category}
        entry = entity_registry.async_get(kitchen)
        assert entry is not None
        assert entry.categories == {"light": category}

    async def test_category_deleted_before_write_is_not_written(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
        kitchen: str,
    ) -> None:
        """A category deleted after tool-entry validation must not be stored (#2159)."""
        category = _category(hass)
        cr.async_get(hass).async_delete(scope="light", category_id=category)
        result: dict[str, Any] = {}

        with patch.object(
            core_client,
            "send_websocket_message",
            wraps=core_client.send_websocket_message,
        ) as sent:
            await _apply(core_client, kitchen, category, result)

        assert "category" not in result
        assert any("no longer exists" in w for w in result["warnings"])
        # Only the recheck ran: no entity_registry/update.
        assert sent.await_count == 1
        entry = entity_registry.async_get(kitchen)
        assert entry is not None
        assert entry.categories == {}

    async def test_recheck_without_answer_still_writes(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
        kitchen: str,
    ) -> None:
        """The connection drops on the recheck; the write still goes through."""
        category = _category(hass)
        socket = await pooled_socket(core_client)
        result: dict[str, Any] = {}

        with dropped_on(hass, "config/category_registry/list", socket):
            await _apply(core_client, kitchen, category, result)

        assert result.get("category") == category
        entry = entity_registry.async_get(kitchen)
        assert entry is not None
        assert entry.categories == {"light": category}

    async def test_failure_keeps_earlier_warnings(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, kitchen: str
    ) -> None:
        result: dict[str, Any] = {"warnings": ["earlier note"]}

        await _apply(core_client, "light.unknown", _category(hass), result)

        assert result["warnings"][0] == "earlier note"
        assert any("failed to set category" in w for w in result["warnings"])
        assert "category_warning" not in result


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


class TestProjectFieldsTypoGuard:
    """project_fields() typo guard: warn when requested top-level keys are absent.

    Pins kingpanther13 v11 fix: project_fields() must emit a diagnostic in
    warnings[] when any requested key doesn't exist, rather than returning a
    mysteriously empty response (e.g. fields=["entity_history"] on a response
    whose real key is "entities").
    """

    def test_unknown_fields_key_emits_warning(self):
        """All unknown keys: success + warnings listing the available keys."""
        data = {"success": True, "entities": [1, 2], "count": 2}
        result = project_fields(data, ["nonexistent_key"])
        assert result["success"] is True
        assert "warnings" in result
        assert any("nonexistent_key" in w for w in result["warnings"])
        # Available keys hint should mention real keys
        assert any("entities" in w for w in result["warnings"])

    def test_partial_unknown_key_warns_about_missing_only(self):
        """Mixed valid+invalid: valid key is kept; diagnostic names the unknown one."""
        data = {"success": True, "entities": [1, 2], "count": 2}
        result = project_fields(data, ["entities", "typo_key"])
        assert "entities" in result
        assert result["entities"] == [1, 2]
        assert "warnings" in result
        assert any("typo_key" in w for w in result["warnings"])
        # "entities" is valid — should not appear in the unknown list
        warn_text = " ".join(result["warnings"])
        assert "entities" not in warn_text.split("not found")[0]

    def test_all_valid_fields_no_warning(self):
        """When all requested keys exist, no warning is emitted."""
        data = {"success": True, "entities": [1, 2], "count": 2}
        result = project_fields(data, ["entities"])
        assert "entities" in result
        assert "warnings" not in result

    def test_success_field_request_no_false_warning(self):
        """fields=["success"] must not warn — success is force-retained, not unknown."""
        data = {"success": True, "count": 5}
        result = project_fields(data, ["success"])
        assert result["success"] is True
        assert "warnings" not in result

    def test_warning_survives_projection_because_warnings_is_force_retained(self):
        """The warning added by the typo guard is itself force-retained."""
        data = {"success": True, "entities": [1, 2], "count": 2}
        result = project_fields(data, ["ghost_key"])
        # warnings must be in the output even though it was not in fields=
        assert "warnings" in result

    def test_available_fields_override_drives_typo_diagnostic_only(self):
        """A narrow payload reports its full schema without inventing values."""
        result = project_fields(
            {"success": True, "system_info": {}},
            ["domains"],
            available_fields=frozenset(
                {"success", "system_info", "notifications", "repairs"}
            ),
        )

        assert "notifications" not in result
        assert "repairs" not in result
        warning = result["warnings"][0]
        assert "domains" in warning
        assert "available keys: ['notifications', 'repairs', 'system_info']" in warning
