"""Issue #2361: an exclusive bound must not reach Core's schema conversion.

Why it is fatal, and why normalising here rather than only at the source, is
written out once in ``llm_api._to_inclusive_bounds``. These tests pin what a
conversation agent sees: the schema the tool-search catalog lists for a tool
(returned verbatim, so it shows the whole walk), and the parameters of a
mirrored tool. Every schema reaches the component the way a real one does,
from the server over loopback MCP.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import probatio
import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import llm_api
from custom_components.ha_mcp_tools.const import EXPOSURE_FULL, EXPOSURE_TOOL_SEARCH

from ._llm_api_server import (
    ServedTool,
    api_instance,
    call,
    payload,
    serve,
    tool_named,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")

Normalise = Callable[..., Awaitable[list[dict[str, Any]]]]


@pytest.fixture
def normalised(hass: HomeAssistant) -> Normalise:
    """Serve each schema as a tool; return the schemas the catalog lists."""

    async def run(*schemas: dict[str, Any]) -> list[dict[str, Any]]:
        tools = [
            ServedTool(f"ha_tool_{i}", input_schema=schema, params=None)
            for i, schema in enumerate(schemas)
        ]
        async with serve(tools) as srv:
            instance = await api_instance(hass, srv.port, EXPOSURE_TOOL_SEARCH)
            result = await call(
                hass,
                tool_named(instance, "ha_search_tools"),
                {"tools": [tool.name for tool in tools]},
            )
        return [entry["input_schema"] for entry in payload(result)["results"]]

    return run


def _node(**keywords: Any) -> dict[str, Any]:
    return {"type": "object", "properties": {"budget": keywords}}


class TestWhatTheWalkRewrites:
    async def test_numeric_exclusive_bounds_become_inclusive(
        self, normalised: Normalise
    ) -> None:
        schema = {
            "type": "object",
            "properties": {
                "budget": {
                    "anyOf": [
                        {
                            "type": "number",
                            "exclusiveMinimum": 0,
                            "exclusiveMaximum": 300,
                        },
                        {"type": "null"},
                    ]
                }
            },
        }

        [out] = await normalised(schema)

        branches = out["properties"]["budget"]["anyOf"]
        assert branches[0] == {"type": "number", "minimum": 0, "maximum": 300}
        assert branches[1] == {"type": "null"}

    async def test_nested_definitions_and_array_items_are_reached(
        self, normalised: Normalise
    ) -> None:
        schema = {
            "$defs": {"Item": {"type": "integer", "exclusiveMinimum": 1}},
            "properties": {
                "rows": {"type": "array", "items": {"exclusiveMaximum": 9}},
            },
        }

        [out] = await normalised(schema)

        # The integer node folds exactly: 1 is excluded, so 2 is the bound.
        # The untyped one widens, there being no smaller step to take.
        assert out["$defs"]["Item"] == {"type": "integer", "minimum": 2}
        assert out["properties"]["rows"]["items"] == {"maximum": 9}

    async def test_an_integer_bound_folds_to_the_value_the_server_accepts(
        self, normalised: Normalise
    ) -> None:
        """An integer excluding 1 accepts 2, and advertising 1 would lie.

        The widening trade the untyped case makes -- advertise an edge the
        server rejects, and let the server reject it on arrival -- is not a
        trade that has to be made here: the exact equivalent exists.
        """
        above, below, fractional, nullable = await normalised(
            {"type": "integer", "exclusiveMinimum": 1},
            {"type": "integer", "exclusiveMaximum": 9},
            # A fractional bound on an integer node lands on the first integer
            # inside it, not on the next one out.
            {"type": "integer", "exclusiveMinimum": 1.5},
            # Nullable integers are still integers.
            {"type": ["integer", "null"], "exclusiveMinimum": 1},
        )

        assert above == {"type": "integer", "minimum": 2}
        assert below == {"type": "integer", "maximum": 8}
        assert fractional == {"type": "integer", "minimum": 2}
        assert nullable == {"type": ["integer", "null"], "minimum": 2}

    async def test_a_union_that_also_admits_numbers_is_not_tightened(
        self, normalised: Normalise
    ) -> None:
        """1.0001 is a legal value there, so 2 would reject what the server takes."""
        [out] = await normalised({"type": ["integer", "number"], "exclusiveMinimum": 1})

        assert out == {"type": ["integer", "number"], "minimum": 1}

    async def test_the_tighter_bound_wins_when_both_are_present(
        self, normalised: Normalise
    ) -> None:
        """The fold must never loosen a bound the schema already carried,
        whichever of the two happens to be tighter."""
        outs = await normalised(
            {"minimum": 1, "exclusiveMinimum": 5},
            {"minimum": 9, "exclusiveMinimum": 5},
            {"maximum": 9, "exclusiveMaximum": 5},
            {"maximum": 1, "exclusiveMaximum": 5},
        )

        assert outs == [
            {"minimum": 5},
            {"minimum": 9},
            {"maximum": 5},
            {"maximum": 1},
        ]

    async def test_draft4_boolean_form_is_dropped_and_its_bound_kept(
        self, normalised: Normalise
    ) -> None:
        [out] = await normalised(
            {"type": "number", "minimum": 0, "exclusiveMinimum": True}
        )

        assert out == {"type": "number", "minimum": 0}

    async def test_a_stray_boolean_flag_is_dropped_so_the_tool_still_converts(
        self, normalised: Normalise
    ) -> None:
        """Pins the trade: repair the malformed node, do not preserve it.

        Probatio refuses a boolean ``exclusiveMinimum`` wherever it sits in a
        subschema slot -- with or without the ``minimum`` the Draft-4 form
        requires beside it, and regardless of what else the node carries. A
        preserved flag therefore costs the whole tool, which is a worse
        outcome than dropping a keyword that means nothing on its own.
        """
        alone, nested = await normalised(
            {"type": "number", "exclusiveMinimum": True},
            {"allOf": [{"type": "number"}, {"exclusiveMinimum": True}]},
        )

        assert alone == {"type": "number"}
        assert nested == {"allOf": [{"type": "number"}, {}]}

    async def test_every_non_numeric_bound_is_dropped(
        self, normalised: Normalise
    ) -> None:
        """A string or a list raises SchemaError in probatio, which costs the
        whole tool, so the key is dropped wherever a subschema holds it. The
        name maps and instance-value keywords keep foreign data out of reach
        before this runs, so no dialect permits a non-numeric bound here."""
        outs = await normalised(
            {"exclusiveMinimum": "not-a-number"},
            {"exclusiveMinimum": None},
            {"exclusiveMaximum": ["a"]},
        )

        assert outs == [{}, {}, {}]

    async def test_a_malformed_twin_is_replaced_rather_than_kept(
        self, normalised: Normalise
    ) -> None:
        """A non-numeric ``minimum`` is not a bound probatio can read; it
        refuses the schema outright, so the folded numeric bound replacing it
        repairs a tool that would otherwise be dropped."""
        [out] = await normalised({"minimum": "x", "exclusiveMinimum": 5})

        assert out == {"minimum": 5}


class TestWhatTheWalkLeavesAlone:
    async def test_a_discriminator_mapping_is_a_name_map(
        self, normalised: Normalise
    ) -> None:
        """Its keys are author-chosen tags and its values are refs, not bounds.
        Descending into it would drop the tag as a non-numeric bound and hand
        the model a discriminated union with a missing branch."""
        schema = {
            "oneOf": [{"$ref": "#/$defs/A"}],
            "discriminator": {
                "propertyName": "kind",
                "mapping": {"exclusiveMinimum": "#/$defs/A"},
            },
        }

        [out] = await normalised(schema)

        assert out == schema

    async def test_a_specification_extension_is_opaque(
        self, normalised: Normalise
    ) -> None:
        """``x-`` keys hold arbitrary vendor objects, not subschemas: a
        bound-like key inside one is vendor data, and rewriting it would
        corrupt the extension while leaving the schema itself unchanged."""
        schema = {
            "type": "number",
            "x-ui": {"exclusiveMinimum": 5, "nested": {"exclusiveMaximum": True}},
        }

        [out] = await normalised(schema)

        assert out == schema

    async def test_a_property_named_like_the_keyword_is_left_alone(
        self, normalised: Normalise
    ) -> None:
        schema = {
            "type": "object",
            "properties": {"exclusiveMinimum": {"type": "number"}},
        }

        [out] = await normalised(schema)

        assert out == schema

    async def test_a_property_named_like_an_extension_is_still_a_subschema(
        self, normalised: Normalise
    ) -> None:
        """Opaque only as a KEYWORD: a property literally named ``x-limit`` is
        a subschema, so its bound is normalised."""
        [out] = await normalised(
            {
                "type": "object",
                "properties": {"x-limit": {"type": "number", "exclusiveMinimum": 0}},
            }
        )

        assert out["properties"]["x-limit"] == {"type": "number", "minimum": 0}

    async def test_instance_values_are_left_alone(self, normalised: Normalise) -> None:
        """default/const/enum/examples hold data, not subschemas."""
        schema = {
            "type": "object",
            "default": {"exclusiveMinimum": 5},
            "const": {"exclusiveMinimum": 5},
            "enum": [{"exclusiveMinimum": 5}],
            "examples": [{"exclusiveMinimum": 5}],
        }

        [out] = await normalised(schema)

        assert out == schema

    @pytest.mark.parametrize(
        "keyword", ["dependentRequired", "dependencies"], ids=lambda k: k
    )
    async def test_dependency_keys_are_property_names(
        self, normalised: Normalise, keyword: str
    ) -> None:
        schema = {keyword: {"exclusiveMinimum": ["a"]}}

        [out] = await normalised(schema)

        assert out == schema

    async def test_the_openapi_singular_example_is_instance_data_too(
        self, normalised: Normalise
    ) -> None:
        schema = {"example": {"exclusiveMinimum": 5}}

        [out] = await normalised(schema)

        assert out == schema


class TestWhatReachesCore:
    async def test_the_probatio_codec_reemits_a_raw_exclusive_bound_in_the_draft4_form(
        self,
    ) -> None:
        """Positive control: the premise every docstring here rests on.

        Without this, the tests below only show that the walk rewrites a
        keyword -- not that leaving the keyword alone would break anything.
        Probatio is what Core 2026.9+ converts with; the legacy
        voluptuous-openapi cannot show the defect at all, since it drops an
        exclusive bound on ingest and never emits the Draft-4 flag.
        """
        raw = {
            "type": "object",
            "properties": {"b": {"type": "number", "exclusiveMinimum": 0}},
        }

        out = probatio.to_openapi(probatio.from_openapi(raw))

        assert out["properties"]["b"].get("exclusiveMinimum") is True

    async def test_the_listed_schema_survives_the_probatio_round_trip(
        self, normalised: Normalise
    ) -> None:
        """The same schema, normalised first, carries no flag out the far end."""
        [listed] = await normalised(
            {
                "type": "object",
                "properties": {"b": {"type": "number", "exclusiveMinimum": 0}},
            }
        )

        out = probatio.to_openapi(probatio.from_openapi(listed))

        assert "exclusiveMinimum" not in json.dumps(out)

    @pytest.mark.parametrize("exposure", [EXPOSURE_FULL, EXPOSURE_TOOL_SEARCH])
    async def test_a_mirrored_tool_keeps_the_bound_as_an_inclusive_one(
        self, hass: HomeAssistant, exposure: str
    ) -> None:
        """Core's converter must never see the exclusive keyword, in either mode.

        The legacy converter drops an exclusive bound on ingest, so a mirrored
        parameter that still rejects -1 proves the bound reached it inclusive.
        """
        tool = ServedTool(
            "ha_search",
            pinned=True,
            input_schema=_node(type="number", exclusiveMinimum=0),
        )
        async with serve([tool]) as srv:
            instance = await api_instance(hass, srv.port, exposure)

        parameters = tool_named(instance, "ha_search").parameters
        assert parameters({"budget": 0}) == {"budget": 0}
        with pytest.raises((vol.Invalid, probatio.Invalid)):
            parameters({"budget": -1})


class TestWhatIsReported:
    @staticmethod
    def _reports(
        caplog: pytest.LogCaptureFixture, tool: str
    ) -> list[logging.LogRecord]:
        return [
            record
            for record in caplog.records
            if record.name == llm_api.__name__ and tool in record.getMessage()
        ]

    async def _list(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture, *tools: ServedTool
    ) -> None:
        async with serve(list(tools)) as srv:
            with caplog.at_level(logging.DEBUG, logger=llm_api.__name__):
                await api_instance(hass, srv.port, EXPOSURE_TOOL_SEARCH)

    async def test_a_tool_on_both_surfaces_is_normalised_once(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A pinned tool is mirrored and also in the catalog; two rewrites would
        also mean two log lines per turn for every pinned tool."""
        bad = _node(type="number", exclusiveMinimum=True)

        await self._list(
            hass,
            caplog,
            ServedTool("ha_pinned", pinned=True, input_schema=bad),
            ServedTool("ha_unpinned", input_schema=bad),
        )

        assert len(self._reports(caplog, "ha_pinned")) == 1
        assert len(self._reports(caplog, "ha_unpinned")) == 1

    async def test_the_rewrite_is_reported_once_per_tool_with_its_name(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Both branches log, and both name the tool that carried the schema.
        The catalog path publishes without converting anything, so a line
        emitted from the converter would say nothing about the tools that reach
        the model through tool search -- the default mode."""
        await self._list(
            hass,
            caplog,
            ServedTool(
                "ha_folded", input_schema={"type": "number", "exclusiveMinimum": 0}
            ),
            ServedTool(
                "ha_dropped", input_schema={"type": "number", "exclusiveMinimum": True}
            ),
        )

        [folded] = self._reports(caplog, "ha_folded")
        [dropped] = self._reports(caplog, "ha_dropped")
        assert folded.levelno == logging.DEBUG
        assert dropped.levelno == logging.WARNING

    async def test_one_tool_that_both_drops_and_folds_reports_both(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The two branches are independent, and a schema can hit both. They
        call for different things: the drop is the server's bug to fix, the
        fold is a widened edge to expect."""
        await self._list(
            hass,
            caplog,
            ServedTool(
                "ha_both",
                input_schema={
                    "type": "object",
                    "properties": {
                        "bad": {"type": "number", "exclusiveMinimum": True},
                        "good": {"type": "number", "exclusiveMinimum": 0},
                    },
                },
            ),
        )

        levels = {record.levelno for record in self._reports(caplog, "ha_both")}
        assert levels == {logging.WARNING, logging.DEBUG}

    async def test_two_bad_nodes_do_not_read_as_one(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A count, because the keyword name alone cannot carry the number: two
        malformed nodes in the single-node wording would send someone after one
        bound and leave the other in place."""
        await self._list(
            hass,
            caplog,
            ServedTool(
                "ha_two_bad",
                input_schema={
                    "type": "object",
                    "properties": {
                        "a": {"type": "number", "exclusiveMinimum": "x"},
                        "b": {"type": "number", "exclusiveMinimum": "y"},
                    },
                },
            ),
        )

        [report] = self._reports(caplog, "ha_two_bad")
        assert "exclusiveMinimum x2" in report.getMessage()

    async def test_an_untouched_schema_is_not_reported(
        self, hass: HomeAssistant, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Nothing changed, so nothing is worth an operator's attention."""
        await self._list(
            hass,
            caplog,
            ServedTool("ha_clean", input_schema={"type": "number", "minimum": 0}),
        )

        assert self._reports(caplog, "ha_clean") == []
