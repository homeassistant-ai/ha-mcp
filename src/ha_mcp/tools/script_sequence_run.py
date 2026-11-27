"""Script run control: run actions without storing them, as the script
editor's Run on one action does, and the checks on ha_config_set_script's run
arguments.

The editor sends Core's ``execute_script`` with the action as its sequence.
Core validates the whole sequence before it runs any of it and answers once
the run has finished, with the response a ``stop`` step returned, if any.
"""

from __future__ import annotations

from typing import Any

from ..client.exceptions import HomeAssistantCommandTimeout, HomeAssistantError
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, raise_tool_error


async def run_script_sequence(
    client: Any, sequence: list[dict[str, Any]], variables: dict[str, Any] | None
) -> dict[str, Any]:
    """Core's ``execute_script`` reply as the tool's reply."""
    message: dict[str, Any] = {"type": "execute_script", "sequence": sequence}
    if variables:
        message["variables"] = variables
    try:
        result = await client.send_websocket_message(message)
    except HomeAssistantCommandTimeout:
        # Core answers only when the run ends; the run goes on without it.
        return {
            "success": True,
            "partial": True,
            "action": "run_sequence",
            "message": "Home Assistant took the actions but did not answer before "
            "the wait ended; they are likely still running.",
            "warnings": [
                "Do not send the actions again: a second run would repeat any "
                "step the first one has already done."
            ],
        }
    except HomeAssistantError as exc:
        exception_to_structured_error(exc, context={"action": "run_sequence"})
    if not result.get("success"):
        code = result.get("error_code")
        # A step that fails as it runs fails after the steps before it ran.
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_FAILED
                if code == "invalid_format"
                else ErrorCode.SERVICE_CALL_FAILED,
                f"Home Assistant did not complete the actions: {result.get('error')}",
                context={"action": "run_sequence", "error_code": code},
            )
        )
    reply = result.get("result") or {}
    return {
        "success": True,
        "action": "run_sequence",
        "response": reply.get("response"),
    }


def refuse_misused_run_control(
    script_id: str,
    run: str | None,
    variables: dict[str, Any] | None,
    run_sequence: list[dict[str, Any]] | None,
    *,
    has_write: bool,
    category: str | None,
) -> None:
    """Refuse run arguments that do not make one run call."""
    problem = None
    if run is not None and run_sequence is not None:
        problem = "run and run_sequence are separate calls"
    elif (run is None and run_sequence is None) or (
        run == "stop" and variables is not None
    ):
        problem = "variables only apply with run='start' or run_sequence"
    elif has_write:
        problem = (
            "run and run_sequence cannot be combined with config, "
            "python_transform or take_control_of_blueprint"
        )
    elif category is not None:
        problem = "category requires a config update"
    if problem:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                problem,
                suggestions=[
                    "Write the script config in one call, then start or stop "
                    + "it in a second call with only script_id and run",
                    "Pass variables together with run='start' or run_sequence",
                ],
                context={"action": "run", "script_id": script_id, "run": run},
            )
        )
