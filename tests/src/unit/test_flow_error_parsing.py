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
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools.config_entry_flow import (
    create_config_entry,
    create_flow_helper,
    set_config_subentry,
    update_config_entry_options,
)
from ha_mcp.tools.config_entry_flow_errors import (
    FlowRef,
    _parse_flow_api_error,
    raise_form_errors,
)
from ha_mcp.tools.config_entry_flow_walker import _handle_flow_steps
from ha_mcp.tools.config_entry_reconfigure import _run_reconfigure_flow


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
        live_schema = [
            {"name": "entity_id", "required": True, "selector": {"entity": {}}},
        ]

        async def start_flow(handler: str) -> dict[str, Any]:
            return {
                "type": "form",
                "flow_id": "flow-1",
                "step_id": "user",
                "data_schema": live_schema,
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
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        # Field errors are exposed at the top level (via context).
        assert body.get("field_errors") == {"entity_id": "not_a_sensor"}
        assert body.get("status_code") == 400
        # Issue #1149: the data_schema is attached even when structured
        # field_errors are present — the LLM gets both "what failed" and
        # "what's accepted" to self-correct. It is the rejected form's.
        assert body.get("data_schema") == live_schema
        client.abort_config_flow.assert_called()


# ---------------------------------------------------------------------------
# 2. Unstructured errors -> data_schema attached
# ---------------------------------------------------------------------------


class TestUnstructuredErrorAttachesSchema:
    """When HA returns only a ``message`` (or nothing useful), the tool
    fetches the helper's data_schema and attaches it to the error."""

    async def test_unstructured_400_on_a_later_form_attaches_that_forms_schema(
        self,
    ) -> None:
        """A statistics flow fails on its second form: the error shows that
        form's fields, not the first form's again (#2632)."""
        second_schema = [{"name": "state_characteristic", "required": True}]
        start_calls: list[str] = []

        async def start_flow(handler: str) -> dict[str, Any]:
            start_calls.append(handler)
            return {
                "type": "form",
                "flow_id": "real-flow",
                "step_id": "user",
                "data_schema": [{"name": "entity_id"}],
            }

        api_err = HomeAssistantAPIError(
            "API error: 400 - Bad Request",
            status_code=400,
            # No "errors" map — only a vague message.
            response_data={"message": "Bad Request"},
        )
        second_form = {
            "type": "form",
            "flow_id": "real-flow",
            "step_id": "state_characteristic",
            "data_schema": second_schema,
        }

        client = AsyncMock()
        client.start_config_flow = AsyncMock(side_effect=start_flow)
        client.submit_config_flow_step = AsyncMock(side_effect=[second_form, api_err])
        client.abort_config_flow = AsyncMock(return_value={})

        with pytest.raises(ToolError) as exc_info:
            await create_flow_helper(
                client,
                "statistics",
                {"entity_id": "sensor.foo", "state_characteristic": "bogus"},
            )

        body = _parse_tool_error(exc_info.value)
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "field_errors" not in body
        assert body.get("data_schema") == second_schema
        assert body.get("step_id") == "state_characteristic"
        assert "400" in body["error"]["message"]
        # The live form answers it: no second, introspection flow.
        assert start_calls == ["statistics"]

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

        client = AsyncMock()

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
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert body.get("field_errors") == {"window_size": "value_too_small"}
        assert body.get("status_code") == 400
        # Issue #1149: data_schema is attached alongside field_errors: the
        # options form's own, never a setup flow's (a different contract).
        assert body.get("data_schema") == initial_step["data_schema"]
        client.start_config_flow.assert_not_called()
        assert body.get("flow_id") == "opt-flow"

    async def test_reconfigure_error_uses_active_schema_without_new_setup_flow(
        self,
    ) -> None:
        """Reconfigure errors must not introspect the unrelated setup flow."""
        api_err = HomeAssistantAPIError(
            "API error: 400 - Bad Request",
            status_code=400,
            response_data={"errors": {"host": "invalid_host"}},
        )
        client = AsyncMock()
        submit_fn = AsyncMock(side_effect=api_err)
        active_schema = [{"name": "host", "required": True}]

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_steps(
                client=client,
                flow_id="reconfigure-flow",
                initial_step={
                    "type": "form",
                    "flow_id": "reconfigure-flow",
                    "step_id": "reconfigure",
                    "data_schema": active_schema,
                },
                config={"host": "bad"},
                submit_fn=submit_fn,
                helper_type="shelly",
                is_reconfigure=True,
            )

        body = _parse_tool_error(exc_info.value)
        assert body.get("data_schema") == active_schema
        assert body.get("status") == "apply_failed"
        client.start_config_flow.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Undrivable step types (issue #1814)
# ---------------------------------------------------------------------------


class TestUndrivableStepTypes:
    """External (browser/OAuth) steps cannot be driven over MCP, nor can a
    progress step in a walk without a FlowRef (a snapshot restore).

    ``_handle_flow_steps`` must surface them as SERVICE_CALL_FAILED with an
    actionable "complete in the HA UI" suggestion — NOT fall through to the
    INTERNAL_UNEXPECTED "Unexpected flow result type" branch, which loses
    the error code the ha_set_integration docstring documents.
    """

    @pytest.mark.parametrize(
        "step_type", ["external", "external_done", "progress", "progress_done"]
    )
    async def test_undrivable_step_raises_structured_error(
        self, step_type: str
    ) -> None:
        client = AsyncMock()
        initial_step = {
            "type": step_type,
            "flow_id": "oauth-flow",
            "step_id": "auth",
        }

        with pytest.raises(ToolError) as exc_info:
            await _handle_flow_steps(
                client=client,
                flow_id="oauth-flow",
                initial_step=initial_step,
                config={},
                helper_type="some_oauth_integration",
            )

        body = _parse_tool_error(exc_info.value)
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "cannot be completed via MCP" in body["error"]["message"]
        assert step_type in body["error"]["message"]
        # The actionable pointer to the HA UI must survive. A single
        # suggestion is emitted as the singular ``suggestion`` key.
        assert "Home Assistant UI" in body["error"].get("suggestion", "")
        # Nothing was submitted — the step is surfaced, not attempted.
        client.submit_config_flow_step.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Core's own text for form errors and abort reasons
# ---------------------------------------------------------------------------


def _strings_client(resources: dict[str, str]) -> AsyncMock:
    """A client whose ``frontend/get_translations`` answers ``resources``."""
    client = AsyncMock()
    client.send_websocket_message.return_value = {
        "success": True,
        "result": {"resources": resources},
    }
    return client


def _translation_requests(client: AsyncMock) -> list[tuple[str, list[str]]]:
    return [
        (c.args[0]["category"], c.args[0]["integration"])
        for c in client.send_websocket_message.await_args_list
        if c.args[0].get("type") == "frontend/get_translations"
    ]


class TestFlowTextFromCore:
    """An abort reason or a form error is a key; the UI shows Core's string
    for it. The tool error carries that string next to the key, so the caller
    reads what Home Assistant says instead of guessing from the key."""

    async def test_setup_abort_carries_cores_reason_text(self) -> None:
        # Core's own single-instance abort, as its flow manager answers it:
        # the reason is defined once under the homeassistant integration.
        text = "Already configured. Only a single configuration possible."
        client = _strings_client(
            {"component.homeassistant.config.abort.single_instance_allowed": text}
        )
        client.start_config_flow.return_value = {
            "type": "abort",
            "flow_id": "dup-flow",
            "handler": "sun",
            "reason": "single_instance_allowed",
            "translation_domain": "homeassistant",
        }

        with pytest.raises(ToolError) as exc_info:
            await create_config_entry(client, "sun", {})

        body = _parse_tool_error(exc_info.value)
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "single_instance_allowed" in body["error"]["message"]
        assert body["reason_text"] == text
        assert _translation_requests(client) == [("config", ["homeassistant"])]

    async def test_abort_without_a_core_string_keeps_only_the_reason(self) -> None:
        client = _strings_client({})
        client.start_config_flow.return_value = {
            "type": "abort",
            "flow_id": "f",
            "reason": "no_devices_found",
        }

        with pytest.raises(ToolError) as exc_info:
            await create_config_entry(client, "demo", {})

        body = _parse_tool_error(exc_info.value)
        assert "no_devices_found" in body["error"]["message"]
        assert "reason_text" not in body

    async def test_options_form_errors_carry_cores_text_with_placeholders(
        self,
    ) -> None:
        client = _strings_client(
            {"component.demo.options.error.cannot_connect": "Cannot reach {host}."}
        )
        client.get_config_entry.return_value = {"entry_id": "e1", "domain": "demo"}
        client.start_options_flow.return_value = {
            "type": "form",
            "flow_id": "opt-flow",
            "step_id": "init",
            "data_schema": [{"name": "host"}],
            "errors": {"base": "cannot_connect", "host": "no_string_for_this"},
            "description_placeholders": {"host": "10.0.0.2"},
        }

        with pytest.raises(ToolError) as exc_info:
            await update_config_entry_options(client, "e1", {"host": "10.0.0.2"})

        body = _parse_tool_error(exc_info.value)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        # The raw keys stay; a key Core ships no string for has no text.
        assert body["errors"] == {
            "base": "cannot_connect",
            "host": "no_string_for_this",
        }
        assert body["error_text"] == {"base": "Cannot reach 10.0.0.2."}
        assert _translation_requests(client) == [("options", ["demo"])]
        client.submit_options_flow_step.assert_not_called()

    async def test_subentry_abort_reads_its_type_under_the_parent_domain(
        self,
    ) -> None:
        client = _strings_client(
            {
                "component.ollama.config_subentries.conversation.abort."
                "entry_not_loaded": "The entry is not loaded."
            }
        )
        client.get_config_entry.return_value = {"entry_id": "e1", "domain": "ollama"}
        client.start_config_subentry_flow.return_value = {
            "type": "abort",
            "flow_id": "sub-flow",
            "reason": "entry_not_loaded",
        }

        with pytest.raises(ToolError) as exc_info:
            await set_config_subentry(client, "e1", "conversation", {})

        body = _parse_tool_error(exc_info.value)
        assert body["reason_text"] == "The entry is not loaded."
        assert _translation_requests(client) == [("config_subentries", ["ollama"])]

    async def test_abort_translation_domain_names_where_the_reason_lives(
        self,
    ) -> None:
        # translation_domain names the integration that owns the reason; a
        # subentry flow then reads it under that domain's config strings, as
        # the frontend does.
        client = _strings_client(
            {"component.shared.config.abort.cloud_not_connected": "Log in first."}
        )
        client.start_config_subentry_flow.return_value = {
            "type": "abort",
            "flow_id": "sub-flow",
            "reason": "cloud_not_connected",
            "translation_domain": "shared",
        }

        with pytest.raises(ToolError) as exc_info:
            await set_config_subentry(client, "e1", "conversation", {})

        body = _parse_tool_error(exc_info.value)
        assert body["reason_text"] == "Log in first."
        assert _translation_requests(client) == [("config", ["shared"])]
        client.get_config_entry.assert_not_called()

    async def test_reconfigure_abort_carries_cores_reason_text(self) -> None:
        client = _strings_client(
            {"component.shelly.config.abort.another_device": "Wrong device."}
        )
        client.start_reconfigure_flow.return_value = {
            "type": "abort",
            "flow_id": "rc-flow",
            "reason": "another_device",
        }

        with pytest.raises(ToolError) as exc_info:
            await _run_reconfigure_flow(
                client,
                domain="shelly",
                entry_id="e1",
                flow_config={"host": "10.0.0.3"},
                rollback_metadata={},
            )

        body = _parse_tool_error(exc_info.value)
        assert body["reason_text"] == "Wrong device."
        assert body["status"] == "apply_failed"


@pytest.mark.asyncio
async def test_form_errors_redact_the_schemas_password_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A reconfigure form arrives with the stored values in suggested_value;
    # under redact_secrets a password field's value must not ride into the
    # error's data_schema.
    monkeypatch.setattr(
        "ha_mcp.redaction.get_global_settings",
        lambda: SimpleNamespace(redact_secrets=True, redact_app_option_keys=""),
    )
    step = {
        "type": "form",
        "step_id": "reconfigure",
        "errors": {"base": "cannot_connect"},
        "data_schema": [
            {
                "name": "api_key",
                "selector": {"text": {"type": "password"}},
                "description": {"suggested_value": "sk-LIVE"},
            }
        ],
    }

    with pytest.raises(ToolError) as exc_info:
        await raise_form_errors(AsyncMock(), None, "f1", step)

    assert "sk-LIVE" not in str(exc_info.value)
    assert json.loads(str(exc_info.value))["data_schema"][0]["name"] == "api_key"


@pytest.mark.parametrize("path", ["config", "options", "config_subentries.device"])
def test_a_flow_ref_takes_the_flow_categories_core_has(path: str) -> None:
    assert FlowRef("demo", path).path == path


@pytest.mark.parametrize("path", ["", "option", "subentries.device"])
def test_a_flow_ref_refuses_a_path_core_has_no_flow_for(path: str) -> None:
    with pytest.raises(ValueError, match="flow path"):
        FlowRef("demo", path)
