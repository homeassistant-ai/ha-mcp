"""Restart request behind ha_restart (config check, then homeassistant.restart)."""

import asyncio
import logging
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from .._version import is_embedded
from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, raise_tool_error

logger = logging.getLogger(__name__)

# Embedded mode: delay before the restart fires, so ha_restart's reply flushes
# to the client first. Same value as tools_dev._SELF_ACTION_FLUSH_DELAY_S.
_EMBEDDED_RESTART_DELAY_S = 1.0

# Strong references to in-flight deferred restarts (the event loop holds tasks
# only weakly).
_RESTART_TASKS: set[asyncio.Task[None]] = set()


def _schedule_embedded_restart(client: Any) -> None:
    """Fire ``homeassistant.restart`` after this tool's reply has flushed."""

    async def _restart() -> None:
        await asyncio.sleep(_EMBEDDED_RESTART_DELAY_S)
        try:
            await client.call_service("homeassistant", "restart", {})
        except Exception:
            # No caller left to answer; WARNING is the level HA surfaces.
            logger.warning("Deferred Home Assistant restart failed", exc_info=True)

    task = asyncio.get_running_loop().create_task(_restart())
    _RESTART_TASKS.add(task)
    task.add_done_callback(_RESTART_TASKS.discard)


async def restart_home_assistant(client: Any) -> dict[str, Any]:
    """Validate the config, then restart Home Assistant."""
    restart_initiated = False
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
                    context={"config_errors": errors},
                )
            )

        if is_embedded():
            # This server runs inside HA and stops with it: on HAOS the
            # restart call never returns before Core is killed, so reply
            # first and restart after (#2691).
            _schedule_embedded_restart(client)
            return {
                "success": True,
                "message": (
                    "Home Assistant restart scheduled. This MCP server runs "
                    "inside Home Assistant and goes down with it; calls fail "
                    "until it is back (1-5 minutes)."
                ),
                "warnings": [
                    "Calls may fail while this server starts. "
                    "Try again after a few seconds."
                ],
            }

        # Call the restart service - mark as initiated before the call
        # as the connection may be closed before we get a response
        restart_initiated = True
        await client.call_service("homeassistant", "restart", {})

        return {
            "success": True,
            "message": (
                "Home Assistant restart initiated. "
                "The system will be unavailable for 1-5 minutes."
            ),
            "warnings": [
                "Connection will be lost during restart. "
                "Wait for Home Assistant to become available again."
            ],
        }

    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        error_msg = str(e)
        # Connection errors after restart initiated are expected
        # (HA closes connections during restart)
        if restart_initiated and any(
            pattern in error_msg.lower()
            for pattern in (
                "connect",
                "closed",
                "504",
                "502",
                "503",
                "gateway",
                "unavailable",
            )
        ):
            return {
                "success": True,
                "message": (
                    "Home Assistant restart initiated. "
                    "Connection was closed as expected during restart."
                ),
                "warnings": ["Wait 1-5 minutes for Home Assistant to restart."],
            }

        exception_to_structured_error(e)
        return None  # unreachable: exception_to_structured_error always raises
