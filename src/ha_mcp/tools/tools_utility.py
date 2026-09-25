"""
Utility tools for Home Assistant MCP server.

Template and condition evaluation. The log tools that used to share this module
are in ``tools_logs`` and its helpers, split out under `.gemini/styleguide.md` §
Tool Consolidation and Module Size.
"""

import logging
from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..client.rest_client import HomeAssistantCommandError, HomeAssistantCommandTimeout
from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .util_helpers import JSON_STRING_COERCION

logger = logging.getLogger(__name__)

TEMPLATE_ERROR_SUGGESTIONS = [
    "Fix the template at the reported line, if one is given, then evaluate it again",
    "Use default values for missing data: {{ states('sensor.temp') | float(0) }}",
    "Verify entity_ids exist using ha_get_state() or ha_search()",
    "Pass values the template expects through 'variables'",
]

TEMPLATE_TIMEOUT_SUGGESTIONS = ["Simplify the template or raise 'timeout'"]

CONDITION_ERROR_SUGGESTIONS = [
    "Check the condition against the Home Assistant condition schema",
    "Verify entity_ids exist using ha_get_state() or ha_search()",
    "See ha_get_skill_guide for native condition examples",
]

# Core rejects a render that outlives its ``timeout`` with this message (code
# ``template_error``, which it also uses for other template failures).
HA_RENDER_TIMEOUT_TEXT = "Exceeded maximum execution time"


def _command_error_message(exc: HomeAssistantCommandError) -> str:
    return str(exc).removeprefix("Command failed: ")


def _render_outcome(
    error: str,
    report_errors: bool,
    result: dict[str, Any],
    diagnosis: dict[str, Any] | None,
) -> tuple[str, bool]:
    """The error to report for a failed render, and whether it timed out."""
    if not result.get("no_verdict"):
        return error, False
    # HA sent no verdict. With report_errors it always sends one, so the render
    # outran the wait; without it, silence is how HA answers a failed render.
    # The in-process render, when there is one, is the only place the actual
    # outcome is known.
    if diagnosis is not None:
        if diagnosis.get("error"):
            return diagnosis["error"], False
        if diagnosis.get("stage") == "timeout":
            return error, True
    return error, report_errors


class UtilityTools:
    def __init__(self, client: Any) -> None:
        self._client = client

    async def eval_template(
        self,
        template: str | None,
        condition: dict[str, Any] | None,
        variables: dict[str, Any] | None,
        strict: bool,
        timeout: int,
        report_errors: bool,
    ) -> dict[str, Any]:
        if (template is None) == (condition is None):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Provide exactly one of 'template' or 'condition'",
                    suggestions=[
                        "Use 'template' to render a Jinja2 template",
                        "Use 'condition' to test an automation condition",
                    ],
                )
            )
        try:
            if condition is not None:
                return await self._test_condition(condition, variables)
            assert template is not None
            return await self._render_template(
                template, variables, strict, timeout, report_errors
            )
        except ToolError:
            raise
        except Exception as e:
            error_str = str(e)
            suggestions = ["Check Home Assistant WebSocket connection"]

            if "403" in error_str and "Forbidden" in error_str:
                suggestions = [
                    "The request was blocked (403 Forbidden) - this may be caused by:",
                    "  • Reverse proxy security rules (Apache, Nginx, Traefik)",
                    "  • Rate limiting from multiple simultaneous requests",
                    "  • Complex template triggering security filters",
                    "Try simplifying the template (remove newlines, reduce complexity)",
                    "Break complex templates into multiple simpler calls",
                    "Use ha_report_issue to check Home Assistant logs for details",
                ] + suggestions

            exception_to_structured_error(
                e,
                context=(
                    {"condition": condition}
                    if condition is not None
                    else {"template": template}
                ),
                suggestions=suggestions,
            )
            raise  # unreachable: exception_to_structured_error always raises

    async def _render_template(
        self,
        template: str,
        variables: dict[str, Any] | None,
        strict: bool,
        timeout: int,
        report_errors: bool,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "type": "render_template",
            "template": template,
            "timeout": timeout,
            "report_errors": report_errors,
            "strict": strict,
        }
        if variables is not None:
            message["variables"] = variables

        result = await self._client.send_websocket_message(message)

        if result.get("success"):
            response: dict[str, Any] = {
                "success": True,
                "template": template,
                "result": result.get("result"),
                "listeners": result.get("listeners", {}),
            }
            if result.get("warnings"):
                response["warnings"] = result["warnings"]
            return response

        await self._raise_render_failure(
            template, variables, strict, timeout, report_errors, result
        )
        raise AssertionError("unreachable: _raise_render_failure always raises")

    async def _raise_render_failure(
        self,
        template: str,
        variables: dict[str, Any] | None,
        strict: bool,
        timeout: int,
        report_errors: bool,
        result: dict[str, Any],
    ) -> NoReturn:
        reported = result.get("error")
        error = reported if isinstance(reported, str) else "Template evaluation failed"
        context: dict[str, Any] = {"template": template}
        if result.get("error_code"):
            context["ha_error_code"] = result["error_code"]
        if result.get("warnings"):
            context["warnings"] = result["warnings"]

        if result.get("client_error"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    error,
                    context=context,
                    suggestions=["Check Home Assistant WebSocket connection"],
                )
            )
        if HA_RENDER_TIMEOUT_TEXT in error:
            # A second render would only time out again.
            raise_tool_error(
                create_error_response(
                    ErrorCode.TIMEOUT_OPERATION,
                    error,
                    context=context,
                    suggestions=TEMPLATE_TIMEOUT_SUGGESTIONS,
                )
            )

        diagnosis, unavailable = await self._diagnose_template(
            template, variables, strict, timeout
        )
        error, timed_out = _render_outcome(error, report_errors, result, diagnosis)
        if diagnosis is not None and isinstance(diagnosis.get("line"), int):
            context["line"] = diagnosis["line"]
            if diagnosis.get("source_line"):
                context["source_line"] = diagnosis["source_line"]
        elif unavailable:
            context["line_unavailable"] = unavailable

        raise_tool_error(
            create_error_response(
                ErrorCode.TIMEOUT_OPERATION
                if timed_out
                else ErrorCode.VALIDATION_FAILED,
                error,
                context=context,
                suggestions=(
                    TEMPLATE_TIMEOUT_SUGGESTIONS
                    if timed_out
                    else TEMPLATE_ERROR_SUGGESTIONS
                ),
            )
        )

    async def _diagnose_template(
        self,
        template: str,
        variables: dict[str, Any] | None,
        strict: bool,
        timeout: int,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Ask the ha_mcp_tools component where in the template the render fails.

        Home Assistant reports a template error without its line. The component
        renders the same template in-process, where the Jinja exception still
        carries it. Returns ``(diagnosis, None)``, or ``(None, reason)`` when a
        component that advertises the capability could not answer; without
        the capability both are ``None`` and the error stands as Home
        Assistant reported it.
        """
        caps = await get_component_caps(self._client)
        if not component_supports(caps, "template_diagnose"):
            return None, None
        params: dict[str, Any] = {
            "template": template,
            "strict": strict,
            "timeout": timeout,
            # The component may spend up to ``timeout`` in its guarded render.
            "_wait_timeout": timeout + 10,
        }
        if variables is not None:
            params["variables"] = variables
        try:
            ws = await get_websocket_client(
                url=self._client.base_url,
                token=self._client.token,
                verify_ssl=getattr(self._client, "verify_ssl", None),
            )
            raw = await ws.send_command("ha_mcp_tools/template_diagnose", **params)
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
            if is_unknown_command(exc):
                invalidate_caps(self._client)
                return None, None
            logger.warning("ha_mcp_tools/template_diagnose failed: %r", exc)
            return None, f"line lookup failed: {exc}"
        except Exception as exc:
            logger.warning("ha_mcp_tools/template_diagnose unavailable", exc_info=True)
            return None, f"line lookup failed: {exc}"
        diagnosis = raw.get("result")
        if not isinstance(diagnosis, dict):
            return None, "line lookup returned no diagnosis"
        return diagnosis, None

    async def _test_condition(
        self,
        condition: dict[str, Any],
        variables: dict[str, Any] | None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"condition": condition}
        if variables is not None:
            params["variables"] = variables
        ws = await get_websocket_client(
            url=self._client.base_url,
            token=self._client.token,
            verify_ssl=getattr(self._client, "verify_ssl", None),
        )
        try:
            response = await ws.send_command("test_condition", **params)
        except HomeAssistantCommandError as exc:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    _command_error_message(exc),
                    context={"condition": condition, "ha_error_code": exc.code},
                    suggestions=CONDITION_ERROR_SUGGESTIONS,
                )
            )
            raise AssertionError("unreachable: raise_tool_error always raises") from exc
        result = response.get("result") or {}
        data: dict[str, Any] = {
            "success": True,
            "condition": condition,
            "result": result.get("result"),
        }
        # Home Assistant 2026.7+ returns template errors hit while checking a
        # template condition that still evaluated (e.g. an undefined variable).
        if result.get("template_errors"):
            data["warnings"] = list(result["template_errors"])
        return data


def register_utility_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant utility tools."""
    tools = UtilityTools(client)

    @mcp.tool(
        tags={"Utilities"},
        annotations={
            "openWorldHint": False,
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Evaluate Template",
        },
    )
    @log_tool_usage
    async def ha_eval_template(
        template: Annotated[
            str | None,
            Field(
                description="Jinja2 template to render. Omit when using 'condition'."
            ),
        ] = None,
        condition: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Automation condition to test instead of a template, e.g. "
                    "{'condition': 'numeric_state', 'entity_id': 'sensor.temp', "
                    "'above': 20}. Test several at once with {'condition': 'and', "
                    "'conditions': [...]}."
                )
            ),
        ] = None,
        variables: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Variables available to the template or condition, e.g. "
                    "sample trigger data: {'trigger': {'to_state': {'state': 'on'}}}"
                )
            ),
        ] = None,
        strict: Annotated[
            bool,
            Field(
                description=(
                    "Template only: fail on undefined variables instead of "
                    "rendering them as empty (reported in warnings when "
                    "report_errors is on)"
                )
            ),
        ] = False,
        timeout: Annotated[
            int,
            Field(
                ge=1,
                le=60,
                description="Template only: maximum render time in seconds",
            ),
        ] = 3,
        report_errors: Annotated[
            bool,
            Field(
                description=(
                    "Template only: have Home Assistant report render errors and "
                    "warnings. With false, a failed render is only written to "
                    "Home Assistant's log."
                )
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Execute a Jinja2 template render, or an automation condition check, in Home Assistant.

        Renders templates with Home Assistant's template engine (all states,
        functions and filters available), or checks a condition block the way
        an automation would and returns true/false.

        When to use: any one-shot answer DERIVED from current HA state (an
        average across sensors, a count of entities matching a condition, a
        rendered message with live values), and testing a template or condition
        before embedding it in an automation. One render beats fetching N states
        and doing the math yourself.

        When NOT to use: a plain single-entity value is ha_get_state /
        ha_search. Templates in automation `condition:` / `trigger:` positions
        or templated action names: prefer native constructs, which are validated
        at config load, where a template only errors at runtime and a non-truthy
        render is silently false — see ha_get_skill_guide for the anti-pattern
        list. Test the native condition here with `condition`.

        Results: a render that works but hits something suspect (an undefined
        variable, a missing attribute) still returns its result with the
        messages in `warnings`; strict=true makes those hard errors. A failed
        render returns Home Assistant's error text and, with the ha_mcp_tools
        component installed, the template `line` and `source_line` it failed on
        when the error names one.

        EXAMPLES:
        - ha_eval_template(template="{{ states.light | selectattr('state', 'eq', 'on') | list | count }}")
        - ha_eval_template(template="Hello {{ name }}", variables={"name": "Alice"})
        - ha_eval_template(condition={"condition": "numeric_state", "entity_id": "sensor.temperature", "above": 20})
        """
        return await tools.eval_template(
            template, condition, variables, strict, timeout, report_errors
        )
