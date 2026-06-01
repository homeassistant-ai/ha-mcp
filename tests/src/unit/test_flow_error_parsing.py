"""Unit tests for flow error parsing (Bug 15 / issue #1150 + issue #1149).

When Home Assistant returns a 400/422 during a flow create or update,
the tool should surface a structured error with field-level detail AND
the helper's ``data_schema`` so the caller has both "what failed" and
"what's accepted" — together they're enough for self-correction. The
pre-fix behaviour collapsed every 4xx into a generic
``"API error: 400 - Bad Request"`` ToolError with no actionable detail.

Tests in this module:

1. HA returns ``{"errors": {...}}`` -> ToolError carries ``field_errors``
   with the original keys/values AND ``data_schema`` (issue #1149: the
   schema is now attached symmetrically with the unstructured branch
   instead of being skipped when field_errors are present).
2. HA returns an unstructured 400 (just ``{"message": "..."}``) ->
   ToolError carries ``data_schema`` fetched via a fresh introspection
   flow against the helper.
3. The existing menu-helper "missing menu_option" path still raises
   ``CONFIG_MISSING_REQUIRED_FIELDS`` and is not affected by the new
   wrapping logic (regression guard).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools.tools_config_entry_flow import (
    _handle_flow_steps,
    _parse_flow_api_error,
    create_flow_helper,
)


def _parse_tool_error(te: ToolError) -> dict[str, Any]:
    """Parse the JSON body of a ToolError raised via raise_tool_error()."""
    return json.loads(str(te))


# ---------------------------------------------------------------------------
# 1. Structured field-level errors
# ---------------------------------------------------------------------------


class TestStructuredFieldErrors:
    """When HA returns a body with ``errors``, the tool surfaces it verbatim."""

    def test_parse_extracts_field_errors_and_message(self) -> None:
        api_err = HomeAssistantAPIError(
            "API error: 400 - Bad Request",
            status_code=400,
            response_data={
                "errors": {"entity_id": "invalid_entity"},
                "description_placeholders": {"hint": "must be a sensor"},
            },
        )
        parsed = _parse_flow_api_error(api_err)
        assert parsed["field_errors"] == {"entity_id": "invalid_entity"}
        assert "must be a sensor" in parsed["message"]
        assert parsed["raw"]["errors"] == {"entity_id": "invalid_entity"}

    async def test_create_flow_with_400_field_errors_raises_structured(self) -> None:
        """End-to-end: a form submit failing with structured errors should
        surface the field errors via the wrapping ToolError, and (issue
        #1149) ALSO attach the data_schema so the LLM has both "what
        failed" and "what's accepted" available."""
        # The introspection flow fired by the new schema-attach branch is a
        # second call to start_config_flow; sequence both.
        intro_schema = [
            {"name": "entity_id", "required": True, "selector": {"entity": {}}},
            {"name": "filter", "required": True, "selector": {"select": {"options": ["lowpass", "outlier"]}}},
        ]
        start_calls: list[str] = []

        async def start_flow(handler: str) -> dict[str, Any]:
            start_calls.append(handler)
            if len(start_calls) == 1:
                return {
                    "type": "form",
                    "flow_id": "flow-1",
                    "step_id": "user",
                    "data_schema": [{"name": "entity_id"}],
                }
            return {
                "type": "form",
                "flow_id": "intro-flow",
                "step_id": "user",
                "data_schema": intro_schema,
            }

        client = AsyncMock()
        client.start_config_flow = AsyncMock(side_effect=start_flow)

        api_err = HomeAssistantAPIError(
            "API error: 400 - Bad Request",
            status_code=400,
            response_data={"errors": {"entity_id": "not_a_sensor"}},
        )
        client.submit_config_flow_step = AsyncMock(side_effect=api_err)
        client.abort_config_flow = AsyncMock(return_value={})

        with pytest.raises(ToolError) as exc_info:
            await create_flow_helper(client, "filter", {"entity_id": "light.foo"})

        body = _parse_tool_error(exc_info.value)
        assert body["success"] is False
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        # Field errors are exposed at the top level (via context).
        assert body.get("field_errors") == {"entity_id": "not_a_sensor"}
        assert body.get("status_code") == 400
        # Issue #1149: the data_schema is now attached even when
        # structured field_errors are present — the LLM gets both
        # "what failed" and "what's accepted" to self-correct.
        assert body.get("data_schema") == intro_schema
        # The original-flow + introspection-flow were both aborted.
        client.abort_config_flow.assert_called()


# ---------------------------------------------------------------------------
# 2. Unstructured errors -> data_schema attached
# ---------------------------------------------------------------------------


class TestUnstructuredErrorAttachesSchema:
    """When HA returns only a ``message`` (or nothing useful), the tool
    fetches the helper's data_schema and attaches it to the error."""

    async def test_create_flow_with_unstructured_400_attaches_data_schema(
        self,
    ) -> None:
        # State machine for start_config_flow:
        # call 1 -> the real create flow's initial form
        # call 2 -> the introspection flow used to fetch the schema for
        #           the error context (post-failure).
        intro_schema = [
            {"name": "entity_id", "required": True},
            {"name": "state_characteristic", "required": True},
        ]
        start_calls: list[str] = []

        async def start_flow(handler: str) -> dict[str, Any]:
            start_calls.append(handler)
            if len(start_calls) == 1:
                # Real flow: form with one field, will be submitted and 400.
                return {
                    "type": "form",
                    "flow_id": "real-flow",
                    "step_id": "user",
                    "data_schema": [{"name": "entity_id"}],
                }
            # Introspection flow used by error context.
            return {
                "type": "form",
                "flow_id": "intro-flow",
                "step_id": "user",
                "data_schema": intro_schema,
            }

        api_err = HomeAssistantAPIError(
            "API error: 400 - Bad Request",
            status_code=400,
            # No "errors" map — only a vague message.
            response_data={"message": "Bad Request"},
        )

        client = AsyncMock()
        client.start_config_flow = AsyncMock(side_effect=start_flow)
        client.submit_config_flow_step = AsyncMock(side_effect=api_err)
        client.abort_config_flow = AsyncMock(return_value={})

        with pytest.raises(ToolError) as exc_info:
            await create_flow_helper(
                client, "statistics", {"entity_id": "sensor.foo"}
            )

        body = _parse_tool_error(exc_info.value)
        assert body["success"] is False
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        # No structured field_errors because the body had none.
        assert "field_errors" not in body
        # data_schema must be attached so the LLM can correct itself.
        assert body.get("data_schema") == intro_schema
        # The error message should mention HA rejecting the request, with status.
        assert "400" in body["error"]["message"]
        # Two start_config_flow calls: one for the real flow, one for
        # introspection during error-handling.
        assert start_calls == ["statistics", "statistics"]

    async def test_parse_falls_back_to_exception_message(self) -> None:
        """When response_data is None or empty, parser still returns the
        wrapper exception message rather than an empty string."""
        api_err = HomeAssistantAPIError(
            "API error: 400 - Bad Request",
            status_code=400,
            response_data=None,
        )
        parsed = _parse_flow_api_error(api_err)
        assert parsed["field_errors"] == {}
        assert "Bad Request" in parsed["message"]
        # When HA returns no body, raw is normalised to {} (it's still a
        # well-formed empty dict, distinguished by empty field_errors).
        assert parsed["raw"] == {}


# ---------------------------------------------------------------------------
# 3. Existing menu helper good-error path is preserved
# ---------------------------------------------------------------------------


class TestMenuMissingOptionStillRaisesCorrectly:
    """The new wrapping logic only kicks in for HomeAssistantAPIError; the
    pre-existing CONFIG_MISSING_REQUIRED_FIELDS error for menu helpers is
    raised before any submit happens and must remain unchanged."""

    async def test_menu_helper_without_selection_raises_missing_fields(
        self,
    ) -> None:
        client = AsyncMock()
        # Real flow returns a menu — caller didn't supply a menu choice.
        client.start_config_flow = AsyncMock(
            return_value={
                "type": "menu",
                "flow_id": "menu-flow",
                "step_id": "user",
                "menu_options": ["sensor", "binary_sensor"],
            }
        )
        # No submit should happen — the missing-option check fires first.
        client.submit_config_flow_step = AsyncMock(
            side_effect=AssertionError("submit must not be called")
        )
        client.abort_config_flow = AsyncMock(return_value={})

        with pytest.raises(ToolError) as exc_info:
            await create_flow_helper(client, "template", {"name": "x"})

        body = _parse_tool_error(exc_info.value)
        assert body["error"]["code"] == "CONFIG_MISSING_REQUIRED_FIELDS"
        # Menu options must surface so the caller knows what to pick.
        assert body.get("menu_options") == ["sensor", "binary_sensor"]
        # The wrapping path's status_code field must NOT be present —
        # this confirms we're still on the legacy code path.
        assert "status_code" not in body
        client.submit_config_flow_step.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Direct test of _handle_flow_steps wrapper for options/update flows
# ---------------------------------------------------------------------------


class TestHandleFlowStepsOptionsFlowError:
    """Options (update) flows go through _handle_flow_steps with a custom
    submit_fn. The wrapping must still apply."""

    async def test_options_flow_400_with_field_errors_is_wrapped(self) -> None:
        """Direct exercise of _handle_flow_steps to ensure the options
        submit_fn path also surfaces structured errors."""
        api_err = HomeAssistantAPIError(
            "API error: 400 - Bad Request",
            status_code=400,
            response_data={"errors": {"window_size": "value_too_small"}},
        )
        submit_fn = AsyncMock(side_effect=api_err)

        initial_step = {
            "type": "form",
            "flow_id": "opt-flow",
            "step_id": "init",
            "data_schema": [{"name": "window_size"}],
        }

        # Issue #1149: the structured-error branch now also tries to fetch
        # the data_schema for context. Wire the client mock so the
        # introspection flow (`start_config_flow` -> a form with a usable
        # `flow_id`) succeeds, and the matching `abort_config_flow` is
        # awaitable. Without an explicit return value AsyncMock surfaces
        # auto-generated coroutines that never await cleanly.
        client = AsyncMock()
        intro_schema = [{"name": "window_size", "selector": {"number": {}}}]
        client.start_config_flow = AsyncMock(return_value={
            "type": "form",
            "flow_id": "intro-opt",
            "step_id": "init",
            "data_schema": intro_schema,
        })
        client.abort_config_flow = AsyncMock(return_value={})

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_steps(
                client=client,
                flow_id="opt-flow",
                initial_step=initial_step,
                config={"window_size": 1},
                submit_fn=submit_fn,
                helper_type="filter",
            )

        body = _parse_tool_error(exc_info.value)
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert body.get("field_errors") == {"window_size": "value_too_small"}
        assert body.get("status_code") == 400
        # Issue #1149: data_schema is now attached alongside field_errors.
        assert body.get("data_schema") == intro_schema
        assert body.get("flow_id") == "opt-flow"
