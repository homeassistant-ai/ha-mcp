"""Optional access to the running Core's request contracts, never local replicas."""

import logging
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import raise_tool_error

logger = logging.getLogger(__name__)


async def include_core_contract(
    client: Any, result: dict[str, Any], command: str
) -> None:
    """Attach optional discovery without discarding an otherwise successful read."""
    try:
        contract = dict(await core_contract(client, command))
    except ToolError:
        logger.warning("Optional Core schema discovery failed", exc_info=True)
        contract = {
            "status": "unavailable",
            "reason": "Core schema discovery failed; the read result is preserved.",
        }
    warnings = contract.pop("warnings", [])
    if contract.get("status") == "unavailable":
        warnings.append(contract["reason"])
    if warnings:
        result.setdefault("warnings", []).extend(warnings)
    result["core_contract"] = contract


async def core_contract(
    client: Any, command: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Describe or validate without dispatching; absence is explicitly unvalidated."""
    unavailable = {
        "status": "unavailable",
        "reason": "The installed component does not expose Core contract discovery/validation. Core still validates actual requests.",
    }
    try:
        caps = await get_component_caps(client, strict=True)
        if not component_supports(caps, "core_contract"):
            return unavailable
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        params: dict[str, Any] = {"command": command}
        if payload is not None:
            params["payload"] = payload
        response = await ws.send_command("ha_mcp_tools/core_contract", **params)
        result = response.get("result")
        if isinstance(result, dict) and result.get("status") in {
            "available",
            "validated",
            "unavailable",
        }:
            return result
        raise ValueError("Malformed Core contract response")
    except Exception as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
            return unavailable
        logger.warning("Core contract discovery failed", exc_info=True)
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Could not obtain Core contract: {exc}",
                context={"command": command},
            )
        )
        return {}  # unreachable


def command_payload(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Preserve every payload field while preventing command-envelope injection."""
    if {"id", "type"} & payload.keys():
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_FAILED,
                "id and type are reserved WebSocket transport fields",
            )
        )
    return {"type": command, **payload}


async def validate_energy_proposal(
    client: Any, config: dict[str, Any]
) -> dict[str, Any]:
    """Validate the whole submitted payload with the same schema Core will save."""
    command_payload("energy/save_prefs", config)
    result = await core_contract(client, "energy/save_prefs", config)
    if result.get("status") == "validated" and result.get("valid") is not True:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_FAILED,
                "Home Assistant Core rejected the proposed energy preferences",
                context={"proposal_validation": result},
            )
        )
    return result


def merge_core_options(
    params: dict[str, Any], options: dict[str, Any] | None
) -> dict[str, Any]:
    """Pass native options through while retaining the tool's bounded query envelope."""
    if not options:
        return params
    reserved = set(params) | {"id", "type"}
    if overlap := reserved.intersection(options):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Use the tool parameters for protected query fields",
                context={"protected_fields": sorted(overlap)},
            )
        )
    return {**params, **options}
