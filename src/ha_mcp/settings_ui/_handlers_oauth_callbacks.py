"""OAuth callback allowlist route handlers for the settings UI (#2427).

The in-process server's none-mode ``/authorize`` redirects only to callbacks
on an allowlist the ha_mcp_tools component stores on its server entry. These
handlers read and replace that list through the component's
``ha_mcp_tools/oauth_callbacks`` commands, so the panel and the entry's
Configure screen edit the same setting. Only the embedded server has the list;
every other deployment reports it unavailable and the page hides the section.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from .._version import is_embedded
from ..errors import ErrorCode, create_error_response

if TYPE_CHECKING:
    from ..server import HomeAssistantSmartMCPServer

logger = logging.getLogger(__name__)

WS_OAUTH_CALLBACKS = "ha_mcp_tools/oauth_callbacks"
WS_OAUTH_CALLBACKS_UPDATE = "ha_mcp_tools/oauth_callbacks_update"
CAPABILITY = "oauth_callbacks"
_MAX_CALLBACKS = 50


class _Unavailable(Exception):
    """The list cannot be edited here; the message says why."""


async def _component_command(
    server: HomeAssistantSmartMCPServer, command: str, **fields: Any
) -> dict[str, Any]:
    """Run one allowlist command on the component and return its result."""
    from ..client.websocket_client import get_websocket_client
    from ..tools.component_api import component_supports, get_component_caps

    client = server.client
    caps = await get_component_caps(client)
    if not component_supports(caps, CAPABILITY):
        raise _Unavailable(
            "This HA-MCP component cannot edit the callback list. Update the "
            "HA-MCP integration in HACS."
        )
    ws = await get_websocket_client(
        url=client.base_url,
        token=client.token,
        verify_ssl=getattr(client, "verify_ssl", None),
    )
    raw = await ws.send_command(command, **fields)
    result = raw.get("result") if isinstance(raw, dict) else None
    if not isinstance(result, dict):
        raise _Unavailable("HA-MCP component returned an unexpected response.")
    return result


def _unavailable(reason: str | None) -> JSONResponse:
    return JSONResponse({"success": True, "available": False, "reason": reason})


async def _get_oauth_callbacks(
    server: HomeAssistantSmartMCPServer | None, _: Request
) -> JSONResponse:
    """The allowlist in force; always 200, with ``available`` False when absent."""
    if server is None or not is_embedded():
        return _unavailable(None)
    try:
        result = await _component_command(server, WS_OAUTH_CALLBACKS)
    except _Unavailable as exc:
        return _unavailable(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning("oauth-callbacks GET could not reach ha_mcp_tools: %s", exc)
        return _unavailable(f"Could not reach the HA-MCP component: {exc}")
    return JSONResponse({"success": True, "available": True, **result})


def _parse_change(body: Any) -> dict[str, Any] | None:
    if not isinstance(body, dict):
        return None
    if body.get("reset") is True:
        return {"reset": True}
    allowlist = body.get("allowlist")
    if (
        isinstance(allowlist, list)
        and len(allowlist) <= _MAX_CALLBACKS
        and all(isinstance(item, str) for item in allowlist)
    ):
        return {"allowlist": allowlist}
    return None


async def _save_oauth_callbacks(
    server: HomeAssistantSmartMCPServer | None, request: Request
) -> JSONResponse:
    """Replace the allowlist (``{"allowlist": [...]}``) or restore the default
    (``{"reset": true}``). The change applies to the next sign-in."""
    if server is None or not is_embedded():
        return JSONResponse(
            create_error_response(
                ErrorCode.CONFIG_VALIDATION_FAILED,
                "The OAuth callback list belongs to the server built into the "
                "HA-MCP integration.",
            ),
            status_code=409,
        )
    try:
        change = _parse_change(await request.json())
    except (ValueError, TypeError):
        change = None
    if change is None:
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Send 'allowlist' as a list of at most {_MAX_CALLBACKS} URLs, "
                "or 'reset': true.",
            ),
            status_code=400,
        )
    try:
        result = await _component_command(server, WS_OAUTH_CALLBACKS_UPDATE, **change)
    except _Unavailable as exc:
        return JSONResponse(
            create_error_response(ErrorCode.SERVICE_CALL_FAILED, str(exc)),
            status_code=409,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("oauth-callbacks POST could not reach ha_mcp_tools: %s", exc)
        return JSONResponse(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Could not reach the HA-MCP component: {exc}",
            ),
            status_code=502,
        )
    if not result.get("saved"):
        return JSONResponse(
            {
                **create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Nothing was saved. Each callback must be an https:// URL, "
                    "or an http:// URL on 127.0.0.1, [::1] or localhost, without "
                    "a #fragment.",
                ),
                "invalid": result.get("invalid", []),
            },
            status_code=400,
        )
    return JSONResponse({"success": True, **result})


def build_oauth_callback_handlers(
    server: HomeAssistantSmartMCPServer | None,
) -> dict[str, Any]:
    """Construct the OAuth callback allowlist route handlers."""

    async def get_oauth_callbacks(request: Request) -> JSONResponse:
        return await _get_oauth_callbacks(server, request)

    async def save_oauth_callbacks(request: Request) -> JSONResponse:
        return await _save_oauth_callbacks(server, request)

    return {
        "get_oauth_callbacks": get_oauth_callbacks,
        "save_oauth_callbacks": save_oauth_callbacks,
    }
