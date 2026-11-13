"""Regression tests for issue #1581: JSON-encoded strings on dict/list params.

The #1485/#1487/#1492 schema cleanup narrowed MCP-exposed dict/list params from
`str | dict` to `dict` so the advertised schema stops teaching models to send
JSON-encoded strings. But some MCP client stacks (Claude Desktop stdio among
them) pass model-emitted stringified objects through unrepaired, so the strict
boundary rejected previously-valid traffic with VALIDATION_FAILED/dict_type.

These tests pin the lenient-runtime half of the contract: a JSON-parseable
string for a dict/list-typed param is coerced to its parsed value before
validation, while genuinely-malformed input still fails dict_type (and gets
the actionable message from #1491's ValidationErrorMiddleware). The strict
schema half — no string arm advertised — is pinned by
test_config_param_no_string_schema.py.

Validation is checked at the annotation level: FastMCP builds its argument
TypeAdapter from the tool function's signature, so the annotation on the
registered tool fn is exactly what the transport enforces.
"""

from __future__ import annotations

import inspect
import json
from typing import Annotated, Any

import pytest
from pydantic import TypeAdapter, ValidationError

from ha_mcp.tools.coercion import JSON_STRING_COERCION

from ._registered_tools import all_registered_tools
from ._tool_param_tables import BULK_TOOLS, DICT_TOOLS


@pytest.fixture(scope="module")
def registered_tools() -> dict[str, Any]:
    """Every tool the real registry registers, keyed by tool name."""
    return all_registered_tools()


def _param_annotation(
    registered: dict[str, Any], tool_name: str, param_name: str
) -> Any:
    return inspect.signature(registered[tool_name].fn).parameters[param_name].annotation


# Param-appropriate sample values: each must satisfy the param's value type
# (e.g. ha_set_entity.expose_to is dict[str, bool]).
_DICT_SAMPLES: dict[tuple[str, str], dict[str, Any]] = {
    ("ha_set_entity", "options"): {"sensor": {"display_precision": 2}},
    ("ha_set_entity", "categories"): {"automation": "category_id"},
    ("ha_set_entity", "expose_to"): {"conversation": True},
    ("ha_call_service", "data"): {
        "notification_id": "x",
        "title": "t",
        "message": "m",
    },
    ("ha_call_event", "data"): {"source": "unit_test"},
}
_DEFAULT_DICT_SAMPLE: dict[str, Any] = {"alias": "Test", "key": "value"}

_LIST_SAMPLE: list[dict[str, Any]] = [
    {"entity_id": "light.kitchen", "action": "turn_on"}
]

_DICT_PARAMS = [(tool, param) for _, _, tool, param in DICT_TOOLS]
_BULK_PARAMS = [(tool, param) for _, _, tool, param in BULK_TOOLS]


@pytest.mark.parametrize(("tool_name", "param_name"), _DICT_PARAMS)
def test_dict_param_coerces_json_string(registered_tools, tool_name, param_name):
    """A JSON-encoded object string is coerced to a dict before validation."""
    sample = _DICT_SAMPLES.get((tool_name, param_name), _DEFAULT_DICT_SAMPLE)
    ann = _param_annotation(registered_tools, tool_name, param_name)
    assert TypeAdapter(ann).validate_python(json.dumps(sample)) == sample


@pytest.mark.parametrize(("tool_name", "param_name"), _DICT_PARAMS)
def test_dict_param_passes_native_dict_through(registered_tools, tool_name, param_name):
    """A native dict continues to validate unchanged."""
    sample = _DICT_SAMPLES.get((tool_name, param_name), _DEFAULT_DICT_SAMPLE)
    ann = _param_annotation(registered_tools, tool_name, param_name)
    assert TypeAdapter(ann).validate_python(sample) == sample


@pytest.mark.parametrize(("tool_name", "param_name"), _BULK_PARAMS)
def test_list_param_coerces_json_string(registered_tools, tool_name, param_name):
    """A JSON-encoded array string is coerced to a list before validation."""
    ann = _param_annotation(registered_tools, tool_name, param_name)
    assert TypeAdapter(ann).validate_python(json.dumps(_LIST_SAMPLE)) == _LIST_SAMPLE


@pytest.mark.parametrize(("tool_name", "param_name"), _DICT_PARAMS)
def test_dict_param_rejects_unparseable_string(registered_tools, tool_name, param_name):
    """A non-JSON string still fails validation (keeps #1491's actionable error)."""
    ann = _param_annotation(registered_tools, tool_name, param_name)
    with pytest.raises(ValidationError):
        TypeAdapter(ann).validate_python("definitely not json {")


@pytest.mark.parametrize(
    ("malformed", "detail"),
    [
        ('{"alias": "Test",}', "line 1 column 17"),
        ("  [1, 2,]", "line 1 column 8"),
    ],
)
def test_json_like_malformed_string_preserves_decode_location(
    registered_tools, malformed, detail
):
    """Malformed container JSON reports its parse location instead of dict_type."""
    tool_name, param_name = _DICT_PARAMS[0]
    ann = _param_annotation(registered_tools, tool_name, param_name)

    with pytest.raises(ValidationError) as exc_info:
        TypeAdapter(ann).validate_python(malformed)

    error = exc_info.value.errors(include_url=False)[0]
    assert error["type"] == "value_error"
    assert "Invalid JSON" in error["msg"]
    assert detail in error["msg"]


@pytest.mark.parametrize(
    "jinja_template",
    [
        "{{ states('light.kitchen') }}",
        "{% if is_state('light.kitchen', 'on') %}on{% endif %}",
        "{# explanatory comment #}",
    ],
)
def test_jinja_templates_pass_through_for_string_union(jinja_template):
    """Jinja strings remain available to the string arm of union parameters."""
    annotation = Annotated[str | dict[str, Any], JSON_STRING_COERCION]

    assert TypeAdapter(annotation).validate_python(jinja_template) == jinja_template


def test_malformed_json_containing_jinja_preserves_decode_location():
    """Jinja inside a JSON-like container does not suppress decoder details."""
    annotation = Annotated[str | dict[str, Any], JSON_STRING_COERCION]

    with pytest.raises(ValidationError) as exc_info:
        TypeAdapter(annotation).validate_python('{"option": {{ template }}}')

    error = exc_info.value.errors(include_url=False)[0]
    assert error["type"] == "value_error"
    assert "Invalid JSON" in error["msg"]
    assert "line 1 column 13" in error["msg"]


@pytest.mark.parametrize(("tool_name", "param_name"), _DICT_PARAMS)
def test_dict_param_rejects_json_string_of_array(
    registered_tools, tool_name, param_name
):
    """A JSON-encoded array for a dict param still fails dict validation."""
    ann = _param_annotation(registered_tools, tool_name, param_name)
    with pytest.raises(ValidationError):
        TypeAdapter(ann).validate_python("[1, 2, 3]")


@pytest.mark.parametrize("scalar_string", ['"hello"', "123", "null"])
def test_dict_param_passes_json_scalar_string_through_unchanged(
    registered_tools, scalar_string
):
    """A well-formed JSON scalar string is not coerced: it fails dict_type
    with the original string as the offending input."""
    tool_name, param_name = _DICT_PARAMS[0]
    ann = _param_annotation(registered_tools, tool_name, param_name)
    with pytest.raises(ValidationError) as exc_info:
        TypeAdapter(ann).validate_python(scalar_string)
    assert exc_info.value.errors()[0]["input"] == scalar_string


def test_deeply_nested_string_fails_validation_not_recursion(registered_tools):
    """Regression: json.loads raises RecursionError on deeply-nested input.

    Pydantic only converts ValueError/AssertionError raised in a validator
    into a ValidationError, so an uncaught RecursionError would surface as an
    opaque internal error instead of the actionable dict_type message.
    """
    tool_name, param_name = _DICT_PARAMS[0]
    ann = _param_annotation(registered_tools, tool_name, param_name)
    with pytest.raises(ValidationError):
        TypeAdapter(ann).validate_python("[" * 100_000)


@pytest.mark.parametrize(("tool_name", "param_name"), _BULK_PARAMS)
def test_list_param_passes_native_list_through(registered_tools, tool_name, param_name):
    """A native list continues to validate unchanged."""
    ann = _param_annotation(registered_tools, tool_name, param_name)
    assert TypeAdapter(ann).validate_python(_LIST_SAMPLE) == _LIST_SAMPLE


@pytest.mark.parametrize(("tool_name", "param_name"), _BULK_PARAMS)
def test_list_param_rejects_json_string_of_object(
    registered_tools, tool_name, param_name
):
    """A JSON-encoded object for a list param still fails list validation."""
    ann = _param_annotation(registered_tools, tool_name, param_name)
    with pytest.raises(ValidationError):
        TypeAdapter(ann).validate_python('{"entity_id": "light.kitchen"}')


# ---------------------------------------------------------------------------
# Dev-tool dict params (ha_dev_manage_settings policy / backup)
#
# These carry the same JSON_STRING_COERCION as the config-tool dict params, but
# register_dev_tools no-ops unless developer mode is on, so the shared
# registry above never reaches them. Enable the flag, reset the cached
# settings singleton, and register the tools again under it.
# ---------------------------------------------------------------------------


@pytest.fixture
def dev_mode_enabled(monkeypatch):
    from ha_mcp.config import reset_global_settings

    monkeypatch.setenv("HAMCP_ENABLE_DEV_MODE", "true")
    reset_global_settings()
    try:
        yield
    finally:
        # Drop the flag *before* re-reading so later tests see dev mode off.
        monkeypatch.delenv("HAMCP_ENABLE_DEV_MODE", raising=False)
        reset_global_settings()


@pytest.fixture
def dev_registered_tools(dev_mode_enabled) -> dict[str, Any]:
    """Every tool the registry registers with developer mode on."""
    return all_registered_tools()


_DEV_DICT_PARAMS: list[tuple[str, str]] = [
    ("ha_dev_manage_settings", "policy"),
    ("ha_dev_manage_settings", "backup"),
]

_DEV_DICT_SAMPLES: dict[tuple[str, str], dict[str, Any]] = {
    ("ha_dev_manage_settings", "policy"): {
        "wait_seconds": 30,
        "approval_ttl_minutes": 5,
        "rules": [],
        "version": 0,
    },
    ("ha_dev_manage_settings", "backup"): {"enable_auto_backup": False},
}


class TestDevToolDictParamCoercion:
    """The dev-mode set_policy/set_backup dict params round-trip a JSON string
    just like the config-tool dict params — a stringified object is coerced,
    a native dict passes through, and genuinely-malformed input still fails."""

    @pytest.mark.parametrize(("tool_name", "param_name"), _DEV_DICT_PARAMS)
    def test_dev_dict_param_coerces_json_string(
        self, dev_registered_tools, tool_name, param_name
    ):
        sample = _DEV_DICT_SAMPLES[(tool_name, param_name)]
        ann = _param_annotation(dev_registered_tools, tool_name, param_name)
        assert TypeAdapter(ann).validate_python(json.dumps(sample)) == sample

    @pytest.mark.parametrize(("tool_name", "param_name"), _DEV_DICT_PARAMS)
    def test_dev_dict_param_passes_native_dict_through(
        self, dev_registered_tools, tool_name, param_name
    ):
        sample = _DEV_DICT_SAMPLES[(tool_name, param_name)]
        ann = _param_annotation(dev_registered_tools, tool_name, param_name)
        assert TypeAdapter(ann).validate_python(sample) == sample

    @pytest.mark.parametrize(("tool_name", "param_name"), _DEV_DICT_PARAMS)
    def test_dev_dict_param_rejects_unparseable_string(
        self, dev_registered_tools, tool_name, param_name
    ):
        ann = _param_annotation(dev_registered_tools, tool_name, param_name)
        with pytest.raises(ValidationError):
            TypeAdapter(ann).validate_python("definitely not json {")
