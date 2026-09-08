"""Capability-negotiated dashboard writes with no retry after a sent command."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, NoReturn

from ..client.rest_client import HomeAssistantCommandError, HomeAssistantCommandNotSent
from ..client.websocket_client import get_websocket_client
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .dashboard_edit_errors import raise_dashboard_edit_error

WS_DASHBOARD_EDIT = "ha_mcp_tools/dashboard_edit"
_LOGGER = logging.getLogger(__name__)


def _invalid_success_fields(result: dict[str, Any]) -> list[str]:
    """Identify invalid consumed fields without copying dashboard values into logs."""
    verified = result.get("post_write_verified")
    config_hash = result.get("config_hash")
    size = result.get("previous_config_size")
    warnings = result.get("warnings", [])
    checks = {
        "success": result.get("success") is True,
        "config": isinstance(result.get("config"), dict),
        "config_hash": "config_hash" in result
        and (isinstance(config_hash, str) if verified else config_hash is None),
        "post_write_verified": isinstance(verified, bool),
        "write_committed": isinstance(result.get("write_committed"), bool),
        "previous_config_size": isinstance(size, int)
        and not isinstance(size, bool)
        and size >= 0,
        "warnings": isinstance(warnings, list)
        and all(isinstance(warning, str) for warning in warnings),
        "unchanged": isinstance(result.get("unchanged", False), bool),
    }
    return [name for name, valid in checks.items() if not valid]


def _raise_malformed_reply(
    url_path: str | None, action: str, fields: list[str]
) -> NoReturn:
    """Diagnose protocol skew without logging the config, templates, or card values."""
    details = ", ".join(fields)
    _LOGGER.warning("Malformed dashboard_edit response; invalid fields: %s", details)
    raise_dashboard_edit_error(
        url_path,
        "write_outcome_unknown",
        f"Malformed response fields: {details}",
        None,
        action,
    )


def _validate_edit_result(
    raw: Any, url_path: str | None, action: str, *, hash_supplied: bool = False
) -> dict[str, Any]:
    """An incomplete response cannot establish whether the write happened."""
    if not isinstance(raw, dict):
        _raise_malformed_reply(url_path, action, ["envelope"])
    if raw.get("success") is not True:
        _raise_malformed_reply(url_path, action, ["envelope.success"])
    result = raw.get("result")
    if not isinstance(result, dict):
        _raise_malformed_reply(url_path, action, ["result"])
    if result.get("success") is False:
        error = result.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("code"), str):
            _raise_malformed_reply(url_path, action, ["error.code"])
        committed = result.get("write_committed")
        if committed is not False and committed is not None:
            _raise_malformed_reply(url_path, action, ["write_committed"])
        raise_dashboard_edit_error(
            url_path,
            error["code"],
            str(error.get("message", error["code"])),
            committed,
            action,
            hash_supplied=hash_supplied,
        )
    invalid_fields = _invalid_success_fields(result)
    if invalid_fields:
        _raise_malformed_reply(url_path, action, invalid_fields)
    return result


def _handle_command_failure(
    exc: BaseException, client: Any, url_path: str | None, action: str
) -> None:
    """Only a missing command allows fallback; other failures terminate the edit."""
    if isinstance(exc, HomeAssistantCommandNotSent):
        raise_dashboard_edit_error(url_path, "write_not_sent", str(exc), False, action)
    if isinstance(exc, Exception) and is_unknown_command(exc):
        invalidate_caps(client)
        return
    # Core's schema/admin gates reject these before this command's prep runs.
    # Other command errors can originate after dispatch and remain ambiguous.
    if isinstance(exc, HomeAssistantCommandError) and exc.code in {
        "unauthorized",
        "invalid_format",
    }:
        raise_dashboard_edit_error(url_path, exc.code, str(exc), False, action)
    _LOGGER.warning("Dashboard edit command failed after dispatch", exc_info=exc)
    raise_dashboard_edit_error(
        url_path,
        "write_outcome_unknown",
        str(exc) or "Dashboard edit interrupted before its response was received",
        None,
        action,
    )


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

    Legacy fallback requires confirmed absence of a compatible capability or
    HA's definitive unknown_command. Never use the REST client's retrying
    WebSocket bridge for a native write.
    ``action`` labels local error responses only; it is not a command argument.
    """
    # Clients without connection credentials cannot probe the component.
    if not getattr(client, "base_url", None) or not getattr(client, "token", None):
        return None
    try:
        caps = await get_component_caps(client, strict=True)
        if not component_supports(caps, "dashboard_edit"):
            return None
        ws = await get_websocket_client(
            url=client.base_url,
            token=client.token,
            verify_ssl=getattr(client, "verify_ssl", None),
        )
    except Exception as exc:
        _LOGGER.warning(
            "Unable to prepare the dashboard edit connection", exc_info=True
        )
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
    except (asyncio.CancelledError, Exception) as exc:
        # Pre-dispatch cancellation still propagates; after dispatch it cannot
        # establish whether Core saved unless an explicit rejection says so.
        _handle_command_failure(exc, client, url_path, action)
        return None
    return _validate_edit_result(
        raw, url_path, action, hash_supplied=expected_hash is not None
    )
