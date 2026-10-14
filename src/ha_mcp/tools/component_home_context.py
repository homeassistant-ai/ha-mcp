"""Home context documents through the component's ``home_context_*`` commands.

The documents live in Home Assistant's storage, owned by the ``ha_mcp_tools``
component, so there is no legacy path: without the ``home_context``
capability the tools report that the component has to be installed or updated.
"""

from __future__ import annotations

import logging
from typing import Any, NoReturn

from ..client.websocket_client import get_websocket_client
from ..errors import ErrorCode, create_error_response
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import exception_to_structured_error, raise_tool_error

logger = logging.getLogger(__name__)

WS_HOME_CONTEXT_GET = "ha_mcp_tools/home_context_get"
WS_HOME_CONTEXT_WRITE = "ha_mcp_tools/home_context_write"
WS_HOME_CONTEXT_MATCH = "ha_mcp_tools/home_context_match"
HOME_CONTEXT_CAPABILITY = "home_context"


def _raise_unsupported() -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.COMPONENT_NOT_INSTALLED,
            "Home context documents are stored by the HA-MCP custom component, "
            "and the component reachable from this server does not provide them.",
            suggestions=[
                "Install the HA-MCP integration through HACS, or update it: "
                + "HACS → Integrations → HA-MCP Custom Component → Update",
                "Restart Home Assistant after installing or updating",
            ],
        )
    )


async def _component_ws(client: Any) -> Any | None:
    if not getattr(client, "base_url", None) or not getattr(client, "token", None):
        return None
    caps = await get_component_caps(client)
    if not component_supports(caps, HOME_CONTEXT_CAPABILITY):
        return None
    return await get_websocket_client(
        url=client.base_url,
        token=client.token,
        verify_ssl=getattr(client, "verify_ssl", None),
    )


async def send(client: Any, command: str, **kwargs: Any) -> dict[str, Any]:
    """Send a home context command and return the component's result payload.

    A refused write or a missing revision comes back as ``success: False``
    with the component's error; the caller decides how to report it.
    """
    ws = await _component_ws(client)
    if ws is None:
        _raise_unsupported()
    try:
        raw = await ws.send_command(command, **kwargs)
    except Exception as exc:  # noqa: BLE001 - ends in a tool error
        if is_unknown_command(exc):
            invalidate_caps(client)
            _raise_unsupported()
        exception_to_structured_error(exc, context={"command": command})
    result = raw.get("result") if isinstance(raw, dict) else None
    if not isinstance(result, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"{command} returned a malformed reply",
                context={"command": command},
            )
        )
    return result


async def match_scopes(
    client: Any, targets: dict[str, list[str]]
) -> list[dict[str, Any]] | None:
    """The stored scopes covering ``targets``; ``None`` when they cannot be read.

    Used for hints on other tools' results, so it never raises.
    """
    try:
        ws = await _component_ws(client)
        if ws is None:
            return None
        raw = await ws.send_command(WS_HOME_CONTEXT_MATCH, **targets)
    except Exception as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        logger.debug("home context match failed: %r", exc, exc_info=True)
        return None
    result = raw.get("result") if isinstance(raw, dict) else None
    scopes = result.get("scopes") if isinstance(result, dict) else None
    return scopes if isinstance(scopes, list) else None
