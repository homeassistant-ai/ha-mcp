"""Restart request behind ha_restart (pre-checks, then homeassistant.restart)."""

import asyncio
import logging
from typing import Any, NoReturn

import httpx

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from .._version import is_embedded
from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, raise_tool_error

logger = logging.getLogger(__name__)

# Embedded mode: delay before the restart fires, so ha_restart's reply flushes
# to the client first. Same value as tools_dev._SELF_ACTION_FLUSH_DELAY_S.
_EMBEDDED_RESTART_DELAY_S = 1.0

# Strong references to in-flight deferred restarts (the event loop holds tasks
# only weakly).
_RESTART_TASKS: set[asyncio.Task[None]] = set()


def _schedule_embedded_restart(client: Any, data: dict[str, Any]) -> None:
    """Fire ``homeassistant.restart`` after this tool's reply has flushed."""

    async def _restart() -> None:
        await asyncio.sleep(_EMBEDDED_RESTART_DELAY_S)
        try:
            await client.call_service("homeassistant", "restart", data)
        except Exception as exc:
            # No caller remains. A lost acknowledgement does not prove either
            # restart success or rejection, and must not trigger a retry.
            if isinstance(exc.__cause__, httpx.ReadError | httpx.RemoteProtocolError):
                logger.warning(
                    "Deferred Home Assistant restart outcome is unknown; "
                    "the connection closed before acknowledgement"
                )
            else:
                logger.warning("Deferred Home Assistant restart failed", exc_info=True)

    task = asyncio.get_running_loop().create_task(_restart())
    _RESTART_TASKS.add(task)
    task.add_done_callback(_RESTART_TASKS.discard)


async def _core_info(client: Any, msg_type: str) -> dict[str, Any] | None:
    """Core's reply to an info command, or None when Core does not answer it."""
    try:
        info = await client.send_websocket_message({"type": msg_type})
    except HomeAssistantConnectionError:
        return None
    if not info.get("success"):
        return None
    result = info.get("result")
    return result if isinstance(result, dict) else None


async def _embedded_precheck(client: Any, *, safe_mode: bool) -> list[str]:
    """Refuse what Core would refuse; return the warnings for the reply."""
    warnings = [
        "Calls may fail while this server starts. Try again after a few seconds."
    ]
    if safe_mode:
        warnings.append(
            "Safe mode loads no custom integrations, and this server is "
            "one: it stays down until Home Assistant is next "
            "restarted normally."
        )
    # A missing field (older Core, renamed key) means the check was not made,
    # so it warns like an unanswered command instead of passing silently.
    recorder = await _core_info(client, "recorder/info")
    if recorder is None or not isinstance(recorder.get("migration_in_progress"), bool):
        warnings.append(
            "Could not read the recorder status to check for a database "
            "upgrade; Home Assistant refuses to restart during one."
        )
    elif recorder.get("migration_in_progress"):
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_LOCKED,
                "A database upgrade is in progress - restart refused",
                details=(
                    "Home Assistant cannot restart while the recorder "
                    "is upgrading its database."
                ),
                context={"recorder": recorder, "execution_status": "not_started"},
                suggestions=[
                    "Wait for the upgrade to finish, then call "
                    "ha_restart(confirm=True) again"
                ],
            )
        )
    backup = await _core_info(client, "backup/info")
    state = backup.get("state") if backup else None
    if not isinstance(state, str):
        warnings.append(
            "Could not read the backup status to check for a running "
            "backup; Home Assistant cannot restart during one."
        )
    elif state != "idle":
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_LOCKED,
                f"Backup manager is busy ({state}) - restart refused",
                details=(
                    "Home Assistant cannot restart while the backup manager is "
                    "busy creating, receiving or restoring one."
                ),
                context={"backup_state": state, "execution_status": "not_started"},
                suggestions=[
                    "Wait for the backup to finish, then call "
                    "ha_restart(confirm=True) again"
                ],
            )
        )
    return warnings


async def restart_home_assistant(
    client: Any, *, confirm: bool, safe_mode: bool = False
) -> dict[str, Any]:
    """Validate the config, then restart Home Assistant."""
    if not confirm:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Restart not confirmed",
                context={"execution_status": "not_started"},
                details=(
                    "You must set confirm=True to restart Home Assistant. "
                    "This is a safety measure to prevent accidental restarts."
                ),
                suggestions=[
                    'Pre-check config via ha_get_system_health(include="config_check")',
                    "Call ha_restart(confirm=True) to proceed with restart",
                    "Consider using ha_reload_core() for config-only changes",
                ],
            )
        )

    restart_initiated = False
    data = {"safe_mode": safe_mode}
    try:
        # Check configuration first as a safety measure
        config_result = await client.check_config()
        if config_result.get("result") != "valid":
            errors = config_result.get("errors") or []
            raise_tool_error(
                create_error_response(
                    ErrorCode.CONFIG_INVALID,
                    "Configuration is invalid - restart aborted",
                    details=(
                        "Home Assistant configuration has errors. "
                        "Fix the errors before restarting."
                    ),
                    context={
                        "config_errors": errors,
                        "execution_status": "not_started",
                    },
                )
            )

        if is_embedded():
            # This server runs inside HA and stops with it: on HAOS the
            # restart call never returns before Core is killed, so reply
            # first and restart after (#2691). Core's refusal during a
            # database upgrade would then reach nobody, so read Core's own
            # flag first (#2632). Same for a running backup: Supervisor refuses
            # to stop Core during one, and the frontend's restart dialog holds
            # the restart while the backup manager is not idle.
            warnings = await _embedded_precheck(client, safe_mode=safe_mode)

            _schedule_embedded_restart(client, data)
            return {
                "success": True,
                "requested": True,
                "scheduled": True,
                "message": (
                    "Home Assistant restart scheduled. This MCP server runs "
                    "inside Home Assistant and goes down with it; calls fail "
                    "until it is back (1-5 minutes)."
                ),
                "warnings": warnings,
            }

        # Call the restart service - mark as initiated before the call
        # as the connection may be closed before we get a response
        restart_initiated = True
        await client.call_service("homeassistant", "restart", data)

        return {
            "success": True,
            "requested": True,
            "message": (
                "Home Assistant accepted the restart request. "
                "Completion is not confirmed; the system may be unavailable for 1-5 minutes."
            ),
            "warnings": [
                "Connection will be lost during restart. "
                "Wait for Home Assistant to become available again."
            ],
        }

    except ToolError:
        raise
    except Exception as error:
        _raise_restart_error(error, dispatched=restart_initiated)
        raise


def _raise_restart_error(error: Exception, *, dispatched: bool) -> NoReturn:
    rejected = isinstance(
        error, HomeAssistantCommandNotSent | HomeAssistantAuthError
    ) or (
        isinstance(error, HomeAssistantAPIError)
        and error.status_code in {400, 401, 403, 404, 422}
    )
    if not dispatched or rejected:
        payload = exception_to_structured_error(error, raise_error=False)
        payload["execution_status"] = "not_started"
    else:
        payload = create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Home Assistant restart was requested but its outcome is unknown.",
            context={"operation": "restart", "write_outcome_unknown": True},
            suggestions=[
                "Check Home Assistant's availability and uptime before retrying; the restart may already have happened.",
            ],
        )
        payload["execution_status"] = "outcome_unknown"
    raise_tool_error(payload)
