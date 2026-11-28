"""Script run control: run actions without storing them, as the script
editor's Run on one action does, and the checks on ha_config_set_script's run
arguments.

The editor sends Core's ``execute_script`` with the action as its sequence.
Core checks each action's config before any step runs; a service's own data
is checked only when its step runs. Core answers once the run has finished,
with the response a ``stop`` step returned, if any.
"""

from __future__ import annotations

from typing import Any

from ..client.exceptions import (
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response, create_validation_error
from .backup_access import guard_snapshot_route
from .config_entry_flow import is_component_domain
from .helpers import (
    exception_to_structured_error,
    raise_core_refusal,
    raise_tool_error,
)

# What an action passes on rather than runs: an action name inside it is data.
_PAYLOAD_KEYS = frozenset(
    {
        "data",
        "data_template",
        "event_data",
        "event_data_template",
        "variables",
        "for_each",
    }
)


def _guard_actions(node: Any) -> None:
    """Hold each action, nested ones too, to ha_call_service's checks.

    A templated action name is refused: Core resolves it only when the step
    runs, after these checks.
    """
    if isinstance(node, list):
        for item in node:
            _guard_actions(item)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in _PAYLOAD_KEYS:
                continue
            if key in ("action", "service", "service_template") and isinstance(
                value, str
            ):
                if "{" in value:
                    raise_tool_error(
                        create_validation_error(
                            "run_sequence takes a literal action name such as "
                            "'light.turn_on', not a template.",
                            parameter="run_sequence",
                        )
                    )
                domain, _, service = value.partition(".")
                if is_component_domain(domain):
                    raise_tool_error(
                        create_validation_error(
                            "run_sequence cannot run 'ha_mcp_tools' actions; use "
                            "the dedicated ha-mcp tools.",
                            parameter="run_sequence",
                        )
                    )
                guard_snapshot_route(domain=domain, service=service)
            _guard_actions(value)


async def run_script_sequence(
    client: Any, sequence: list[dict[str, Any]], variables: dict[str, Any] | None
) -> dict[str, Any]:
    """Core's ``execute_script`` reply as the tool's reply."""
    _guard_actions(sequence)
    message: dict[str, Any] = {"type": "execute_script", "sequence": sequence}
    if variables:
        message["variables"] = variables
    try:
        result = await client.send_websocket_message(message)
    except HomeAssistantCommandNotSent as exc:
        exception_to_structured_error(exc, context={"action": "run_sequence"})
    except HomeAssistantConnectionError as exc:
        # Sent, then no answer: Core answers only when the run ends, and the
        # run goes on without the connection.
        return {
            "success": True,
            "partial": True,
            "action": "run_sequence",
            "message": "The actions were sent, but "
            + (
                "Home Assistant did not answer within the wait"
                if exc.timed_out
                else "the connection dropped before Home Assistant answered"
            )
            + "; they are most likely still running.",
            "warnings": [
                "Check the actions' effects before sending them again: a second "
                "run would repeat any step the first one has already done."
            ],
        }
    if not result.get("success"):
        # Core does not say which step failed, and a refusal can come after
        # earlier steps ran: a service's data is checked when its step runs.
        raise_core_refusal(
            "Home Assistant did not complete the actions",
            result,
            context={"action": "run_sequence"},
            suggestions=[
                "Steps before the failing one may already have run; check "
                "their effects before sending the actions again."
            ],
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
