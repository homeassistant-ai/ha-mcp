"""Guardrail (issue #1601): EVERY MCP container param must coerce a JSON string.

Issue #1581/#1601 root cause: some MCP clients (Claude Desktop, Cowork/Agent
SDK) serialize object/array tool arguments as JSON-encoded *strings* before
they reach the server. The #1485/#1487/#1492 series intentionally narrowed
these params from `str | dict` to `dict` (a schema cleanup so models stop being
told a JSON string is valid) — and that narrowing is what broke stringified-
object clients (#1581). PR #1582 restored tolerance via the `JSON_STRING_COERCION`
BeforeValidator, but applied it by hand, one call site at a time — the same
per-annotation pattern that makes a missed param easy to overlook.

This test makes the contract structural and impossible to regress: it walks
*every* registered MCP tool parameter, and for every parameter whose declared
type can be a dict or a list (including `str | list[...]` unions), it asserts
that `JSON_STRING_COERCION` is present in the annotation metadata.

Adding a new container param without the coercion fails this test. Removing the
coercion from an existing one fails this test. That is the point.
"""

from __future__ import annotations

import inspect
import typing
from typing import Any

import pytest

from ha_mcp.tools.coercion import JSON_STRING_COERCION

from ._registered_tools import all_registered_tools

# (tool_name, param_name) pairs deliberately exempt from coercion, each with a
# documented reason. These are params where `str` is a semantically distinct,
# first-class value — NOT a serialization artifact — so coercing a JSON-object
# string into a dict could change behavior. Surfaced for maintainer decision;
# move them out of this allowlist to opt them in.
COERCION_EXEMPT: dict[tuple[str, str], str] = {
    ("ha_manage_app", "body"): (
        "Proxy request body: 'Pass a JSON object or JSON string' — str is an "
        "accepted raw-body form, not a serialization artifact."
    ),
    ("ha_remove_todo_item", "item"): (
        "A str is an item name, which may start with '[' or '{'; the tool reads "
        "a JSON array of strings as the list only when no item has that name."
    ),
    ("ha_manage_energy_prefs", "config_hash"): (
        "str = full-blob optimistic-lock token, dict = per-key lock; the two "
        "forms are semantically distinct and this is a fail-closed param."
    ),
}


def _type_can_be_container(annotation: Any) -> bool:
    """True if a dict or list appears anywhere in the type portion of an
    annotation (unwrapping Annotated, unions, and Optional)."""
    origin = typing.get_origin(annotation)

    # Annotated[T, ...] -> inspect T only (metadata handled separately).
    if origin is typing.Annotated:
        return _type_can_be_container(typing.get_args(annotation)[0])

    if origin in (dict, list):
        return True

    # Union / Optional: any arm being a container counts.
    args = typing.get_args(annotation)
    if args:
        return any(_type_can_be_container(arg) for arg in args)

    return annotation in (dict, list)


def _has_coercion(annotation: Any) -> bool:
    """True if JSON_STRING_COERCION is in the Annotated metadata."""
    if typing.get_origin(annotation) is not typing.Annotated:
        return False
    # get_args -> (type, *metadata); skip the leading type.
    return any(meta is JSON_STRING_COERCION for meta in typing.get_args(annotation)[1:])


def test_every_container_param_has_json_string_coercion() -> None:
    """Walk all registered MCP params; every container-typed one must coerce a
    JSON string (or be in the documented exemption allowlist)."""
    tools = all_registered_tools()
    assert tools, "no tools registered — guardrail cannot run"

    gaps: list[str] = []
    for tool_name, tool in tools.items():
        for param_name, param in inspect.signature(tool.fn).parameters.items():
            annotation = param.annotation
            if annotation is inspect.Parameter.empty:
                continue
            if not _type_can_be_container(annotation):
                continue
            if (tool_name, param_name) in COERCION_EXEMPT:
                continue
            if not _has_coercion(annotation):
                gaps.append(f"{tool_name}.{param_name}: {annotation!r}")

    assert not gaps, (
        "Container params missing JSON_STRING_COERCION (a stringified dict/list "
        "from an MCP client would be rejected or silently mishandled):\n  "
        + "\n  ".join(sorted(gaps))
    )


_TOOLS_CACHE: dict[str, Any] = {}


def _param_annotation(tool_name: str, param_name: str) -> Any:
    if not _TOOLS_CACHE:
        _TOOLS_CACHE.update(all_registered_tools())
    tool = _TOOLS_CACHE[tool_name]
    return inspect.signature(tool.fn).parameters[param_name].annotation


# Behavioral pin for the silent-swallow union class (#1601, gioanph-sudo report):
# a `str | list[...]` param given a JSON-array string must coerce to a list
# instead of matching the `str` arm and being mishandled (the ha_get_state bug
# returned ENTITY_NOT_FOUND because the whole JSON string was looked up as one id).
@pytest.mark.parametrize(
    ("tool_name", "param_name", "json_value", "expected"),
    [
        ("ha_get_state", "entity_id", '["light.a", "light.b"]', ["light.a", "light.b"]),
        ("ha_get_entity", "entity_id", '["light.a"]', ["light.a"]),
        (
            "ha_set_entity",
            "entity_id",
            '["light.a", "light.b"]',
            ["light.a", "light.b"],
        ),
        ("ha_config_set_group", "entities", '["light.a"]', ["light.a"]),
        ("ha_get_history", "entity_ids", '["sensor.x"]', ["sensor.x"]),
    ],
)
def test_container_param_coerces_json_array_string(
    tool_name: str, param_name: str, json_value: str, expected: Any
) -> None:
    from pydantic import TypeAdapter

    ann = _param_annotation(tool_name, param_name)
    assert TypeAdapter(ann).validate_python(json_value) == expected


@pytest.mark.parametrize(
    ("tool_name", "param_name", "value"),
    [
        ("ha_get_state", "entity_id", "light.kitchen"),  # bare id stays a string
        ("ha_get_state", "entity_id", ["light.a", "light.b"]),  # native list intact
        (
            "ha_get_history",
            "entity_ids",
            "sensor.x,sensor.y",
        ),  # CSV stays str (split in body)
    ],
)
def test_container_param_passes_non_json_through_unchanged(
    tool_name: str, param_name: str, value: Any
) -> None:
    """A non-JSON-container value (bare id, CSV string, native list) is not
    altered by the coercion — only JSON object/array strings are parsed."""
    from pydantic import TypeAdapter

    ann = _param_annotation(tool_name, param_name)
    assert TypeAdapter(ann).validate_python(value) == value


def _type_allows_str(annotation: Any) -> bool:
    """True if `str` is a legitimate arm of the param's type (so it may validly
    advertise a string in its schema)."""
    if typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    args = typing.get_args(annotation)
    if not args:
        return annotation is str
    return any(arg is str for arg in args)


def _schema_advertises_string(schema: dict[str, Any]) -> bool:
    return schema.get("type") == "string" or any(
        variant.get("type") == "string" for variant in schema.get("anyOf", [])
    )


def test_dict_or_list_only_params_advertise_no_string_arm() -> None:
    """Registry-driven companion to test_config_param_no_string_schema.py: every
    container param that CANNOT be a `str` must not advertise a string arm in its
    MCP schema. Walking all registered tools (not a hand-list) means a future
    revert to `str | dict` on ANY of the coerced params re-introduces the #1485
    regression here, not silently."""
    if not _TOOLS_CACHE:
        _TOOLS_CACHE.update(all_registered_tools())

    leaks: list[str] = []
    for tool_name, tool in _TOOLS_CACHE.items():
        props = tool.parameters.get("properties", {})
        for param_name, param in inspect.signature(tool.fn).parameters.items():
            annotation = param.annotation
            if annotation is inspect.Parameter.empty:
                continue
            if not _type_can_be_container(annotation):
                continue
            if _type_allows_str(annotation):
                continue  # union legitimately advertises a string arm
            if (tool_name, param_name) in COERCION_EXEMPT:
                continue
            schema = props.get(param_name)
            if schema and _schema_advertises_string(schema):
                leaks.append(f"{tool_name}.{param_name}: {schema}")

    assert not leaks, (
        "dict/list-only params advertising a string arm (re-teaches models to "
        "send JSON strings — the #1485 regression):\n  " + "\n  ".join(sorted(leaks))
    )


# Behavioral coercion for the dict-only params added in this PR — the guardrail
# proves the validator is *present*; these prove it actually *works* end-to-end.
@pytest.mark.parametrize(
    ("tool_name", "param_name", "json_value", "expected"),
    [
        ("ha_manage_app", "options", '{"ssl": true}', {"ssl": True}),
        ("ha_manage_app", "network", '{"5800/tcp": 8081}', {"5800/tcp": 8081}),
        (
            "ha_manage_energy_prefs",
            "config",
            '{"energy_sources": []}',
            {"energy_sources": []},
        ),
    ],
)
def test_dict_only_param_coerces_json_object_string(
    tool_name: str, param_name: str, json_value: str, expected: Any
) -> None:
    from pydantic import TypeAdapter

    ann = _param_annotation(tool_name, param_name)
    assert TypeAdapter(ann).validate_python(json_value) == expected


@pytest.mark.parametrize(
    ("tool_name", "param_name"),
    [
        ("ha_manage_app", "options"),
        ("ha_manage_energy_prefs", "config"),
    ],
)
def test_dict_only_param_rejects_malformed_and_array(
    tool_name: str, param_name: str
) -> None:
    """A non-JSON string and a JSON *array* string both still fail dict
    validation (keeps #1491's actionable error for genuinely-wrong input)."""
    from pydantic import TypeAdapter, ValidationError

    ann = _param_annotation(tool_name, param_name)
    adapter = TypeAdapter(ann)
    with pytest.raises(ValidationError):
        adapter.validate_python("not valid json {")
    with pytest.raises(ValidationError):
        adapter.validate_python("[1, 2, 3]")


def test_exempt_params_still_exist() -> None:
    """If an exempt param is renamed/removed, force a conscious update of the
    allowlist rather than letting it rot into a silent no-op."""
    tools = all_registered_tools()
    present = {
        (name, p)
        for name, tool in tools.items()
        for p in inspect.signature(tool.fn).parameters
    }
    stale = [f"{t}.{p}" for (t, p) in COERCION_EXEMPT if (t, p) not in present]
    assert not stale, f"COERCION_EXEMPT references params that no longer exist: {stale}"
