"""Unit tests for ``ha_eval_template``'s template and condition paths (#2522).

The client's ``render_template`` envelope (``success`` / ``result`` / ``error``
/ ``warnings``) is covered in ``test_rest_client_ws_transport_failure.py``;
these tests pin how the tool turns it into a response or a ``ToolError``, how
the component's ``template_diagnose`` adds the failing line, and the
``test_condition`` route.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    RENDER_NO_VERDICT_WITHOUT_REPORT_ERRORS,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_utility import UtilityTools

_MODULE = "ha_mcp.tools.tools_utility"


def _tools(render_result: dict[str, Any] | None = None) -> UtilityTools:
    client = MagicMock()
    client.base_url = "http://ha.local:8123"
    client.token = "token"
    client.send_websocket_message = AsyncMock(return_value=render_result or {})
    return UtilityTools(client)


def _error_payload(exc: ToolError) -> dict[str, Any]:
    return json.loads(str(exc))


async def _eval(tools: UtilityTools, **kwargs: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "template": None,
        "condition": None,
        "variables": None,
        "strict": False,
        "timeout": 3,
        "report_errors": True,
    }
    args.update(kwargs)
    return await tools.eval_template(**args)


def _caps(*capabilities: str) -> Any:
    return patch(
        f"{_MODULE}.get_component_caps",
        new=AsyncMock(return_value=MagicMock(capabilities=frozenset(capabilities))),
    )


def _ws(send_command: AsyncMock) -> Any:
    ws = MagicMock()
    ws.send_command = send_command
    return patch(f"{_MODULE}.get_websocket_client", new=AsyncMock(return_value=ws))


class TestArguments:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [{}, {"template": "{{ 1 }}", "condition": {"condition": "state"}}],
    )
    async def test_exactly_one_of_template_or_condition(self, kwargs) -> None:
        with pytest.raises(ToolError) as exc:
            await _eval(_tools(), **kwargs)
        assert _error_payload(exc.value)["error"]["code"] == (
            "VALIDATION_INVALID_PARAMETER"
        )

    @pytest.mark.asyncio
    async def test_strict_and_variables_are_sent_to_render_template(self) -> None:
        tools = _tools({"success": True, "result": 42, "listeners": {}})
        await _eval(tools, template="{{ foo * 2 }}", variables={"foo": 21}, strict=True)
        message = tools._client.send_websocket_message.await_args.args[0]
        assert message["type"] == "render_template"
        assert message["variables"] == {"foo": 21}
        assert message["strict"] is True


_ZERO_DIV = "ZeroDivisionError: division by zero"


class TestTemplateResults:
    @pytest.mark.asyncio
    async def test_warnings_are_returned_with_the_result(self) -> None:
        tools = _tools(
            {
                "success": True,
                "result": "x",
                "listeners": {},
                "warnings": ["'undefined_var' is undefined"],
            }
        )
        data = await _eval(tools, template="{{ undefined_var }}x")
        assert data == {
            "success": True,
            "template": "{{ undefined_var }}x",
            "result": "x",
            "listeners": {},
            "warnings": ["'undefined_var' is undefined"],
        }

    @pytest.mark.asyncio
    async def test_home_assistant_error_text_is_the_message(self) -> None:
        tools = _tools(
            {
                "success": False,
                "error": "UndefinedError: 'dict object' has no attribute 'split'",
                "warnings": ["'x' is undefined"],
            }
        )
        with _caps(), pytest.raises(ToolError) as exc:
            await _eval(tools, template="{{ d.split('-') }}")
        error = _error_payload(exc.value)
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert error["error"]["message"] == (
            "UndefinedError: 'dict object' has no attribute 'split'"
        )
        assert error["warnings"] == ["'x' is undefined"]
        assert "line" not in error
        assert "line_unavailable" not in error

    @pytest.mark.asyncio
    async def test_component_diagnosis_adds_the_failing_line(self) -> None:
        tools = _tools({"success": False, "error": _ZERO_DIV})
        diagnose = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "stage": "render",
                    "error": _ZERO_DIV,
                    "line": 3,
                    "source_line": "{{ 1/0 }}",
                },
            }
        )
        with (
            _caps("template_diagnose"),
            _ws(diagnose),
            pytest.raises(ToolError) as exc,
        ):
            await _eval(tools, template="a\n\n{{ 1/0 }}", variables={"v": 1})
        error = _error_payload(exc.value)
        assert error["line"] == 3
        assert error["source_line"] == "{{ 1/0 }}"
        assert error["error"]["message"] == _ZERO_DIV
        diagnose.assert_awaited_once_with(
            "ha_mcp_tools/template_diagnose",
            template="a\n\n{{ 1/0 }}",
            strict=False,
            timeout=3,
            _wait_timeout=13,
            variables={"v": 1},
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "failure",
        [
            HomeAssistantCommandTimeout("no answer"),
            HomeAssistantCommandError("Command failed: bad", "invalid_format"),
            HomeAssistantConnectionError("socket closed"),
        ],
    )
    async def test_failed_line_lookup_keeps_home_assistant_error(self, failure) -> None:
        tools = _tools({"success": False, "error": _ZERO_DIV})
        with (
            _caps("template_diagnose"),
            _ws(AsyncMock(side_effect=failure)),
            patch(f"{_MODULE}.invalidate_caps") as invalidate,
            pytest.raises(ToolError) as exc,
        ):
            await _eval(tools, template="{{ 1/0 }}")
        error = _error_payload(exc.value)
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert error["error"]["message"] == _ZERO_DIV
        assert "line" not in error
        assert error["line_unavailable"].startswith("line lookup failed")
        invalidate.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_command_invalidates_the_cached_caps(self) -> None:
        tools = _tools({"success": False, "error": _ZERO_DIV})
        diagnose = AsyncMock(
            side_effect=HomeAssistantCommandError("Command failed", "unknown_command")
        )
        with (
            _caps("template_diagnose"),
            _ws(diagnose),
            patch(f"{_MODULE}.invalidate_caps") as invalidate,
            pytest.raises(ToolError) as exc,
        ):
            await _eval(tools, template="{{ 1/0 }}")
        invalidate.assert_called_once_with(tools._client)
        error = _error_payload(exc.value)
        assert error["error"]["message"] == _ZERO_DIV
        assert "line_unavailable" not in error

    @pytest.mark.asyncio
    async def test_no_verdict_uses_the_component_error(self) -> None:
        tools = _tools(
            {
                "success": False,
                "error": RENDER_NO_VERDICT_WITHOUT_REPORT_ERRORS,
                "no_verdict": True,
            }
        )
        diagnose = AsyncMock(
            return_value={
                "result": {
                    "stage": "compile",
                    "error": "TemplateSyntaxError: unexpected end of template",
                    "line": 1,
                }
            }
        )
        with (
            _caps("template_diagnose"),
            _ws(diagnose),
            pytest.raises(ToolError) as exc,
        ):
            await _eval(tools, template="{% if %}", report_errors=False)
        error = _error_payload(exc.value)
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert error["error"]["message"] == (
            "TemplateSyntaxError: unexpected end of template"
        )
        assert error["line"] == 1

    @pytest.mark.asyncio
    async def test_no_verdict_without_the_component_explains_itself(self) -> None:
        tools = _tools(
            {
                "success": False,
                "error": RENDER_NO_VERDICT_WITHOUT_REPORT_ERRORS,
                "no_verdict": True,
            }
        )
        with _caps(), pytest.raises(ToolError) as exc:
            await _eval(tools, template="{% if %}", report_errors=False)
        error = _error_payload(exc.value)
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert error["error"]["message"] == RENDER_NO_VERDICT_WITHOUT_REPORT_ERRORS

    @pytest.mark.asyncio
    async def test_no_verdict_that_timed_out_in_process_is_a_timeout(self) -> None:
        tools = _tools(
            {
                "success": False,
                "error": RENDER_NO_VERDICT_WITHOUT_REPORT_ERRORS,
                "no_verdict": True,
            }
        )
        diagnose = AsyncMock(return_value={"result": {"stage": "timeout"}})
        with (
            _caps("template_diagnose"),
            _ws(diagnose),
            pytest.raises(ToolError) as exc,
        ):
            await _eval(tools, template="{{ slow }}", report_errors=False)
        assert _error_payload(exc.value)["error"]["code"] == "TIMEOUT_OPERATION"

    @pytest.mark.asyncio
    async def test_lost_verdict_with_report_errors_is_a_timeout(self) -> None:
        tools = _tools(
            {
                "success": False,
                "error": "Event timeout - template result not received",
                "no_verdict": True,
            }
        )
        with _caps(), pytest.raises(ToolError) as exc:
            await _eval(tools, template="{{ 1 }}")
        assert _error_payload(exc.value)["error"]["code"] == "TIMEOUT_OPERATION"

    @pytest.mark.asyncio
    async def test_home_assistant_timeout_is_not_rendered_again(self) -> None:
        tools = _tools(
            {
                "success": False,
                "error": "Exceeded maximum execution time of 3.0s",
                "error_code": "template_error",
            }
        )
        diagnose = AsyncMock()
        with (
            _caps("template_diagnose"),
            _ws(diagnose),
            pytest.raises(ToolError) as exc,
        ):
            await _eval(tools, template="{% for i in range(10**9) %}{% endfor %}")
        error = _error_payload(exc.value)
        assert error["error"]["code"] == "TIMEOUT_OPERATION"
        assert error["ha_error_code"] == "template_error"
        diagnose.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_client_side_fault_is_not_blamed_on_the_template(self) -> None:
        tools = _tools({"success": False, "error": "boom", "client_error": True})
        diagnose = AsyncMock()
        with (
            _caps("template_diagnose"),
            _ws(diagnose),
            pytest.raises(ToolError) as exc,
        ):
            await _eval(tools, template="{{ 1 }}")
        assert _error_payload(exc.value)["error"]["code"] == "SERVICE_CALL_FAILED"
        diagnose.assert_not_awaited()


class TestCondition:
    @pytest.mark.asyncio
    async def test_condition_result_and_template_errors(self) -> None:
        send = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "result": False,
                    "template_errors": ["'undefined_var' is undefined"],
                },
            }
        )
        condition = {"condition": "template", "value_template": "{{ undefined_var }}"}
        with _ws(send):
            data = await _eval(_tools(), condition=condition, variables={"a": 1})
        assert data == {
            "success": True,
            "condition": condition,
            "result": False,
            "warnings": ["'undefined_var' is undefined"],
        }
        send.assert_awaited_once_with(
            "test_condition", condition=condition, variables={"a": 1}
        )

    @pytest.mark.asyncio
    async def test_condition_without_template_errors_has_no_warnings(self) -> None:
        send = AsyncMock(return_value={"success": True, "result": {"result": True}})
        condition = {"condition": "state", "entity_id": "sun.sun", "state": "x"}
        with _ws(send):
            data = await _eval(_tools(), condition=condition)
        assert data["result"] is True
        assert "warnings" not in data
        send.assert_awaited_once_with("test_condition", condition=condition)

    @pytest.mark.asyncio
    async def test_rejected_condition_keeps_home_assistant_message(self) -> None:
        send = AsyncMock(
            side_effect=HomeAssistantCommandError(
                'Command failed: Invalid condition "nope" specified',
                "home_assistant_error",
            )
        )
        with _ws(send), pytest.raises(ToolError) as exc:
            await _eval(_tools(), condition={"condition": "nope"})
        error = _error_payload(exc.value)
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert error["error"]["message"] == 'Invalid condition "nope" specified'
        assert error["ha_error_code"] == "home_assistant_error"

    @pytest.mark.asyncio
    async def test_transport_failure_names_the_condition(self) -> None:
        send = AsyncMock(side_effect=HomeAssistantConnectionError("socket closed"))
        condition = {"condition": "state", "entity_id": "sun.sun", "state": "x"}
        with _ws(send), pytest.raises(ToolError) as exc:
            await _eval(_tools(), condition=condition)
        error = _error_payload(exc.value)
        assert error["condition"] == condition
        assert "template" not in error
