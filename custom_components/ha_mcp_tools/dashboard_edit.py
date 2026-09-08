"""Compare and edit storage dashboards on Home Assistant's event loop.

Core's loaded LovelaceStorage replaces its cache and fires lovelace_updated
before yielding to persistence. Preload first, then keep the hash comparison,
validation and save invocation together without a yielding await. Save completion
is an API acknowledgement: Core's Store logs and swallows some disk errors.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any

from .dashboard_patch import apply_dashboard_patch

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


_LOGGER = logging.getLogger(__name__)


class _EditError(Exception):
    """A dashboard validation/load error; its phase determines the write outcome."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _failure(code: str, message: str, *, unknown: bool = False) -> dict[str, Any]:
    return {
        "success": False,
        "error": {"code": code, "message": message},
        "write_committed": None if unknown else False,
        "post_write_verified": False,
    }


def _normalize(config: Any) -> dict[str, Any]:
    """Copy through the same JSON encoder as Core's WebSocket config response."""
    from homeassistant.helpers.json import json_bytes

    result = json.loads(json_bytes(config))
    if not isinstance(result, dict):
        raise ValueError("Dashboard config must be an object")
    return result


def _config_hash(config: dict[str, Any]) -> str:
    """Keep byte compatibility with ha_mcp.utils.config_hash.compute_config_hash."""
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _resolve_dashboard(hass: HomeAssistant, url_path: str | None) -> Any:
    from .websocket_api import _lovelace_dashboards_map

    dashboards = _lovelace_dashboards_map(hass)
    if dashboards is None:
        return None
    if url_path in (None, "lovelace"):
        return dashboards.get("lovelace") or dashboards.get(None)
    return dashboards.get(url_path)


def _require_storage(dashboard: Any) -> None:
    if dashboard is None:
        raise _EditError("not_found", "Dashboard not found")
    if dashboard.mode == "yaml":
        raise _EditError("yaml_not_supported", "YAML dashboards cannot be edited here")
    if dashboard.mode != "storage":
        raise _EditError("unsupported_mode", "Dashboard does not use storage mode")


def _validate_message(msg: dict[str, Any]) -> None:
    if ("config" in msg) == ("patch" in msg):
        raise _EditError("validation_failed", "Provide exactly one of config or patch")
    if msg.get("url_path") is not None and not isinstance(msg["url_path"], str):
        raise _EditError("validation_failed", "url_path must be a string or null")
    expected_hash = msg.get("expected_hash")
    if expected_hash is not None and not isinstance(expected_hash, str):
        raise _EditError("validation_failed", "expected_hash must be a string")
    if "patch" in msg:
        if not isinstance(msg["patch"], list) or not isinstance(expected_hash, str):
            raise _EditError(
                "validation_failed",
                "patch requires an operation list and expected_hash",
            )
    elif not isinstance(msg["config"], dict):
        raise _EditError("validation_failed", "config must be an object")


def _prepare_edit(
    current: dict[str, Any] | None, msg: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any], str, int]:
    """Compare the loaded config and validate detached candidates without yielding."""
    current_plain = _normalize(current) if current is not None else {}
    current_hash = _config_hash(current_plain)
    if msg.get("expected_hash") is not None and msg["expected_hash"] != current_hash:
        raise _EditError("conflict", "Dashboard modified since last read (conflict)")
    previous_config_size = len(json.dumps(current_plain))
    try:
        candidate = (
            apply_dashboard_patch(current_plain, msg["patch"])
            if "patch" in msg
            else msg["config"]
        )
        candidate = _normalize(candidate)
        # Save owns candidate after the call; keep a detached fallback for a
        # failed readback, and finish serialization checks BEFORE saving.
        fallback = _normalize(candidate)
    except (TypeError, ValueError, OverflowError) as err:
        raise _EditError("validation_failed", str(err)) from err
    if "strategy" in current_plain and "strategy" not in candidate:
        raise _EditError(
            "strategy_conversion",
            "Strategy dashboards cannot be converted to custom dashboards via this tool",
        )
    return candidate, fallback, current_hash, previous_config_size


async def async_edit_dashboard(
    hass: HomeAssistant, msg: dict[str, Any]
) -> dict[str, Any]:
    """Apply one edit with an atomic cached-config comparison and verified readback."""
    save_started = False
    try:
        from homeassistant.components.lovelace.const import ConfigNotFound

        _validate_message(msg)
        url_path = msg.get("url_path")
        dashboard = _resolve_dashboard(hass, url_path)
        _require_storage(dashboard)
        try:
            current = await dashboard.async_load(False)
        except ConfigNotFound:
            if "config" not in msg or msg.get("expected_hash") is not None:
                raise _EditError("not_found", "Dashboard has no saved config") from None
            current = None

        # Preloading can yield. A deleted/replaced dashboard must not be written
        # through its stale object, even if the replacement has the same config.
        if _resolve_dashboard(hass, url_path) is not dashboard:
            raise _EditError("conflict", "Dashboard changed while loading its config")
        _require_storage(dashboard)
        candidate, fallback, current_hash, previous_config_size = _prepare_edit(
            current, msg
        )

        if "patch" in msg and _config_hash(candidate) == current_hash:
            return {
                "success": True,
                "unchanged": True,
                "config": candidate,
                "config_hash": current_hash,
                "previous_config_size": previous_config_size,
                "write_committed": False,
                "post_write_verified": True,
            }

        # No yielding await since the fresh comparison above. With its cache
        # preloaded, Core replaces config and emits the event before its first
        # persistence await. Mark started BEFORE entering even that coroutine:
        # an exception cannot prove that the cache/event/save did not occur.
        save_started = True
        await dashboard.async_save(candidate)
    except (Exception, asyncio.CancelledError) as err:
        if save_started:
            _LOGGER.warning("Dashboard save did not complete normally", exc_info=True)
            return _failure(
                "write_outcome_unknown",
                "Dashboard save did not complete normally; read its current config before retrying",
                unknown=True,
            )
        if isinstance(err, _EditError):
            return _failure(err.code, str(err))
        _LOGGER.warning("Unable to prepare the dashboard edit", exc_info=True)
        return _failure("load_failed", "Unable to prepare the dashboard edit")

    try:
        # A concurrent writer may have updated or replaced the dashboard while
        # persistence yielded. Return the current authoritative config, not an
        # assumed echo of our candidate, and never read a replacement YAML body.
        dashboard = _resolve_dashboard(hass, url_path)
        _require_storage(dashboard)
        config = _normalize(await dashboard.async_load(False))
        if _resolve_dashboard(hass, url_path) is not dashboard:
            raise _EditError("conflict", "Dashboard changed during verification")
        config_hash = _config_hash(config)
    except (Exception, asyncio.CancelledError):
        _LOGGER.warning("Unable to verify the saved dashboard config", exc_info=True)
        return {
            "success": True,
            "config": fallback,
            "config_hash": None,
            "previous_config_size": previous_config_size,
            "write_committed": True,
            "post_write_verified": False,
            "warnings": [
                "Dashboard save completed, but its current config could not be verified. "
                "Read the dashboard before editing it again."
            ],
        }
    return {
        "success": True,
        "config": config,
        "config_hash": config_hash,
        "previous_config_size": previous_config_size,
        "write_committed": True,
        "post_write_verified": True,
    }
