"""Home Assistant's answers to a flow step as structured tool errors.

When Home Assistant rejects a step (a 400/422, a form back with ``errors``,
an ``abort``), the error carries what failed, Core's own text for it where
Core ships one, and the rejected form's fields, so the caller can answer the
step again (issues #1149, #1150).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools import integration_flow_steps
from ha_mcp.tools.config_entry_flow_errors import (
    FlowRef,
    _parse_flow_api_error,
    raise_form_errors,
)
from ha_mcp.tools.config_helpers.flow import _handle_set_config_subentry
from ha_mcp.tools.integration_flow_steps import FlowTarget, drive_flow


def _parse_tool_error(te: ToolError) -> dict[str, Any]:
    """Parse the JSON body of a ToolError raised via raise_tool_error()."""
    return json.loads(str(te))


def _form(flow_id: str, step_id: str, *fields: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "form",
        "flow_id": flow_id,
        "step_id": step_id,
        "data_schema": list(fields),
    }


async def _drive(
    client: Any,
    target: FlowTarget,
    start: dict[str, Any],
    config: dict[str, Any],
    *,
    flow_id: str | None = None,
) -> Any:
    """One step-driver call; with ``flow_id`` the flow counts as opened."""
    if flow_id is not None:
        integration_flow_steps._opened_flows[flow_id] = target.anchor
    return await drive_flow(
        client,
        target,
        flow_id=flow_id,
        config=config,
        menu_choice=None,
        abort=False,
        start=AsyncMock(return_value=start),
    )


async def _set_subentry(
    client: Any, subentry_type: str, config: dict[str, Any], subentry_id: str | None
) -> Any:
    return await _handle_set_config_subentry(
        client, None, "e1", subentry_type, subentry_id, config, False
    )


class TestRejectedSubmit:
    """A 400 on a submit says what failed and shows the rejected form."""

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

    def test_parse_falls_back_to_exception_message(self) -> None:
        """Without a response body the wrapper's message is kept."""
        api_err = HomeAssistantAPIError(
            "API error: 400 - Bad Request", status_code=400, response_data=None
        )
        parsed = _parse_flow_api_error(api_err)
        assert parsed["field_errors"] == {}
        assert "Bad Request" in parsed["message"]
        assert parsed["raw"] == {}

    async def test_field_errors_come_with_the_rejected_forms_schema(self) -> None:
        form = _form(
            "flow-1",
            "user",
            {"name": "entity_id", "required": True, "selector": {"entity": {}}},
        )
        client = AsyncMock()
        client.submit_config_flow_step.side_effect = HomeAssistantAPIError(
            "API error: 400 - Bad Request",
            status_code=400,
            response_data={"errors": {"entity_id": "not_a_sensor"}},
        )

        with pytest.raises(ToolError) as exc_info:
            await _drive(
                client, FlowTarget("setup", "filter"), form, {"entity_id": "light.foo"}
            )

        body = _parse_tool_error(exc_info.value)
        assert body["success"] is False
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert body["field_errors"] == {"entity_id": "not_a_sensor"}
        assert body["status_code"] == 400
        assert body["data_schema"] == form["data_schema"]
        # The flow stays open to be answered again.
        client.abort_config_flow.assert_not_called()

    async def test_an_unstructured_400_on_a_later_form_shows_that_form(self) -> None:
        """A statistics flow fails on its second form: the error shows that
        form's fields, not the first form's again (#2632)."""
        second = _form(
            "real-flow",
            "state_characteristic",
            {"name": "state_characteristic", "required": True},
        )
        client = AsyncMock()
        client._request.return_value = second
        client.submit_config_flow_step.side_effect = HomeAssistantAPIError(
            "API error: 400 - Bad Request",
            status_code=400,
            response_data={"message": "Bad Request"},
        )

        with pytest.raises(ToolError) as exc_info:
            await _drive(
                client,
                FlowTarget("setup", "statistics"),
                {},
                {"state_characteristic": "bogus"},
                flow_id="real-flow",
            )

        body = _parse_tool_error(exc_info.value)
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "field_errors" not in body
        assert body["data_schema"] == second["data_schema"]
        assert body["step_id"] == "state_characteristic"
        assert "400" in body["error"]["message"]

    async def test_a_rejected_reconfigure_submit_reports_nothing_applied(self) -> None:
        form = _form("rc-flow", "reconfigure", {"name": "host", "required": True})
        client = AsyncMock()
        client.submit_config_flow_step.side_effect = HomeAssistantAPIError(
            "API error: 400 - Bad Request",
            status_code=400,
            response_data={"errors": {"host": "invalid_host"}},
        )

        with pytest.raises(ToolError) as exc_info:
            await _drive(
                client, FlowTarget("reconfigure", "shelly", "e1"), form, {"host": "bad"}
            )

        body = _parse_tool_error(exc_info.value)
        assert body["data_schema"] == form["data_schema"]
        assert body["status"] == "apply_failed"


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
    """The tool error carries Core's string for an abort reason or form error."""

    async def test_abort_without_a_core_string_keeps_only_the_reason(self) -> None:
        client = _strings_client({})
        aborted = {"type": "abort", "flow_id": "f", "reason": "no_devices_found"}

        with pytest.raises(ToolError) as exc_info:
            await _drive(client, FlowTarget("setup", "demo"), aborted, {})

        body = _parse_tool_error(exc_info.value)
        assert "no_devices_found" in body["error"]["message"]
        assert "reason_text" not in body

    async def test_options_form_errors_carry_cores_text_with_placeholders(
        self,
    ) -> None:
        client = _strings_client(
            {"component.demo.options.error.cannot_connect": "Cannot reach {host}."}
        )
        form = _form("opt-flow", "init", {"name": "host", "required": True})
        client.submit_options_flow_step.return_value = {
            **form,
            "errors": {"base": "cannot_connect", "host": "no_string_for_this"},
            "description_placeholders": {"host": "10.0.0.2"},
        }

        with pytest.raises(ToolError) as exc_info:
            await _drive(
                client, FlowTarget("options", "demo", "e1"), form, {"host": "10.0.0.2"}
            )

        body = _parse_tool_error(exc_info.value)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        # The raw keys stay; a key Core ships no string for has no text.
        assert body["errors"] == {
            "base": "cannot_connect",
            "host": "no_string_for_this",
        }
        assert body["error_text"] == {"base": "Cannot reach 10.0.0.2."}
        assert body["fields"] == [{"name": "host", "required": True}]
        assert "data_schema" not in body
        assert _translation_requests(client) == [("options", ["demo"])]
        client.submit_options_flow_step.assert_awaited_once()

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
            await _set_subentry(client, "conversation", {}, None)

        body = _parse_tool_error(exc_info.value)
        assert body["reason_text"] == "The entry is not loaded."
        assert _translation_requests(client) == [("config_subentries", ["ollama"])]

    async def test_abort_translation_domain_names_where_the_reason_lives(
        self,
    ) -> None:
        # translation_domain names the integration that owns the reason; a
        # subentry flow then reads it under that domain's config strings, as
        # the frontend's dev branch does (the release Core 2026.8.0 pins reads
        # config_subentries.<type>.abort).
        client = _strings_client(
            {"component.shared.config.abort.cloud_not_connected": "Log in first."}
        )
        client.get_config_entry.return_value = {"entry_id": "e1", "domain": "ollama"}
        client.start_config_subentry_flow.return_value = {
            "type": "abort",
            "flow_id": "sub-flow",
            "reason": "cloud_not_connected",
            "translation_domain": "shared",
        }

        with pytest.raises(ToolError) as exc_info:
            await _set_subentry(client, "conversation", {}, None)

        body = _parse_tool_error(exc_info.value)
        assert body["reason_text"] == "Log in first."
        assert _translation_requests(client) == [("config", ["shared"])]

    async def test_reconfigure_abort_carries_cores_reason_text(self) -> None:
        client = _strings_client(
            {"component.shelly.config.abort.another_device": "Wrong device."}
        )
        aborted = {"type": "abort", "flow_id": "rc-flow", "reason": "another_device"}

        with pytest.raises(ToolError) as exc_info:
            await _drive(
                client,
                FlowTarget("reconfigure", "shelly", "e1"),
                aborted,
                {"host": "10.0.0.3"},
            )

        body = _parse_tool_error(exc_info.value)
        assert body["reason_text"] == "Wrong device."
        assert body["status"] == "apply_failed"

    async def test_a_reconfigure_form_back_with_errors_is_refused_and_stays_open(
        self,
    ) -> None:
        client = _strings_client({})
        form = _form("rc-flow", "reconfigure", {"name": "file_path", "required": True})
        client.submit_config_flow_step.return_value = {
            **form,
            "errors": {"base": "not_valid"},
        }

        with pytest.raises(ToolError) as exc_info:
            await _drive(
                client,
                FlowTarget("reconfigure", "filesize", "e1"),
                form,
                {"file_path": "/nope/x.txt"},
            )

        body = json.loads(str(exc_info.value))
        assert body["flow_id"] == "rc-flow"
        assert body["errors"] == {"base": "not_valid"}
        assert body["status"] == "apply_failed"
        client.submit_config_flow_step.assert_awaited_once()
        client.abort_config_flow.assert_not_awaited()

    @pytest.mark.parametrize(
        ("subentry_id", "status"), [("s1", "apply_failed"), (None, None)]
    )
    async def test_a_subentry_form_back_with_errors_is_refused(
        self, subentry_id: str | None, status: str | None
    ) -> None:
        client = _strings_client({})
        client.get_config_entry.return_value = {"entry_id": "e1", "domain": "demo"}
        form = _form(
            "sub-flow",
            "reconfigure" if subentry_id else "user",
            {"name": "name", "required": True},
        )
        client.start_config_subentry_flow.return_value = form
        client.submit_config_subentry_flow_step.return_value = {
            **form,
            "errors": {"base": "cannot_connect"},
        }

        with pytest.raises(ToolError) as exc_info:
            await _set_subentry(client, "device", {"name": "x"}, subentry_id)

        body = _parse_tool_error(exc_info.value)
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert body["errors"] == {"base": "cannot_connect"}
        assert body.get("status") == status
        client.submit_config_subentry_flow_step.assert_awaited_once()
        client.abort_config_subentry_flow.assert_not_awaited()


@pytest.mark.asyncio
async def test_form_errors_redact_the_schemas_password_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A reconfigure form carries stored values in suggested_value.
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
    assert json.loads(str(exc_info.value))["fields"][0]["name"] == "api_key"


@pytest.mark.parametrize("path", ["config", "options", "config_subentries.device"])
def test_a_flow_ref_takes_the_flow_categories_core_has(path: str) -> None:
    assert FlowRef("demo", path).path == path


@pytest.mark.parametrize("path", ["", "option", "subentries.device"])
def test_a_flow_ref_refuses_a_path_core_has_no_flow_for(path: str) -> None:
    with pytest.raises(ValueError, match="flow path"):
        FlowRef("demo", path)


@pytest.mark.asyncio
async def test_a_form_error_whose_strings_cannot_be_read_warns_at_top_level() -> None:
    client = AsyncMock()
    client.send_websocket_message.side_effect = RuntimeError("no answer")
    step = {"type": "form", "step_id": "user", "errors": {"base": "not_valid"}}

    with pytest.raises(ToolError) as exc_info:
        await raise_form_errors(client, FlowRef("filesize", "config"), "f1", step)

    body = json.loads(str(exc_info.value))
    assert "error_text" not in body
    assert any("could not be read" in w for w in body["warnings"])
