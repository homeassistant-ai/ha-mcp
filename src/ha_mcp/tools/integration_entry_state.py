"""The enable/disable and log-level modes of ``ha_set_integration``.

Neither runs a config flow: each sends one mutating WebSocket command after
the own-entry check (enable/disable and log level by entry look the entry up
first), and both refuse ha-mcp's own ``ha_mcp_tools`` entries.
"""

from typing import Any

from ..errors import ErrorCode, create_error_response
from .config_entry_flow import refuse_component_entry
from .helpers import raise_tool_error, ws_failure_code


async def set_log_level(
    client: Any,
    entry_id: str | None,
    domain: str | None,
    log_level: str,
    *,
    other_modes: bool,
) -> dict[str, Any]:
    """Set an integration's log level through ``logger/integration_log_level``."""
    if other_modes or (domain is None) == (entry_id is None):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "log_level takes exactly one of 'domain' or 'entry_id' and "
                "cannot be combined with config, enabled or reconfigure",
                suggestions=[
                    "Example: ha_set_integration(domain='zha', log_level='DEBUG')",
                ],
                context={"entry_id": entry_id, "domain": domain},
            )
        )
    if entry_id is not None:
        entry = await client.get_config_entry(entry_id)
        domain = entry.get("domain")
    refuse_component_entry(domain, action="set the log level of", entry_id=entry_id)
    result = await client.send_websocket_message(
        {
            "type": "logger/integration_log_level",
            "integration": domain,
            # NOTSET is what the UI's Disable debug logging sends.
            "level": "NOTSET" if log_level == "DEFAULT" else log_level,
            "persistence": "once",
        }
    )
    if not result.get("success"):
        raise_tool_error(
            create_error_response(
                ws_failure_code(result),
                f"Failed to set the log level for '{domain}': "
                f"{result.get('error') or 'unknown error'}",
                context={"domain": domain, "entry_id": entry_id},
            )
        )
    return {
        "success": True,
        "action": "set_log_level",
        "domain": domain,
        "log_level": log_level,
        "note": (
            "Override cleared: the integration now logs at the inherited "
            "default level; a level set for it in configuration.yaml returns "
            "after the next restart."
            if log_level == "DEFAULT"
            else "Applies now and through the next Home Assistant restart."
        ),
    }


async def set_entry_enabled(
    client: Any, entry_id: str, enabled: bool
) -> dict[str, Any]:
    """Enable or disable a config entry via ``config_entries/disable``."""
    entry = await client.get_config_entry(entry_id)
    refuse_component_entry(
        entry.get("domain"), action="enable or disable", entry_id=entry_id
    )
    message = {
        "type": "config_entries/disable",
        "entry_id": entry_id,
        "disabled_by": None if enabled else "user",
    }

    result = await client.send_websocket_message(message)

    if not result.get("success"):
        error_msg = result.get("error", {})
        if isinstance(error_msg, dict):
            error_msg = error_msg.get("message", str(error_msg))
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to {'enable' if enabled else 'disable'} integration: {error_msg}",
                context={"entry_id": entry_id},
            )
        )

    require_restart = (result.get("result") or {}).get("require_restart", False)

    if require_restart:
        note = "Home Assistant restart required for changes to take effect."
    else:
        note = (
            "Integration has been loaded."
            if enabled
            else "Integration has been unloaded."
        )

    return {
        "success": True,
        "message": f"Integration {'enabled' if enabled else 'disabled'} successfully",
        "entry_id": entry_id,
        "require_restart": require_restart,
        "note": note,
    }
