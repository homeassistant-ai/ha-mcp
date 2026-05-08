"""Unit tests for issue #1149 — inline helper schema in ha_config_set_helper.

Issue #1149 expanded ``ha_get_helper_schema`` to cover simple-helper types
(previously flow-only) and made every reachable validation-error path in
``ha_config_set_helper`` attach the helper's ``data_schema`` to the response
context, so an LLM can self-correct from a 4xx without a separate
discovery round-trip.

Tests in this module:

1. ``SIMPLE_HELPER_SCHEMAS`` invariants — every simple helper type has a
   schema entry, every entry has a uniform shape, ``name`` is required for
   every simple type, the assertion at module load matches the
   ``SIMPLE_HELPER_TYPES`` set.
2. ``ha_get_helper_schema`` simple-type dispatch — returns the static dict
   without any HA round-trip; flow-types still drive the introspection
   flow; ``menu_option`` is rejected for simple types.
3. Simple-helper validation errors carry the schema — ``name``-required,
   ``options``-required, ``latitude``/``longitude``-required, etc., all
   surface ``data_schema`` in the response context.
4. Flow pre-flow validation gates carry the schema — the gates in
   ``_handle_flow_helper`` (``name``-required for create, malformed
   ``config``, etc.) attach the data_schema fetched via the introspection
   flow.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_config_entry_flow import (
    ALL_HELPER_TYPES,
    FLOW_HELPER_TYPES,
    SUPPORTED_HELPERS,
    ConfigEntryFlowTools,
    _FlowType,
)
from ha_mcp.tools.tools_config_helpers import (
    SIMPLE_HELPER_SCHEMAS,
    SIMPLE_HELPER_TYPES,
    _flow_helper_error_context,
    _simple_helper_error_context,
    get_simple_helper_schema,
)


def _parse_tool_error(te: ToolError) -> dict[str, Any]:
    """Parse the JSON body of a ToolError raised via raise_tool_error()."""
    return json.loads(str(te))


# ---------------------------------------------------------------------------
# 1. SIMPLE_HELPER_SCHEMAS shape invariants
# ---------------------------------------------------------------------------


class TestSimpleHelperSchemasInvariants:
    """SIMPLE_HELPER_SCHEMAS must stay aligned with SIMPLE_HELPER_TYPES and
    every entry must carry the same minimum field-spec shape."""

    def test_every_simple_helper_type_has_a_schema(self) -> None:
        # The module-level assert in tools_config_helpers ensures import-time
        # alignment; replicate it as a runtime test so a future code change
        # that breaks the assert fails loudly in CI.
        assert frozenset(SIMPLE_HELPER_SCHEMAS.keys()) == SIMPLE_HELPER_TYPES

    def test_every_field_spec_has_minimum_keys(self) -> None:
        for helper_type, schema in SIMPLE_HELPER_SCHEMAS.items():
            assert isinstance(schema, list), f"{helper_type} schema must be a list"
            assert schema, f"{helper_type} schema is empty"
            for field in schema:
                assert isinstance(field, dict), (
                    f"{helper_type} field is not a dict: {field!r}"
                )
                missing = {"name", "required", "type"} - field.keys()
                assert not missing, (
                    f"{helper_type}.{field.get('name', '?')} is missing keys "
                    f"{missing}"
                )
                assert isinstance(field["name"], str)
                assert isinstance(field["required"], bool)
                assert isinstance(field["type"], str)

    def test_name_field_is_required_for_every_simple_type(self) -> None:
        # Every simple-helper create call requires `name` (line 1697-1711 of
        # tools_config_helpers.ha_config_set_helper). The schemas must agree.
        for helper_type, schema in SIMPLE_HELPER_SCHEMAS.items():
            name_field = next(
                (f for f in schema if f["name"] == "name"), None,
            )
            assert name_field is not None, f"{helper_type} schema missing `name`"
            assert name_field["required"] is True, (
                f"{helper_type}.name should be required=True"
            )

    def test_input_select_options_is_required(self) -> None:
        schema = SIMPLE_HELPER_SCHEMAS["input_select"]
        options_field = next((f for f in schema if f["name"] == "options"), None)
        assert options_field is not None
        assert options_field["required"] is True

    def test_zone_latitude_and_longitude_are_required(self) -> None:
        schema = SIMPLE_HELPER_SCHEMAS["zone"]
        required = {f["name"] for f in schema if f["required"]}
        assert {"name", "latitude", "longitude"}.issubset(required)

    def test_tag_id_is_optional_despite_HA_requiring_it(self) -> None:
        # The tool auto-generates tag_id when missing (Bug 9 of issue #1150),
        # so the schema reflects optional from the LLM's perspective even
        # though HA's tag/create rejects without one.
        schema = SIMPLE_HELPER_SCHEMAS["tag"]
        tag_id_field = next((f for f in schema if f["name"] == "tag_id"), None)
        assert tag_id_field is not None
        assert tag_id_field["required"] is False

    def test_field_names_align_with_typed_param_table(self) -> None:
        # Each simple type's schema fields (minus the cross-cutting `name`)
        # should be a subset of `_TYPE_TYPED_PARAMS[helper_type]` plus the
        # cross-cutting allowed params (`name`/`icon`). Drift here means a
        # caller could pass a schema-listed param that the tool then
        # rejects via _validate_applicable_params.
        from ha_mcp.tools.tools_config_helpers import _TYPE_TYPED_PARAMS

        cross_cutting = {"name", "icon"}
        for helper_type, schema in SIMPLE_HELPER_SCHEMAS.items():
            schema_names = {f["name"] for f in schema}
            type_params = _TYPE_TYPED_PARAMS.get(helper_type, frozenset())
            allowed = cross_cutting | set(type_params)
            extras = schema_names - allowed
            assert not extras, (
                f"{helper_type} schema lists fields not in _TYPE_TYPED_PARAMS "
                f"or cross-cutting set: {extras}"
            )


# ---------------------------------------------------------------------------
# 2. get_simple_helper_schema and context builders
# ---------------------------------------------------------------------------


class TestGetSimpleHelperSchema:
    """get_simple_helper_schema is the single accessor used by both
    ``ha_get_helper_schema`` (dispatch path) and the validation-error-attach
    path; behaviour must stay consistent."""

    def test_returns_schema_for_each_simple_type(self) -> None:
        for helper_type in SIMPLE_HELPER_TYPES:
            schema = get_simple_helper_schema(helper_type)
            assert schema is not None
            assert schema is SIMPLE_HELPER_SCHEMAS[helper_type]

    def test_returns_none_for_flow_types(self) -> None:
        # Flow helpers go through the HA flow API; the static dict has no
        # entry, and callers branch on the None to fall back.
        for helper_type in FLOW_HELPER_TYPES:
            assert get_simple_helper_schema(helper_type) is None

    def test_returns_none_for_unknown_helper_type(self) -> None:
        assert get_simple_helper_schema("not_a_real_helper") is None


class TestSimpleHelperErrorContext:
    """`_simple_helper_error_context` is the helper called from every
    simple-helper raise site; it must always include `helper_type` and
    attach the schema when one is registered."""

    def test_context_has_helper_type_and_schema(self) -> None:
        ctx = _simple_helper_error_context("input_select")
        assert ctx["helper_type"] == "input_select"
        assert ctx["data_schema"] == SIMPLE_HELPER_SCHEMAS["input_select"]

    def test_extra_kwargs_are_appended(self) -> None:
        ctx = _simple_helper_error_context(
            "input_select", initial="x", options=["a", "b"],
        )
        assert ctx == {
            "helper_type": "input_select",
            "data_schema": SIMPLE_HELPER_SCHEMAS["input_select"],
            "initial": "x",
            "options": ["a", "b"],
        }

    def test_missing_schema_omits_data_schema(self) -> None:
        # A helper_type without a registered schema (e.g. a flow helper or
        # a typo) yields a context dict that contains only what was
        # supplied — no `data_schema` key, no exception.
        ctx = _simple_helper_error_context("template")
        assert "data_schema" not in ctx
        assert ctx == {"helper_type": "template"}


class TestFlowHelperErrorContext:
    """_flow_helper_error_context wraps the introspection-fetch with
    best-effort semantics — it must always return a context dict with
    helper_type even if HA introspection fails."""

    async def test_returns_data_schema_on_success(self) -> None:
        intro_schema = [
            {"name": "entity_id", "required": True, "selector": {"entity": {}}},
        ]
        client = AsyncMock()
        client.start_config_flow = AsyncMock(return_value={
            "type": "form",
            "flow_id": "f1",
            "step_id": "user",
            "data_schema": intro_schema,
        })
        client.abort_config_flow = AsyncMock(return_value={})

        ctx = await _flow_helper_error_context(client, "filter", extra_field="x")

        assert ctx["helper_type"] == "filter"
        assert ctx["data_schema"] == intro_schema
        assert ctx["extra_field"] == "x"

    async def test_returns_helper_type_only_on_introspection_failure(self) -> None:
        client = AsyncMock()
        # Introspection raises — fetch helper swallows the exception and
        # returns None.
        client.start_config_flow = AsyncMock(side_effect=RuntimeError("offline"))

        ctx = await _flow_helper_error_context(client, "statistics")

        assert ctx == {"helper_type": "statistics"}

    async def test_returns_helper_type_only_on_menu_without_choice(self) -> None:
        # A menu-based flow returns flow_type=menu; without menu_choice the
        # fetch helper returns None (you can't pick a sub-form to inspect).
        client = AsyncMock()
        client.start_config_flow = AsyncMock(return_value={
            "type": "menu",
            "flow_id": "menu-1",
            "menu_options": ["sensor", "binary_sensor"],
        })
        client.abort_config_flow = AsyncMock(return_value={})

        ctx = await _flow_helper_error_context(client, "template")

        assert ctx == {"helper_type": "template"}


# ---------------------------------------------------------------------------
# 3. ha_get_helper_schema simple-type dispatch
# ---------------------------------------------------------------------------


class TestHaGetHelperSchemaSimpleDispatch:
    """ha_get_helper_schema for simple types returns the static dict
    without any HA round-trip; ALL_HELPER_TYPES Literal accepts both
    flow and simple types."""

    def test_all_helper_types_literal_covers_27_types(self) -> None:
        import typing

        all_types = set(typing.get_args(ALL_HELPER_TYPES))
        flow_types = set(typing.get_args(SUPPORTED_HELPERS))
        assert all_types == flow_types | SIMPLE_HELPER_TYPES
        assert len(all_types) == 27

    async def test_simple_type_returns_static_schema_without_ha_call(self) -> None:
        client = AsyncMock()
        # If anything in the simple branch reaches HA, fail loudly.
        client.start_config_flow = AsyncMock(
            side_effect=AssertionError("simple types must not call HA"),
        )

        tool_obj = ConfigEntryFlowTools(client)
        result = await tool_obj.ha_get_helper_schema(helper_type="input_select")

        assert result["success"] is True
        assert result["helper_type"] == "input_select"
        assert result["flow_type"] == _FlowType.FORM
        assert result["step_id"] == "user"
        assert result["data_schema"] == SIMPLE_HELPER_SCHEMAS["input_select"]
        assert result["description_placeholders"] == {}
        client.start_config_flow.assert_not_called()

    async def test_simple_type_with_menu_option_rejects(self) -> None:
        client = AsyncMock()
        tool_obj = ConfigEntryFlowTools(client)

        with pytest.raises(ToolError) as exc_info:
            await tool_obj.ha_get_helper_schema(
                helper_type="counter", menu_option="sensor",
            )

        body = _parse_tool_error(exc_info.value)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "menu_option" in body["error"]["message"]
        assert body["helper_type"] == "counter"

    async def test_flow_type_still_drives_introspection_flow(self) -> None:
        # Existing behaviour for flow types must remain intact.
        intro_schema = [{"name": "entity_id", "required": True}]
        client = AsyncMock()
        client.start_config_flow = AsyncMock(return_value={
            "type": "form",
            "flow_id": "intro-1",
            "step_id": "user",
            "data_schema": intro_schema,
        })
        client.abort_config_flow = AsyncMock(return_value={})

        tool_obj = ConfigEntryFlowTools(client)
        result = await tool_obj.ha_get_helper_schema(helper_type="filter")

        assert result["success"] is True
        assert result["data_schema"] == intro_schema
        client.start_config_flow.assert_called_once_with("filter")
        # The introspection flow was aborted to avoid leaking it in HA.
        client.abort_config_flow.assert_called_once_with("intro-1")


# ---------------------------------------------------------------------------
# 4. Simple-helper validation errors carry data_schema
# ---------------------------------------------------------------------------


class TestSimpleHelperValidationAttachesSchema:
    """The high-leverage simple-helper validation gates inside
    ``ha_config_set_helper`` (name-required, options-required,
    latitude/longitude-required, has_date|has_time, etc.) all surface
    ``data_schema`` on the response context."""

    def _call_simple_validator(self, *, helper_type: str, **kwargs: Any) -> dict[str, Any]:
        """Drive a single simple-helper validator directly and return the
        parsed ToolError body. Used to exercise validators that don't need
        a client (e.g. _validate_input_select_options, _validate_mode)."""
        raise NotImplementedError  # exercised via the per-test calls below

    def test_input_select_duplicate_options_attaches_schema(self) -> None:
        from ha_mcp.tools.tools_config_helpers import (
            _validate_input_select_options,
        )

        with pytest.raises(ToolError) as exc_info:
            _validate_input_select_options(["a", "b", "a"])

        body = _parse_tool_error(exc_info.value)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert body["helper_type"] == "input_select"
        # Schema attached (the LLM's path to "what's accepted").
        assert body.get("data_schema") == SIMPLE_HELPER_SCHEMAS["input_select"]
        # Diagnostic detail kept (the path to "what failed").
        assert body["duplicates"] == ["a"]

    def test_invalid_mode_attaches_schema(self) -> None:
        from ha_mcp.tools.tools_config_helpers import _validate_mode

        with pytest.raises(ToolError) as exc_info:
            _validate_mode("input_number", "decimal")

        body = _parse_tool_error(exc_info.value)
        assert body["helper_type"] == "input_number"
        assert body.get("data_schema") == SIMPLE_HELPER_SCHEMAS["input_number"]
        assert body["mode"] == "decimal"

    def test_numeric_range_min_gt_max_attaches_schema(self) -> None:
        from ha_mcp.tools.tools_config_helpers import _validate_numeric_range

        with pytest.raises(ToolError) as exc_info:
            _validate_numeric_range("input_number", 10, 5, None)

        body = _parse_tool_error(exc_info.value)
        assert body["helper_type"] == "input_number"
        assert body.get("data_schema") == SIMPLE_HELPER_SCHEMAS["input_number"]

    def test_input_text_length_above_255_attaches_schema(self) -> None:
        from ha_mcp.tools.tools_config_helpers import _validate_numeric_range

        with pytest.raises(ToolError) as exc_info:
            _validate_numeric_range("input_text", 0, 256, None)

        body = _parse_tool_error(exc_info.value)
        assert body["helper_type"] == "input_text"
        assert body.get("data_schema") == SIMPLE_HELPER_SCHEMAS["input_text"]

    def test_schedule_overlap_attaches_schema(self) -> None:
        # _validate_schedule_days raises on overlapping ranges; verify
        # schedule's schema is attached.
        from ha_mcp.tools.tools_config_helpers import _validate_schedule_days

        overlapping = [
            {"from": "08:00", "to": "10:00"},
            {"from": "09:00", "to": "11:00"},
        ]
        with pytest.raises(ToolError) as exc_info:
            _validate_schedule_days(
                overlapping, None, None, None, None, None, None,
            )

        body = _parse_tool_error(exc_info.value)
        assert body["helper_type"] == "schedule"
        assert body.get("data_schema") == SIMPLE_HELPER_SCHEMAS["schedule"]
        assert body["day"] == "monday"


# ---------------------------------------------------------------------------
# 5. Flow pre-flow validation gates carry data_schema
# ---------------------------------------------------------------------------


class TestFlowPreFlowGatesAttachSchema:
    """The pre-flow validation gates inside ``_handle_flow_helper``
    (``name``-required for create, ``config`` not a JSON object, etc.)
    fire BEFORE HA itself sees the request, so they need to fetch the
    schema themselves via the introspection flow. Issue #1149."""

    @staticmethod
    def _make_client_with_intro_schema(
        intro_schema: list[dict[str, Any]],
    ) -> AsyncMock:
        client = AsyncMock()
        client.start_config_flow = AsyncMock(return_value={
            "type": "form",
            "flow_id": "intro-1",
            "step_id": "user",
            "data_schema": intro_schema,
        })
        client.abort_config_flow = AsyncMock(return_value={})
        # Registry validation is bypassed for these tests by passing
        # area_id/labels=None — but stub the message handler in case it's
        # reached for any reason.
        client.send_websocket_message = AsyncMock(return_value={
            "success": True, "result": [],
        })
        return client

    async def test_name_required_for_create_attaches_data_schema(self) -> None:
        from ha_mcp.tools.tools_config_helpers import _handle_flow_helper

        intro_schema = [
            {"name": "name", "required": True, "selector": {"text": {}}},
            {"name": "entity_id", "required": True, "selector": {"entity": {}}},
        ]
        client = self._make_client_with_intro_schema(intro_schema)

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_helper(
                client=client,
                helper_type="filter",
                name=None,
                helper_id=None,
                config={},  # neither top-level nor inline name
                area_id=None,
                labels=None,
                category=None,
                wait=False,
                action="create",
            )

        body = _parse_tool_error(exc_info.value)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "name is required" in body["error"]["message"]
        assert body["helper_type"] == "filter"
        # Issue #1149: data_schema attached to the pre-flow gate's error.
        assert body.get("data_schema") == intro_schema

    async def test_config_not_a_dict_attaches_data_schema(self) -> None:
        # JSON-parsable but not a dict (e.g. a list) hits the
        # `not isinstance(parsed, dict)` gate at line 1099 of helpers.
        from ha_mcp.tools.tools_config_helpers import _handle_flow_helper

        intro_schema = [
            {"name": "entity_id", "required": True},
        ]
        client = self._make_client_with_intro_schema(intro_schema)

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_helper(
                client=client,
                helper_type="statistics",
                name="My Stats",
                helper_id=None,
                config="[1, 2, 3]",  # valid JSON, parses to list, NOT a dict
                area_id=None,
                labels=None,
                category=None,
                wait=False,
                action="create",
            )

        body = _parse_tool_error(exc_info.value)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "config must" in body["error"]["message"]
        assert body.get("data_schema") == intro_schema

    async def test_config_wrong_type_attaches_data_schema(self) -> None:
        # Neither str nor dict nor None — hits the second config-validation
        # gate at line 1112 of helpers.
        from ha_mcp.tools.tools_config_helpers import _handle_flow_helper

        intro_schema = [{"name": "entity_id", "required": True}]
        client = self._make_client_with_intro_schema(intro_schema)

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_helper(
                client=client,
                helper_type="statistics",
                name="My Stats",
                helper_id=None,
                config=42,  # int — neither dict nor JSON string nor None
                area_id=None,
                labels=None,
                category=None,
                wait=False,
                action="create",
            )

        body = _parse_tool_error(exc_info.value)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "config must be a dict or JSON string" in body["error"]["message"]
        assert body.get("data_schema") == intro_schema

    async def test_pre_flow_gate_recovers_when_introspection_fails(self) -> None:
        # If HA itself refuses introspection, the gate must still raise the
        # original validation error (with helper_type only, no schema). The
        # alternative — surfacing an introspection error in place of the
        # validation error — would be worse.
        from ha_mcp.tools.tools_config_helpers import _handle_flow_helper

        client = AsyncMock()
        client.start_config_flow = AsyncMock(side_effect=RuntimeError("offline"))
        client.abort_config_flow = AsyncMock(return_value={})

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_helper(
                client=client,
                helper_type="statistics",
                name=None,
                helper_id=None,
                config={},
                area_id=None,
                labels=None,
                category=None,
                wait=False,
                action="create",
            )

        body = _parse_tool_error(exc_info.value)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "name is required" in body["error"]["message"]
        # No data_schema (introspection failed), but the validation error
        # still surfaces.
        assert "data_schema" not in body
        assert body["helper_type"] == "statistics"
