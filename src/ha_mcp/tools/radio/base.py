"""Shared contract and helpers for ``ha_manage_radio`` per-radio handlers.

A handler module exposes:

- ``SUPPORTED``: ``dict[action_name, ActionSpec]`` describing every action it
  implements, used for validation, the destructive-confirm gate, and building
  actionable "unsupported action" errors.
- ``async def handle(client, action, args) -> dict``: execute one action.

Handlers send raw WebSocket commands through the REST client's
``send_websocket_message`` bridge (same path the ``ha_get_device`` enrichers
use), or call services via ``call_service`` for the few ZHA operations that are
service-only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from ..helpers import raise_tool_error

logger = logging.getLogger(__name__)

RADIOS = ("zwave", "zigbee", "matter", "thread")


@dataclass(frozen=True)
class ActionSpec:
    """Metadata for one radio action.

    summary: one-line description (surfaced in unsupported-action errors).
    destructive: requires ``confirm=True`` before it will run.
    long_running: starts an operation that completes out-of-band (inclusion,
        rebuild routes, firmware) — the result documents how to follow up.
    required: parameter names that must be present in ``args`` (or supplied as
        the top-level ``device_id``).
    """

    summary: str
    destructive: bool = False
    long_running: bool = False
    required: tuple[str, ...] = field(default_factory=tuple)


def ok(radio: str, action: str, **data: Any) -> dict[str, Any]:
    """Build the standard success envelope for a radio action."""
    return {"success": True, "radio": radio, "action": action, **data}


def require(args: dict[str, Any], spec: ActionSpec, radio: str, action: str) -> None:
    """Raise VALIDATION_INVALID_PARAMETER if any required arg is missing/empty."""
    missing = [k for k in spec.required if args.get(k) in (None, "")]
    if missing:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"{radio}/{action} requires: {', '.join(missing)}",
                context={"radio": radio, "action": action, "missing": missing},
                suggestions=[f"Pass {m} (in params or as device_id)" for m in missing],
            )
        )


async def ws_call(
    client: Any, ws_type: str, *, context: dict[str, Any] | None = None, **fields: Any
) -> Any:
    """Send a WebSocket command and return its ``result``; raise on failure.

    Mirrors the ``send_websocket_message`` usage in the ``ha_get_device``
    enrichers. Raises ToolError (SERVICE_CALL_FAILED) when HA reports the
    command failed, attaching the command type and any caller context.
    """
    message = {"type": ws_type, **{k: v for k, v in fields.items() if v is not None}}
    result = await client.send_websocket_message(message)
    if not result.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                result.get("error", f"WebSocket command '{ws_type}' failed"),
                context={"ws_type": ws_type, **(context or {})},
            )
        )
    return result.get("result")


async def resolve_entry_id(client: Any, domain: str) -> str | None:
    """Return the config entry_id for a single-instance integration ``domain``.

    Uses ``config_entries/get`` (underscore form; the slash form is rejected as
    "Unknown command"). Returns None when the integration is not configured.
    """
    entries = await ws_call(client, "config_entries/get", context={"domain": domain})
    for entry in entries or []:
        if entry.get("domain") == domain:
            return entry.get("entry_id")
    return None


def integration_not_found(radio: str, domain: str) -> dict[str, Any]:
    """Standard degraded payload when an integration/config-entry is absent."""
    return {
        "success": True,
        "radio": radio,
        "available": False,
        "warnings": [f"{domain} integration is not configured on this Home Assistant"],
    }


def confirm_required(radio: str, action: str) -> ToolError:
    """Build the ToolError raised when a destructive action lacks confirm=True."""
    return raise_tool_error(  # type: ignore[return-value]  # NoReturn
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"{radio}/{action} is destructive; pass confirm=True to proceed",
            context={"radio": radio, "action": action, "destructive": True},
            suggestions=["Re-run with confirm=True once you intend the change"],
        )
    )
