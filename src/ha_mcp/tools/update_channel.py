"""Supervisor's release channel, read and set as the Updates page's Join beta /
Leave beta does: through Core's ``supervisor/api`` relay."""

import json
from typing import Any

from .._vendor.fastmcp.exceptions import ToolError
from ..errors import ErrorCode, create_error_response
from .component_api import UNKNOWN_COMMAND_CODE
from .helpers import raise_tool_error

_INFO = "/supervisor/info"


def _core_code(error: ToolError) -> object:
    """The ``ha_error_code`` Core answered the relayed call with, if any."""
    try:
        payload = json.loads(str(error))
    except ValueError:
        return None
    return payload.get("ha_error_code") if isinstance(payload, dict) else None


async def _supervisor(
    client: Any, endpoint: str, method: str = "GET", data: dict[str, Any] | None = None
) -> dict[str, Any]:
    from .tools_addons import _supervisor_api_call

    try:
        reply = await _supervisor_api_call(client, endpoint, method, data)
    except ToolError as e:
        # Core registers supervisor/api only where a Supervisor runs.
        if _core_code(e) != UNKNOWN_COMMAND_CODE:
            raise
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                "Supervisor's release channel needs a Supervisor installation "
                "(Home Assistant OS or Supervised); this Home Assistant has none.",
                context={"ha_error_code": UNKNOWN_COMMAND_CODE},
            )
        )
    result: dict[str, Any] = reply["result"]
    return result


async def supervisor_channel(client: Any) -> str | None:
    """The channel Supervisor reports; None when no Supervisor answers."""
    try:
        info = await _supervisor(client, _INFO)
    except ToolError:
        return None
    channel = info.get("channel")
    return channel if isinstance(channel, str) else None


async def set_supervisor_channel(client: Any, channel: str | None) -> dict[str, Any]:
    """Write ``channel`` as given (Supervisor validates it), reload, and return
    the channel Supervisor reports now and the one it reported before."""
    if channel is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "'set_channel' requires channel.",
            )
        )
    before = await _supervisor(client, _INFO)
    await _supervisor(client, "/supervisor/options", "POST", {"channel": channel})
    await _supervisor(client, "/supervisor/reload", "POST")
    after = await _supervisor(client, _INFO)
    return {
        "success": True,
        "channel": after.get("channel"),
        "previous_channel": before.get("channel"),
    }
