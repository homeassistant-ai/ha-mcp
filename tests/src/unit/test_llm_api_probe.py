"""The in-HA LLM-API probe, pinned without a lane (#2361).

On the embedded lanes the probe source runs inside Home Assistant's
interpreter, and the test that consumes its report reads it through string
keys the source writes. A brace slip in the source or a misspelled key degrades
to a green lane that proves nothing, so the source is compiled here, run the
way the lanes run it (its own interpreter, with Core and the component
importable, against a loopback MCP server listing the tools under test), and
the report parser and the pass/fail decision are table-tested.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from custom_components.ha_mcp_tools import llm_api
from tests.src.e2e.utilities.llm_api_probe import (
    LLM_API_SCHEMA_PROBE,
    MIN_TOOL_COUNT,
    PROBE_SENTINEL,
    assert_report_clean,
    parse_probe_report,
)

from ._llm_api_server import SECRET_PATH, ServedTool, serve

# The directory ``custom_components`` is imported from, as ``/config`` is in the
# Home Assistant container.
_COMPONENT_ROOT = Path(llm_api.__file__).resolve().parents[2]


def _clean_report(**overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "timed_out": False,
        "tool_count": 88,
        "converter": "voluptuous_openapi",
        "probatio": True,
        "probatio_import_error": None,
        "draft2020_checked": True,
        "jsonschema_import_error": None,
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


async def _probe(tool: ServedTool) -> dict[str, Any]:
    """Run the probe against a server listing ``tool``; its report."""
    async with serve([tool]) as server:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            LLM_API_SCHEMA_PROBE,
            f"http://127.0.0.1:{server.port}{SECRET_PATH}",
            env={**os.environ, "PYTHONPATH": str(_COMPONENT_ROOT)},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()
    report = parse_probe_report(stdout.decode())
    assert report["tool_count"] == 1
    assert report["conversion_failures"] == []
    return report


def _written_report_keys() -> set[str]:
    """Keys of the ``report.update({...})`` literal the runner writes."""
    tree = ast.parse(LLM_API_SCHEMA_PROBE)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and node.args
            and isinstance(node.args[0], ast.Dict)
        ):
            return {
                key.value
                for key in node.args[0].keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    raise AssertionError("no report.update({...}) literal found in the probe runner")


def _read_report_keys() -> set[str]:
    """Every ``report.get("...")`` and ``report["..."]`` key the reader uses."""
    tree = ast.parse(inspect.getsource(assert_report_clean))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)
    return keys


class TestProbeSource:
    def test_the_probe_source_compiles(self) -> None:
        compile(LLM_API_SCHEMA_PROBE, "<probe>", "exec")

    def test_every_key_the_reader_uses_is_one_the_runner_writes(self) -> None:
        """A renamed defect-signal key must fail here, not degrade to green.

        The reader goes through ``report.get``, so a key the runner stopped
        writing reads as "no problem"; bind the two statically.
        """
        written = _written_report_keys()
        read = _read_report_keys()

        assert read, "the reader reads no keys at all?"
        assert read <= written, f"read but never written: {sorted(read - written)}"
        assert written <= read, f"written but never read: {sorted(written - read)}"


@pytest.mark.usefixtures("socket_enabled")
class TestTheProbeRun:
    async def test_instance_data_and_extensions_are_not_read_as_schema(self) -> None:
        """Mirrors the component: data under enum/default/x- is neither a bound
        nor a parameter type, so neither flag is raised for it."""
        report = await _probe(
            ServedTool(
                "ha_opaque",
                input_schema={
                    "type": "object",
                    "properties": {
                        # Core re-emits an enum as given.
                        "mode": {
                            "type": "string",
                            "enum": [{"exclusiveMaximum": True}],
                        },
                        "hint": {"type": "string", "x-hint": {"type": "integer"}},
                        "kind": {"type": "string", "default": {"type": "integer"}},
                    },
                },
            )
        )

        assert report["boolean_exclusive"] == []
        assert report["integer_lost"] == []

    async def test_an_integer_type_the_conversion_loses_is_reported(self) -> None:
        """Core's converter drops a ``$ref``; the nullable integer it pointed at
        is a parameter type the model no longer sees."""
        report = await _probe(
            ServedTool(
                "ha_ref",
                input_schema={
                    "type": "object",
                    "properties": {"count": {"$ref": "#/$defs/Count"}},
                    "$defs": {"Count": {"type": ["integer", "null"]}},
                },
            )
        )

        assert report["integer_lost"] == ["ha_ref"]

    async def test_an_invalid_schema_is_named_by_its_first_line_and_frame(
        self,
    ) -> None:
        """A remote interpreter hands back one line per failure: the error's
        first line and where it was raised, never a multi-line traceback."""
        report = await _probe(
            ServedTool(
                "ha_bad_length",
                input_schema={
                    "type": "object",
                    "properties": {"name": {"type": "string", "minLength": -1}},
                },
            )
        )

        [failure] = report["draft2020_invalid"]
        assert re.fullmatch(
            r"ha_bad_length: SchemaError: -1 is less than the minimum of 0"
            r" at \S+\.py:\d+ in \w+",
            failure,
        ), failure


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
