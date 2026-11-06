"""An integration's own ``<domain>/`` WebSocket commands, the API its panel uses.

Core registers each command with its request schema; the component's
``integration_commands`` bridge describes those schemas. Core validates every
call against its schema before the handler runs, so a call is sent as given
and Core's error message and code are returned. Core marks no command as read-only, so
sending one is a write.
"""

import logging
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..client.rest_client import (
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .backup_access import guard_snapshot_route
from .config_entry_flow import is_component_domain, refuse_component_entry
from .config_entry_flow_errors import ReconfigureStatus
from .core_contract import command_payload, integration_commands
from .helpers import extract_tool_error_message, raise_tool_error, ws_failure_code
from .util_helpers import is_streaming_ws_command, websocket_error_message

logger = logging.getLogger(__name__)

_LIST_SUGGESTION = (
    "List the entry's commands with "
    "ha_get_integration(entry_id=..., include_ws_commands=True)"
)


def reject_ws_command_conflicts(
    entry_id: str | None, ws_command: str | None, **others: object
) -> None:
    """Reject a ws_command call without entry_id and ws_command, or with another mode."""
    problems = [
        f"a ws_command call needs {name}"
        for name, value in (("entry_id", entry_id), ("ws_command", ws_command))
        if value is None
    ]
    if given := [name for name, value in others.items() if value is not None]:
        problems.append(f"ws_command takes no {', '.join(given)}")
    if problems:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "; ".join(problems),
                context={"entry_id": entry_id, "ws_command": ws_command},
                suggestions=[
                    "Example: ha_set_integration(entry_id='abc123', "
                    "ws_command='knx/get_knx_project')"
                ],
            )
        )


async def attach_ws_commands(client: Any, resp: dict[str, Any]) -> None:
    """Add the entry's ``<domain>/`` commands with Core's request schemas."""
    domain = (resp.get("entry") or {}).get("domain")
    if is_component_domain(domain):
        resp.setdefault("warnings", []).append(
            "include_ws_commands was ignored: ha-mcp's own entries take no ws_command"
        )
        return
    listing = await _list_commands(client, domain)
    if listing["status"] != "available":
        resp.setdefault("warnings", []).append(
            f"ws_commands not listed: {listing['reason']}"
        )
        return
    commands = listing["commands"]
    warnings: set[str] = set()
    for row in commands:
        warnings.update(row.pop("warnings", []))
    resp["ws_commands"] = commands
    if warnings:
        resp.setdefault("warnings", []).extend(sorted(warnings))


async def _list_commands(client: Any, domain: Any) -> dict[str, Any]:
    """The bridge's listing of ``domain``'s commands; a failure is an
    ``unavailable`` listing, so it never discards the entry read."""
    if not isinstance(domain, str):
        return {"status": "unavailable", "reason": "the entry carries no domain"}
    try:
        return await integration_commands(client, domain)
    except ToolError as err:
        # The bridge logged the cause's traceback.
        logger.warning("Listing %s's WebSocket commands failed", domain)
        return {"status": "unavailable", "reason": extract_tool_error_message(err)}


async def send_ws_command(
    client: Any, entry_id: str, command: str, data: dict[str, Any] | None
) -> dict[str, Any]:
    """Send one of the entry's own commands; Core validates it against its schema."""
    payload = data or {}
    context = {"entry_id": entry_id, "ws_command": command}
    if is_streaming_ws_command(command):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"ws_command '{command}' streams events or answers in two "
                "phases; only one-shot request/response commands are sent.",
                context=context,
            )
        )
    message = command_payload(command, payload)
    entry = await client.get_config_entry(entry_id)
    domain = entry.get("domain")
    refuse_component_entry(
        domain, action="send a WebSocket command to", entry_id=entry_id
    )
    if not command.startswith(f"{domain}/"):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"ws_command must be one of this {domain} entry's own "
                f"'{domain}/' commands",
                context=context,
                suggestions=[_LIST_SUGGESTION],
            )
        )
    guard_snapshot_route(ws_command=command, ws_params=payload)
    try:
        result = await client.send_websocket_message(message)
    except HomeAssistantCommandNotSent:
        raise
    except HomeAssistantConnectionError as lost:
        # Sent, or possibly sent, with no answer back: the command may have run.
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"ws_command '{command}' got no answer from Home Assistant "
                f"({lost}); it may have been applied",
                context={**context, "status": ReconfigureStatus.APPLIED_BUT_UNVERIFIED},
                suggestions=[
                    "Do not retry automatically: a retry can repeat the change. "
                    "Check the integration's state first."
                ],
            )
        )
    if not result.get("success"):
        code = ws_failure_code(result)
        raise_tool_error(
            create_error_response(
                code,
                websocket_error_message(result.get("error", "Operation failed")),
                context=context,
                suggestions=[_LIST_SUGGESTION]
                if code is ErrorCode.VALIDATION_INVALID_PARAMETER
                else None,
            )
        )
    return {
        "success": True,
        "entry_id": entry_id,
        "ws_command": command,
        "result": result.get("result"),
    }
