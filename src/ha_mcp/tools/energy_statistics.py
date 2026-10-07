"""Read the recorder statistics referenced by native Energy Dashboard preferences."""

from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from ..utils.config_hash import compute_config_hash
from .core_contract import core_contract
from .helpers import exception_to_structured_error, raise_tool_error
from .statistics_helpers import fetch_statistics_metadata, statistics_unit


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


def _compute_per_key_hashes(prefs: dict[str, Any]) -> dict[str, str]:
    """Hash every native preference slot without an allowlist or invented defaults."""
    return {key: compute_config_hash({key: value}) for key, value in prefs.items()}


def _is_no_prefs_error(error_msg: str) -> bool:
    """Return True if an error string from send_websocket_message indicates
    ``ERR_NOT_FOUND "No prefs"`` from HA Core's energy/get_prefs handler.

    HA Core wraps the error as ``f"Command failed: {message}"``; the
    underlying sentinel we key on is the literal ``"No prefs"`` message
    emitted by ``ws_get_prefs`` when ``manager.data is None``.
    """
    return error_msg.endswith("No prefs")


async def get_energy_prefs(client: Any) -> dict[str, Any]:
    """Read preferences, mapping Core's never-configured response to empty prefs."""
    try:
        result = await client.send_websocket_message({"type": "energy/get_prefs"})
        note = None
        if not result.get("success"):
            if not _is_no_prefs_error(str(result.get("error", ""))):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to get energy prefs: {result.get('error', 'Unknown error')}",
                        context={"mode": "get"},
                    )
                )
            contract = await core_contract(client, "energy/save_prefs")
            prefs = contract.get("default_preferences", {})
            note = "Energy Dashboard has never been configured."
            if "default_preferences" not in contract:
                note += " Core defaults are unavailable without the component; config is empty. Use the full config_hash for the initial save."
        else:
            prefs = result.get("result") or {}
        response = {
            "success": True,
            "mode": "get",
            "config": prefs,
            "config_hash": compute_config_hash(prefs),
            "config_hash_per_key": _compute_per_key_hashes(prefs),
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
