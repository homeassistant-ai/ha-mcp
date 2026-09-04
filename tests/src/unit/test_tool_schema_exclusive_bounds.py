"""Schema regression test (issue #2361): no tool may advertise an exclusive bound.

Why an exclusive bound is fatal on the conversation-agent path is written out
once, in ``custom_components/ha_mcp_tools/llm_api.py::_to_inclusive_bounds``.
The short of it: Home Assistant re-emits the schema through a codec that spells
the bound in a form the Anthropic API rejects, and it rejects the whole
request, so one such bound fails *every* turn rather than only calls to the
tool carrying it. A mandatory tool cannot be disabled or unpinned to escape
that; only its separate LLM-API exposure switch can, at the cost of the tool
for every conversation agent.

``config_time_budget`` on ``ha_search`` was the first instance: ``gt=0``
produced ``exclusiveMinimum`` and broke the Anthropic agent outright. Use an
inclusive bound (``ge=``/``le=``) with a small positive floor instead. That
does narrow the accepted range -- ``(0, 0.001)`` is no longer valid -- but the
smallest budget anywhere in the tree is 0.005.

Coverage: the shared registry walk forces every bool feature flag on, but
``enable_dev_mode`` is an advanced setting rather than a feature flag, so the
``ha_dev_*`` tools would go uninspected. They are deny-by-default for the LLM
API, not unreachable — ``effective_llm_api_exposed`` honours a user override
for any name — so this module turns dev mode on and asserts they are present
rather than reasoning them away.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from .test_container_param_coercion_complete import _all_registered_tools

_EXCLUSIVE_KEYWORDS = ("exclusiveMinimum", "exclusiveMaximum")

# Keys under which a dictionary is keyed by author-chosen names, and keys whose
# value is instance data or an opaque OpenAPI object rather than a subschema. A
# key spelled like a bound in one of those places is a name or a value, not a
# bound, and a walk that reads it as one would fail a valid tool schema. These
# mirror the sets in ``custom_components/ha_mcp_tools/llm_api.py``, which has
# to make the same distinction to avoid rewriting the same data; they are
# repeated rather than imported so the guard over the server's own tools does
# not depend on the component being importable.
_NAME_MAPS = frozenset(
    {
        "properties",
        "patternProperties",
        "$defs",
        "definitions",
        "dependentSchemas",
        "dependentRequired",
        "dependencies",
    }
)
_NOT_SUBSCHEMAS = frozenset(
    {"default", "const", "enum", "examples", "example", "discriminator"}
)

# Registered only while dev mode is on; the walk must reach them too.
_DEV_TOOLS = ("ha_dev_manage_server", "ha_dev_manage_settings")


@pytest.fixture
def all_tools() -> Any:
    """Every registered tool, dev mode included.

    The cached settings singleton is dropped on the way out so the next read
    rebuilds it from the restored environment: leaving a dev-mode-on singleton
    behind would hand the next test in the session a different tool surface
    than it asked for.
    """
    from ha_mcp.config import _reset_global_settings

    previous = os.environ.get("HAMCP_ENABLE_DEV_MODE")
    os.environ["HAMCP_ENABLE_DEV_MODE"] = "true"
    try:
        return _all_registered_tools()
    finally:
        if previous is None:
            os.environ.pop("HAMCP_ENABLE_DEV_MODE", None)
        else:
            os.environ["HAMCP_ENABLE_DEV_MODE"] = previous
        _reset_global_settings()


def _exclusive_bounds(node: Any, path: str) -> list[str]:
    """Return ``path: keyword`` for every exclusive bound anywhere in a schema.

    Only where the keyword is a *keyword*: a property called
    ``exclusiveMinimum``, a tag of that name in a discriminator mapping, or the
    key of a dictionary sitting in someone's ``default`` are all valid, and
    reporting them would make this guard reject schemas that convert fine.
    """
    found: list[str] = []
    if isinstance(node, dict):
        found.extend(
            f"{path}: {keyword}" for keyword in _EXCLUSIVE_KEYWORDS if keyword in node
        )
        for key, value in node.items():
            if key in _NOT_SUBSCHEMAS:
                continue
            if key in _NAME_MAPS and isinstance(value, dict):
                for name, sub in value.items():
                    found.extend(_exclusive_bounds(sub, f"{path}.{key}.{name}"))
                continue
            found.extend(_exclusive_bounds(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            found.extend(_exclusive_bounds(value, f"{path}[{index}]"))
    return found


def test_the_walk_reports_a_bound_wherever_it_hides() -> None:
    """Positive control for the walk that the guard below is built on."""
    nested = {
        "properties": {"a": {"anyOf": [{"type": "number", "exclusiveMinimum": 0}]}},
        "$defs": {"Item": {"exclusiveMaximum": 9}},
    }

    assert sorted(_exclusive_bounds(nested, "t")) == [
        "t.$defs.Item: exclusiveMaximum",
        "t.properties.a.anyOf[0]: exclusiveMinimum",
    ]


def test_the_walk_does_not_report_names_or_instance_data() -> None:
    """Every shape that spells the keyword without being one."""
    for schema in (
        {"properties": {"exclusiveMinimum": {"type": "number"}}},
        {"default": {"exclusiveMinimum": 5}},
        {"const": {"exclusiveMinimum": 5}},
        {"enum": [{"exclusiveMaximum": 5}]},
        {"examples": [{"exclusiveMinimum": 5}]},
        {
            "discriminator": {
                "propertyName": "k",
                "mapping": {"exclusiveMinimum": "#/x"},
            }
        },
    ):
        assert _exclusive_bounds(schema, "t") == [], schema


def test_the_walk_reaches_the_dev_tools(all_tools: Any) -> None:
    """Positive control: a silently shrinking walk must not read as clean."""
    missing = [name for name in _DEV_TOOLS if name not in all_tools]

    assert not missing, (
        f"dev-mode tools absent from the walk: {missing}. Their schemas go "
        "uninspected, and the guard below would pass by not looking."
    )


def test_the_search_budget_floor_is_advertised(all_tools: Any) -> None:
    """The narrowed contract is pinned, not just described in a comment.

    ``gt=0`` accepted anything above zero; the inclusive floor does not accept
    ``(0, 0.001)`` any more. The smallest budget the tree exercises is 0.005,
    so nothing real moves -- but the boundary is a public parameter contract
    and should fail visibly if someone changes it.
    """
    budget = all_tools["ha_search"].parameters["properties"]["config_time_budget"]
    numeric = next(
        branch for branch in budget["anyOf"] if branch.get("type") == "number"
    )

    assert numeric["minimum"] == 0.001
    assert "exclusiveMinimum" not in numeric


def test_no_tool_schema_advertises_an_exclusive_bound(all_tools: Any) -> None:
    """Walk every tool the registry registers; none may carry an exclusive bound.

    Not quite every tool the running server lists: the categorized-search
    transform adds a search tool and three call proxies on top, and those are
    synthesized rather than registered. They take no numeric parameters.
    """
    tools = all_tools
    assert tools, "no tools registered -- guardrail cannot run"

    offenders: list[str] = []
    for tool_name, tool in sorted(tools.items()):
        offenders.extend(_exclusive_bounds(tool.parameters, tool_name))

    assert not offenders, (
        "Tool schemas carrying an exclusive numeric bound. Home Assistant "
        "re-emits these as the Draft-4 boolean form, which Anthropic rejects "
        "as an invalid input_schema, breaking every conversation turn "
        "(issue #2361). Use ge=/le= instead:\n  " + "\n  ".join(sorted(offenders))
    )


def test_the_real_search_schema_survives_cores_round_trip(
    all_tools: Any, real_probatio: Any
) -> None:
    """The end of the chain, on the schema Core actually converts.

    The guard above reads the emitted schema; this puts the real ``ha_search``
    schema through the codec Core 2026.9 converts with and validates what comes
    out as draft 2020-12 -- the dialect the Anthropic API validates
    ``input_schema`` against, and the step that rejects the Draft-4 boolean
    form. Absent this, nothing in the suite connects "carries no exclusive
    bound" to "the API accepts it".
    """
    from jsonschema import Draft202012Validator

    emitted = real_probatio.to_openapi(
        real_probatio.from_openapi(all_tools["ha_search"].parameters)
    )

    Draft202012Validator.check_schema(emitted)


def test_the_round_trip_check_rejects_an_exclusive_bound(real_probatio: Any) -> None:
    """Positive control: the check above fails on the form this PR removes.

    Without it, the assertion beside it would pass just as happily against a
    validator that accepts everything, and the guard would read as proof of
    something it never tested.
    """
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    emitted = real_probatio.to_openapi(
        real_probatio.from_openapi(
            {
                "type": "object",
                "properties": {"b": {"type": "number", "exclusiveMinimum": 0}},
            }
        )
    )

    with pytest.raises(SchemaError):
        Draft202012Validator.check_schema(emitted)
