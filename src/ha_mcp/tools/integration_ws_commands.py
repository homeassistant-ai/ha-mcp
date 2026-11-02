"""An integration's own ``<domain>/`` WebSocket commands, the API its panel uses.

Core registers each command with its request schema; the component's
``integration_commands`` bridge describes those schemas and validates a call
against them. Core marks no command as read-only, so sending one is a write.
"""

import logging
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from .backup_access import guard_snapshot_route
from .config_entry_flow import is_component_domain, refuse_component_entry
from .core_contract import command_payload, integration_commands
from .helpers import extract_tool_error_message, raise_tool_error
from .util_helpers import is_streaming_ws_command, websocket_error_message

logger = logging.getLogger(__name__)

_LIST_SUGGESTION = (
    "List the entry's commands with "
    "ha_get_integration(entry_id=..., include_ws_commands=True)"
)


async def attach_ws_commands(client: Any, resp: dict[str, Any]) -> None:
    """Add the entry's ``<domain>/`` commands with Core's request schemas."""
    domain = (resp.get("entry") or {}).get("domain")
    if is_component_domain(domain):
        resp.setdefault("warnings", []).append(
            "include_ws_commands was ignored: ha-mcp's own entries take no ws_command"
        )
        return
    listing: dict[str, Any] = {
        "status": "unavailable",
        "reason": "the entry carries no domain",
    }
    if isinstance(domain, str):
        # Optional detail: a failed listing must not discard the entry read.
        try:
            listing = await integration_commands(client, domain)
        except ToolError as err:
            logger.warning(
                "Listing %s's WebSocket commands failed", domain, exc_info=True
            )
            listing = {
                "status": "unavailable",
                "reason": extract_tool_error_message(err),
            }
    if listing["status"] != "available":
        resp.setdefault("warnings", []).append(
            f"ws_commands not listed: {listing['reason']}"
        )
        return
    commands = listing["commands"]
    warnings = {text for row in commands for text in row.pop("warnings", [])}
    resp["ws_commands"] = commands
    if warnings:
        resp.setdefault("warnings", []).extend(sorted(warnings))


async def send_ws_command(
    client: Any, entry_id: str, command: str, data: dict[str, Any] | None
) -> dict[str, Any]:
    """Send one of the entry's own commands once Core's schema accepts it."""
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
    command_payload(command, payload)
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
    validation = await integration_commands(client, domain, command, payload)
    validated = validation["status"] == "validated"
    if validated and validation.get("valid") is not True:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_FAILED,
                "Home Assistant Core's schema for this command rejects the "
                "ws_data; nothing was sent",
                context={**context, "errors": validation.get("errors", [])},
                suggestions=[_LIST_SUGGESTION],
            )
        )
    result = await client.send_websocket_message({"type": command, **payload})
    if not result.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                websocket_error_message(result.get("error", "Operation failed")),
                context=context,
            )
        )
    resp: dict[str, Any] = {
        "success": True,
        "entry_id": entry_id,
        "ws_command": command,
        "result": result.get("result"),
    }
    if not validated:
        # Only a passed validation sends without a warning.
        reason = validation.get("reason") or (
            f"the component answered {validation['status']!r}, not a validation"
        )
        resp["warnings"] = [f"Not validated before sending: {reason}"]
    return resp
