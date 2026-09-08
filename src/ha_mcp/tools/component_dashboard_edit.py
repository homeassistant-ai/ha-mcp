"""Capability-negotiated dashboard writes with no retry after a sent command."""

from __future__ import annotations

import asyncio
from typing import Any

from ..client.rest_client import HomeAssistantCommandNotSent
from ..client.websocket_client import get_websocket_client
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .dashboard_edit_errors import raise_dashboard_edit_error

WS_DASHBOARD_EDIT = "ha_mcp_tools/dashboard_edit"


def _valid_success(result: dict[str, Any]) -> bool:
    """Validate every field consumed after the write before reporting success."""
    verified = result.get("post_write_verified")
    config_hash = result.get("config_hash")
    size = result.get("previous_config_size")
    warnings = result.get("warnings", [])
    return (
        result.get("success") is True
        and isinstance(result.get("config"), dict)
        and "config_hash" in result
        and isinstance(verified, bool)
        and (isinstance(config_hash, str) if verified else config_hash is None)
        and isinstance(result.get("write_committed"), bool)
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size >= 0
        and isinstance(warnings, list)
        and all(isinstance(warning, str) for warning in warnings)
        and isinstance(result.get("unchanged", False), bool)
    )


def _validate_edit_result(raw: Any, url_path: str | None, action: str) -> dict[str, Any]:
    """An incomplete response cannot establish whether the write happened."""
    result = raw.get("result") if isinstance(raw, dict) else None
    if not isinstance(result, dict) or raw.get("success") is not True:
        raise_dashboard_edit_error(
            url_path, "write_outcome_unknown", "Malformed response", None, action
        )
    if result.get("success") is False:
        error = result.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            committed = result.get("write_committed")
            if committed is False or committed is None:
                raise_dashboard_edit_error(
                    url_path,
                    error["code"],
                    str(error.get("message", error["code"])),
                    committed,
                    action,
                )
        raise_dashboard_edit_error(
            url_path, "write_outcome_unknown", "Malformed error", None, action
        )
    if not _valid_success(result):
        raise_dashboard_edit_error(
            url_path, "write_outcome_unknown", "Malformed response", None, action
        )
    return result


async def edit_dashboard_via_component(
    client: Any,
    url_path: str | None,
    *,
    action: str = "set",
    expected_hash: str | None = None,
    config: dict[str, Any] | None = None,
    patch: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Return an authoritative edit result, or None for an unavailable command.

    Only capability absence or HA's definitive unknown_command permits legacy
    fallback. Never use the REST client's retrying WebSocket bridge for a write.
    ``action`` labels local error responses only; it is not a command argument.
    """
    # Clients without connection credentials cannot probe the component.
    if not getattr(client, "base_url", None) or not getattr(client, "token", None):
        return None
    try:
        caps = await get_component_caps(client)
        if not component_supports(caps, "dashboard_edit"):
            return None
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
    except Exception as exc:
        raise_dashboard_edit_error(url_path, "load_failed", str(exc), False, action)

    kwargs: dict[str, Any] = {"url_path": url_path}
    if expected_hash is not None:
        kwargs["expected_hash"] = expected_hash
    if config is not None:
        kwargs["config"] = config
    if patch is not None:
        kwargs["patch"] = patch
    try:
        raw = await ws.send_command(WS_DASHBOARD_EDIT, **kwargs)
    except HomeAssistantCommandNotSent as exc:
        raise_dashboard_edit_error(url_path, "load_failed", str(exc), False, action)
    except (asyncio.CancelledError, Exception) as exc:
        # Cancellation during send/response wait cannot prove HA did not save.
        # Convert only at this write boundary; pre-dispatch cancellation propagates.
        if isinstance(exc, Exception) and is_unknown_command(exc):
            invalidate_caps(client)
            return None
        raise_dashboard_edit_error(
            url_path,
            "write_outcome_unknown",
            str(exc) or "Dashboard edit interrupted before its response was received",
            None,
            action,
        )
    return _validate_edit_result(raw, url_path, action)
