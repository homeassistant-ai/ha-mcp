"""The in-HA LLM-API probe's host-side halves, pinned without a lane (#2361).

The probe source runs only inside a Home Assistant interpreter on the embedded
lanes, and the test that consumes its report reads it through string keys the
source writes. A brace slip in the source or a misspelled key degrades to a
green lane that proves nothing, so the source is compiled here, its pure
helpers are exercised, and the report parser and the pass/fail decision are
table-tested.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

_E2E_UTILITIES = Path(__file__).resolve().parents[1] / "e2e" / "utilities"
if str(_E2E_UTILITIES) not in sys.path:
    sys.path.insert(0, str(_E2E_UTILITIES))

from llm_api_probe import (  # noqa: E402
    LLM_API_SCHEMA_PROBE,
    MIN_TOOL_COUNT,
    PROBE_HELPERS_SOURCE,
    PROBE_SENTINEL,
    assert_report_clean,
    parse_probe_report,
)


def _clean_report(**overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "timed_out": False,
        "tool_count": 88,
        "converter": "voluptuous_openapi",
        "probatio": True,
        "probatio_import_error": None,
        "draft2020_checked": True,
        "jsonschema_import_error": None,
        "inclusive_bounds_normaliser": True,
        "normalise_schema": True,
        "conversion_failures": [],
        "boolean_exclusive": [],
        "integer_lost": [],
        "draft2020_invalid": [],
        "ha_version": "2026.9.0",
        "ha_version_error": None,
    }
    report.update(overrides)
    return report


def _helpers() -> dict[str, Any]:
    namespace: dict[str, Any] = {}
    exec(PROBE_HELPERS_SOURCE, namespace)
    return namespace


class TestProbeSource:
    def test_the_probe_source_compiles(self) -> None:
        compile(LLM_API_SCHEMA_PROBE, "<probe>", "exec")

    def test_the_helpers_compile_and_load_standalone(self) -> None:
        helpers = _helpers()
        assert callable(helpers["_boolean_exclusive_bounds"])

    def test_the_walk_skips_instance_data_and_extensions(self) -> None:
        """Mirrors the component: data under default/enum/x- is not a bound."""
        helpers = _helpers()
        schema = {
            "type": "object",
            "properties": {
                "n": {"type": "number", "default": {"exclusiveMinimum": True}}
            },
            "enum": [{"exclusiveMaximum": True}],
            "x-ui": {"exclusiveMinimum": True, "type": "integer"},
        }

        assert helpers["_boolean_exclusive_bounds"](schema) is False
        assert helpers["_count_integer_nodes"](schema) == 0

    def test_the_walk_reports_a_real_draft4_bound(self) -> None:
        helpers = _helpers()
        schema = {
            "type": "object",
            "properties": {
                "budget": {
                    "anyOf": [
                        {"type": "number", "minimum": 0, "exclusiveMinimum": True},
                        {"type": "null"},
                    ]
                }
            },
        }

        assert helpers["_boolean_exclusive_bounds"](schema) is True

    def test_integer_nodes_are_counted_in_subschemas_only(self) -> None:
        helpers = _helpers()
        schema = {
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": ["integer", "null"], "x-hint": {"type": "integer"}},
            },
        }

        assert helpers["_count_integer_nodes"](schema) == 2

    def test_short_names_the_innermost_frame(self) -> None:
        helpers = _helpers()

        def boom() -> None:
            raise ValueError("first line\nsecond line")

        try:
            boom()
        except ValueError as err:
            text = helpers["_short"](err)

        assert text.startswith("ValueError: first line at ")
        assert text.endswith(" in boom")
        assert "second line" not in text


class TestParseProbeReport:
    def test_returns_the_report_after_the_sentinel(self) -> None:
        stdout = f'noise\n{PROBE_SENTINEL} {{"tool_count": 3}}\n'

        assert parse_probe_report(stdout) == {"tool_count": 3}

    def test_no_sentinel_fails_with_the_output(self) -> None:
        with pytest.raises(
            AssertionError, match="printed no LLM_API_PROBE_REPORT line"
        ):
            parse_probe_report("Traceback (most recent call last):\nboom\n")

    def test_bad_json_fails_with_the_payload(self) -> None:
        with pytest.raises(AssertionError, match="unparseable report"):
            parse_probe_report(f"{PROBE_SENTINEL} {{not json")

    def test_a_non_object_payload_fails(self) -> None:
        with pytest.raises(AssertionError, match="printed a list, not a report object"):
            parse_probe_report(f"{PROBE_SENTINEL} [1, 2]")


class TestAssertReportClean:
    def test_a_clean_report_passes(self) -> None:
        assert_report_clean(_clean_report())

    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            ({"timed_out": True}, "hit its own timeout"),
            ({"ha_version": "unknown"}, "could not read Home Assistant's version"),
            ({"tool_count": MIN_TOOL_COUNT}, "expected the full tool inventory"),
            ({"conversion_failures": ["ha_x: TypeError: boom"]}, "failed to convert"),
            ({"boolean_exclusive": ["ha_search"]}, "BOOLEAN exclusiveMinimum"),
            ({"integer_lost": ["ha_search"]}, "fewer integer-typed nodes"),
            (
                {"draft2020_invalid": ["ha_search: SchemaError"]},
                "not valid JSON Schema",
            ),
            ({"draft2020_checked": False}, "jsonschema was not importable"),
        ],
        ids=lambda value: value if isinstance(value, str) else next(iter(value)),
    )
    def test_each_dirty_field_fails_with_its_own_message(
        self, overrides: dict[str, Any], fragment: str
    ) -> None:
        with pytest.raises(AssertionError, match=fragment):
            assert_report_clean(_clean_report(**overrides))

    def test_the_offending_tools_are_named(self) -> None:
        with pytest.raises(AssertionError, match=r"\['ha_search', 'ha_get_state'\]"):
            assert_report_clean(
                _clean_report(boolean_exclusive=["ha_search", "ha_get_state"])
            )

    def test_missing_probatio_is_fine_on_a_pre_probatio_core(self) -> None:
        """No re-emission step exists there, so conversion is the contract."""
        assert_report_clean(
            _clean_report(
                probatio=False,
                draft2020_checked=False,
                ha_version="2026.8.3",
                boolean_exclusive=["ignored: no re-emission happened"],
            )
        )

    def test_missing_probatio_on_a_probatio_core_is_a_broken_image(self) -> None:
        with pytest.raises(AssertionError, match="broken image"):
            assert_report_clean(
                _clean_report(
                    probatio=False,
                    ha_version="2026.9.0",
                    probatio_import_error="ImportError: boom",
                )
            )

    def test_timed_out_wins_over_everything_else(self) -> None:
        """The partial report is the diagnosis; nothing else is trustworthy."""
        with pytest.raises(AssertionError, match="partial report"):
            assert_report_clean(_clean_report(timed_out=True, tool_count=0))
