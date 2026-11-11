"""Filesystem custom-paths route handlers for the settings UI (issue #1567).

Factory returning the ``get_fs_custom_paths`` / ``save_fs_custom_paths``
handlers, which read/replace the user-configured extra filesystem
directories via the ha_mcp_tools component. The handlers are module-level
functions taking ``server`` explicitly (None in the stdio sidecar, which
builds a transient client instead); ``build_fs_handlers`` binds ``server``
into thin request-only wrappers for the route table.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ..errors import ErrorCode, create_error_response

if TYPE_CHECKING:
    from ..server import HomeAssistantSmartMCPServer

logger = logging.getLogger(__name__)


def _component_error_reason(exc: Exception) -> str | None:
    """Return a readable reason for a component call failure, else None.

    ``raise_tool_error`` serializes the whole error envelope into the
    exception string, so rendering ``str(exc)`` verbatim shows the settings
    UI a raw JSON blob behind a misleading "could not reach" prefix — the
    hard-to-read surface from #1996. The tools imports are lazy, like the
    others in this module.

    Home Assistant answered when it refuses the token, non-admin or rejected,
    so that is classified like in the tools and not reported as unreachable,
    also when it arrives behind the connection error of a refused WebSocket
    retry. OAuth mode without a server token is the exception: the settings
    page sends no request token there, so the proxy client raises the auth
    error without asking Home Assistant, and the remedy the tools suggest does
    not apply.
    """
    from ..client.rest_client import (
        HomeAssistantAuthError,
        HomeAssistantConnectionError,
    )
    from ..config import OAUTH_MODE_TOKEN, get_global_settings
    from ..tools.helpers import (
        exception_to_structured_error,
        extract_structured_error_reason,
        tool_error_from_response,
    )

    cause = exc.__cause__ if isinstance(exc, HomeAssistantConnectionError) else exc
    if (
        isinstance(cause, HomeAssistantAuthError)
        and get_global_settings().homeassistant_token != OAUTH_MODE_TOKEN
    ):
        exc = tool_error_from_response(
            exception_to_structured_error(cause, raise_error=False)
        )
    return extract_structured_error_reason(exc)


async def _fs_custom_paths_call(
    server: HomeAssistantSmartMCPServer | None, service: str, data: dict[str, Any]
) -> Any:
    """Invoke a ha_mcp_tools component service for the custom-paths editor,
    in any deployment mode (issue #1567).

    Uses the live server's HA client in HTTP/add-on modes; in the stdio
    sidecar (``server is None``) builds a transient ``HomeAssistantClient``
    from the HA URL/token the sidecar inherits, and closes it afterward.
    The caller wraps this in try/except so an unreachable HA / missing
    token (e.g. OAuth mode, where ``server.client`` has no request-scoped
    token) degrades to an "unavailable" envelope rather than a 500.
    """
    from ..tools.tools_filesystem import call_mcp_tools_service

    own_client = None
    try:
        if server is not None:
            client = server.client
        else:
            from ..client.rest_client import HomeAssistantClient

            client = own_client = HomeAssistantClient()
        return await call_mcp_tools_service(client, service, data)
    finally:
        if own_client is not None and hasattr(own_client, "close"):
            with contextlib.suppress(Exception):
                await own_client.close()


async def _get_fs_custom_paths(
    server: HomeAssistantSmartMCPServer | None, _: Request
) -> JSONResponse:
    """Return the user-configured extra filesystem directories from the
    ha_mcp_tools component, plus the non-overridable deny floor for the UI
    blurb (issue #1567).

    Always 200s with an ``available`` flag: when filesystem tools are off,
    the component is missing/too old, or HA is unreachable or refuses the
    token, ``available``
    is False with a human-readable ``reason`` so the UI can show a disabled
    section instead of an error.
    """
    from ..tools.tools_filesystem import is_filesystem_tools_enabled
    from ..tools.util_helpers import unwrap_service_response

    def _unavailable(reason: str) -> JSONResponse:
        return JSONResponse(
            {
                "success": True,
                "available": False,
                "reason": reason,
                "paths": [],
                "deny_floor": [],
            }
        )

    if not is_filesystem_tools_enabled():
        return _unavailable(
            "Filesystem tools are disabled. Enable them (beta) to "
            "configure custom directories."
        )
    try:
        result = await _fs_custom_paths_call(server, "get_allowed_paths", {})
    except Exception as exc:  # noqa: BLE001
        logger.warning("fs-custom-paths GET failed calling ha_mcp_tools: %s", exc)
        return _unavailable(
            _component_error_reason(exc)
            or f"Could not reach the ha_mcp_tools component: {exc}"
        )

    data = unwrap_service_response(result) if isinstance(result, dict) else {}
    if not isinstance(data, dict) or not data.get("success", False):
        reason = (
            data.get("error") if isinstance(data, dict) else None
        ) or "ha_mcp_tools returned an unexpected response."
        return _unavailable(str(reason))
    return JSONResponse(
        {
            "success": True,
            "available": True,
            "paths": data.get("paths", []),
            "deny_floor": data.get("deny_floor", []),
            "builtin_read_dirs": data.get("builtin_read_dirs", []),
            "builtin_write_dirs": data.get("builtin_write_dirs", []),
        }
    )


async def _save_fs_custom_paths(
    server: HomeAssistantSmartMCPServer | None, request: Request
) -> JSONResponse:
    """Replace the user-configured extra filesystem directories via the
    ha_mcp_tools component (issue #1567).

    The component validates each entry and drops anything that hits the
    deny floor or escapes the config dir; the dropped entries come back in
    ``rejected``. ``restart_required`` is False — the component applies the
    new allowlist live. A failure with a readable reason is a 400: a proxy such
    as Cloudflare replaces the body of a 502, and with it the reason the page
    shows.
    """
    from ..tools.tools_filesystem import is_filesystem_tools_enabled
    from ..tools.util_helpers import unwrap_service_response

    if not is_filesystem_tools_enabled():
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Filesystem tools are disabled; enable them (beta) before "
                "configuring custom directories.",
            ),
            status_code=409,
        )
    try:
        body = await request.json()
    except ValueError, TypeError:
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_JSON,
                "Request body must be valid JSON.",
            ),
            status_code=400,
        )
    paths = body.get("paths") if isinstance(body, dict) else None
    if paths is None:
        paths = []
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return JSONResponse(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "'paths' must be a list of directory strings.",
            ),
            status_code=400,
        )
    try:
        result = await _fs_custom_paths_call(
            server, "set_allowed_paths", {"paths": paths}
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("fs-custom-paths POST failed calling ha_mcp_tools: %s", exc)
        reason = _component_error_reason(exc)
        return JSONResponse(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                reason or f"Could not reach the ha_mcp_tools component: {exc}",
            ),
            status_code=400 if reason else 502,
        )

    data = unwrap_service_response(result) if isinstance(result, dict) else {}
    if not isinstance(data, dict) or not data.get("success", False):
        reason = (
            data.get("error") if isinstance(data, dict) else None
        ) or "ha_mcp_tools rejected the update."
        return JSONResponse(
            create_error_response(ErrorCode.SERVICE_CALL_FAILED, str(reason)),
            status_code=400,
        )
    return JSONResponse(
        {
            "success": True,
            "applied": data.get("paths", []),
            "paths": data.get("paths", []),
            "rejected": data.get("rejected", []),
            "mode": "component",
            "restart_required": False,
        }
    )


def build_fs_handlers(server: HomeAssistantSmartMCPServer | None) -> dict[str, Any]:
    """Construct the filesystem custom-paths route handlers."""

    async def get_fs_custom_paths(request: Request) -> JSONResponse:
        return await _get_fs_custom_paths(server, request)

    async def save_fs_custom_paths(request: Request) -> JSONResponse:
        return await _save_fs_custom_paths(server, request)

    return {
        "get_fs_custom_paths": get_fs_custom_paths,
        "save_fs_custom_paths": save_fs_custom_paths,
    }
