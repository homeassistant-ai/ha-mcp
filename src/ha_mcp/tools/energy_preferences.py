"""Read native Energy preferences, hash saved state and enrich statistic references."""

import logging
from typing import Any, Literal

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from ..utils.config_hash import compute_config_hash
from .core_contract import core_contract, energy_suggestions
from .helpers import (
    exception_to_structured_error,
    raise_core_refusal,
    raise_tool_error,
)
from .statistics_helpers import fetch_statistics_metadata, statistics_unit

logger = logging.getLogger(__name__)


def _statistic_ids(value: Any) -> set[str]:
    """Collect statistic references across Core's source and device structures."""
    found: set[str] = set()
    if isinstance(value, list):
        for item in value:
            found.update(_statistic_ids(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if (
                (key.startswith("stat_") or key == "included_in_stat")
                and isinstance(item, str)
                and item
            ):
                found.add(item)
            elif isinstance(item, (dict, list)):
                found.update(_statistic_ids(item))
    return found


async def include_energy_statistics(
    client: Any, result: dict[str, Any]
) -> dict[str, Any]:
    """Enrich the read response without including metadata in config or its hash."""
    ids = sorted(_statistic_ids(result["config"]))
    metadata, failure = await fetch_statistics_metadata(client, ids)
    result["statistics_metadata"] = [
        {
            **(metadata.get(statistic_id) or {"statistic_id": statistic_id}),
            **statistics_unit(metadata.get(statistic_id), failure),
        }
        for statistic_id in ids
    ]
    warnings = [
        f"{row['statistic_id']}: {row['unit_reason']}"
        for row in result["statistics_metadata"]
        if row["unit_source"] == "unknown"
    ]
    if warnings:
        result.setdefault("warnings", []).extend(warnings)
    return result


async def include_energy_suggestions(
    client: Any, result: dict[str, Any]
) -> dict[str, Any]:
    """Attach the meters Settings > Energy suggests; a failed read keeps the result."""
    try:
        suggestions = dict(await energy_suggestions(client))
    except ToolError:
        logger.warning("Energy meter suggestions failed", exc_info=True)
        suggestions = {
            "status": "unavailable",
            "reason": "The HA-MCP component could not compute the energy meter "
            "suggestions; update it, then read them again.",
        }
    if suggestions["status"] == "unavailable":
        result.setdefault("warnings", []).append(suggestions["reason"])
    result["energy_suggestions"] = suggestions
    return result


async def change_energy_suggestions(
    client: Any, action: Literal["dismiss", "restore"], statistic_ids: list[str]
) -> dict[str, Any]:
    """Dismiss or restore suggested meters for the token's Home Assistant user,
    as Settings > Energy does."""
    mode = f"{action}_suggestions"
    suggestions = await energy_suggestions(client, {action: statistic_ids})
    if suggestions["status"] != "available":
        raise_tool_error(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                suggestions["reason"],
                context={"mode": mode},
            )
        )
    if unknown := suggestions.get("unknown"):
        listed = "suggested" if action == "dismiss" else "dismissed"
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Not {listed} by Settings > Energy, nothing changed: {', '.join(unknown)}",
                context={"mode": mode, "unknown": unknown},
                suggestions=[
                    "Take statisticId values from ha_manage_energy_prefs(mode='get', include_suggestions=True)",
                ],
            )
        )
    return {
        "success": True,
        "mode": f"{action}_suggestions",
        "energy_suggestions": suggestions,
    }


def compute_per_key_hashes(prefs: dict[str, Any]) -> dict[str, str]:
    """Hash every native preference slot without an allowlist or invented defaults."""
    return {key: compute_config_hash({key: value}) for key, value in prefs.items()}


async def get_energy_prefs(client: Any) -> dict[str, Any]:
    """Read preferences, mapping Core's never-configured response to empty prefs."""
    try:
        result = await client.send_websocket_message({"type": "energy/get_prefs"})
        note = None
        if not result.get("success"):
            # energy/get_prefs answers not_found only while nothing was ever saved.
            if result.get("error_code") != "not_found":
                raise_core_refusal(
                    "Failed to get energy prefs", result, context={"mode": "get"}
                )
            try:
                contract = await core_contract(client, "energy/save_prefs")
            except ToolError:
                logger.warning("Energy defaults discovery failed", exc_info=True)
                contract = {}
            prefs = contract.get("default_preferences", {})
            note = "Energy Dashboard has never been configured."
            if "default_preferences" not in contract:
                note += " Core defaults are unavailable; config is empty. Use the full config_hash for the initial save."
        else:
            prefs = result.get("result") or {}
        response = {
            "success": True,
            "mode": "get",
            "config": prefs,
            "config_hash": compute_config_hash(prefs),
            "config_hash_per_key": compute_per_key_hashes(prefs),
        }
        if note:
            response["note"] = note
        return response
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        exception_to_structured_error(
            e,
            context={"mode": "get"},
            suggestions=[
                "Check Home Assistant connection",
                "Verify WebSocket connection is active",
            ],
        )
        return None  # unreachable: exception_to_structured_error always raises
