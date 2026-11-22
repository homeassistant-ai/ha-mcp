"""Home Assistant's backup-before-update settings for ``ha_manage_backup``.

The Backups settings page reads and writes them with Core's
``hassio/update/config/info`` and ``hassio/update/config/update``, which Core
registers only where its hassio integration runs (a Supervisor installation).
Core answers them only to an administrator, and Supervisor's proxy refuses
every ``hassio/`` command an app sends, so ha-mcp running as the app can
neither read nor change them; both refusals carry the code ``unauthorized``.
"""

from typing import Any, NoReturn

from ..client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ..errors import ErrorCode, create_error_response
from .component_api import UNKNOWN_COMMAND_CODE
from .core_contract import command_payload
from .helpers import core_error_message, raise_core_refusal, raise_tool_error

_INFO = "hassio/update/config/info"
_UPDATE = "hassio/update/config/update"
_UNAUTHORIZED = "unauthorized"
_EXPECTED = frozenset({UNKNOWN_COMMAND_CODE, _UNAUTHORIZED})
_WHO_MAY = (
    "Home Assistant answers them only to an administrator, and not to ha-mcp "
    "running as the Home Assistant app (add-on), whose hassio/ commands "
    "Supervisor blocks"
)


async def read_update_backup_settings(
    client: HomeAssistantClient,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Core's ``info`` answer; None without a Supervisor, and None with a
    warning when the read failed."""
    try:
        reply = await client.send_websocket_message(
            {"type": _INFO}, expected_error_codes=_EXPECTED
        )
    except HomeAssistantConnectionError as err:
        reason = str(err)
    else:
        if reply.get("success"):
            return reply.get("result"), []
        if reply.get("error_code") == UNKNOWN_COMMAND_CODE:
            return None, []
        reason = core_error_message(reply.get("error", ""))
        if reply.get("error_code") == _UNAUTHORIZED:
            reason = f"{reason}: {_WHO_MAY}"
    return None, [
        f"Home Assistant's backup-before-update settings could not be read ({reason})"
    ]


async def add_update_backup_settings(
    client: HomeAssistantClient, result: dict[str, Any]
) -> dict[str, Any]:
    """``result`` with Core's settings as ``update_backup_settings``, where
    Home Assistant has them."""
    settings, warnings = await read_update_backup_settings(client)
    if settings is not None:
        result["update_backup_settings"] = settings
    if warnings:
        result["warnings"] = [*result.get("warnings", []), *warnings]
    return result


def _raise_refusal(reply: dict[str, Any], settings: dict[str, Any]) -> NoReturn:
    code = reply.get("error_code")
    if code == UNKNOWN_COMMAND_CODE:
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                "Home Assistant's backup-before-update settings need a "
                "Supervisor installation (Home Assistant OS or Supervised); "
                "this Home Assistant has none.",
                context={"ha_error_code": code},
            )
        )
    if code == _UNAUTHORIZED:
        raise_tool_error(
            create_error_response(
                ErrorCode.AUTH_INSUFFICIENT_PERMISSIONS,
                "Home Assistant refused the backup-before-update settings "
                f"({core_error_message(reply.get('error', ''))}): {_WHO_MAY}.",
                context={"ha_error_code": code},
                suggestions=[
                    "Change them in Home Assistant's Backups settings, or through "
                    "ha-mcp running as the embedded server or standalone with an "
                    "administrator's token"
                ],
            )
        )
    raise_core_refusal(
        "Home Assistant refused the backup-before-update settings",
        reply,
        context={"update_backup_settings": settings},
    )


async def configure_update_backup_settings(
    client: HomeAssistantClient, settings: dict[str, Any] | None
) -> dict[str, Any]:
    """Send ``settings`` to Core as given and return the settings Core holds then."""
    if not settings:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "scope='snapshot', action='configure' requires "
                "update_backup_settings with at least one setting",
                context={"missing_param": "update_backup_settings"},
            )
        )
    try:
        reply = await client.send_websocket_message(
            command_payload(_UPDATE, settings), expected_error_codes=_EXPECTED
        )
    except HomeAssistantConnectionError as err:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                f"Home Assistant did not answer the settings change ({err}); "
                "it may have been applied",
                suggestions=[
                    "Read the settings with ha_manage_backup(scope='snapshot', "
                    "action='list') before retrying"
                ],
            )
        )
    if not reply.get("success"):
        _raise_refusal(reply, settings)
    return await add_update_backup_settings(client, {"success": True})
