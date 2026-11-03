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
    params: dict[str, Any] = {"command": command}
    if payload is not None:
        params["payload"] = payload
    return await _contract_bridge(
        client, "core_contract", params, "Core contract discovery/validation"
    )


async def integration_commands(client: Any, domain: str) -> dict[str, Any]:
    """List ``domain``'s own WebSocket commands with their request schemas."""
    return await _contract_bridge(
        client,
        "integration_commands",
        {"domain": domain},
        "the listing of an integration's own WebSocket commands",
    )


async def _contract_bridge(
    client: Any, capability: str, params: dict[str, Any], feature: str
) -> dict[str, Any]:
    """Ask the component's ``capability`` bridge; ``feature`` names it in
    the reason and error texts."""
    unavailable = {
        "status": "unavailable",
        "reason": f"The installed component does not expose {feature}. "
        "Core still validates actual requests.",
    }
    try:
        caps = await get_component_caps(client, strict=True)
        if not component_supports(caps, capability):
            return unavailable
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
        response = await ws.send_command(f"ha_mcp_tools/{capability}", **params)
        result = response.get("result")
        if isinstance(result, dict) and result.get("status") in (
            "available",
            "validated",
            "unavailable",
        ):
            return result
        raise ValueError(f"malformed ha_mcp_tools/{capability} response")
    except Exception as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
            return unavailable
        logger.warning("%s failed", feature, exc_info=True)
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Could not obtain {feature}: {exc}",
                context={k: v for k, v in params.items() if k != "payload"},
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
