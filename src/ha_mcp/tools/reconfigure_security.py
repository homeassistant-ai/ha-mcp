"""Shared safety helpers for integration reconfiguration flows."""

from __future__ import annotations

import re
from typing import Any

_REDACTED = "[REDACTED]"
_SECRET_MARKERS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "credential",
        "credentials",
        "api_key",
        "apikey",
        "access_key",
        "private_key",
        "connection_string",
        "client_secret",
        "refresh_token",
        "psk",
        "p_s_k",
        "noise_psk",
    }
)


def _normalise_key(key: str) -> str:
    """Convert common camelCase and delimiter variants to snake_case."""
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", key)
    return snake.lower().replace("-", "_")


def _is_sensitive_key(key: str) -> bool:
    normalised = _normalise_key(key)
    return normalised in _SECRET_MARKERS or any(
        marker in normalised
        for marker in (
            "password",
            "secret",
            "token",
            "credential",
            "api_key",
            "private_key",
            "connection_string",
            "psk",
            "p_s_k",
            "noise_psk",
        )
    )


def _contains_sensitive_value(value: Any, key: str | None = None) -> bool:
    """Return whether a value contains a field that cannot be replayed safely."""
    if key is not None and _is_sensitive_key(key):
        return True
    if isinstance(value, dict):
        return any(
            _contains_sensitive_value(item, str(item_key))
            for item_key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive_value(item) for item in value)
    return False


def redact_reconfigure_value(value: Any, key: str | None = None) -> Any:
    """Recursively redact credentials in flow data and error contexts."""
    if key is not None and _is_sensitive_key(key):
        return _REDACTED
    if isinstance(value, dict):
        return {
            str(item_key): redact_reconfigure_value(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_reconfigure_value(item) for item in value]
    if isinstance(value, tuple):
        return [redact_reconfigure_value(item) for item in value]
    return value


def build_reconfigure_rollback_metadata(
    entry_id: str,
    domain: str,
    before_entry: dict[str, Any],
) -> dict[str, Any]:
    """Describe the honest rollback path for an existing config entry.

    The generic integration backup is intentionally not replayed: its
    credentials are redacted and its shared restore handler only supports the
    entry's enabled/disabled state. Endpoint rollback therefore means an
    operator repeats Home Assistant's official reconfigure flow with the
    previous non-sensitive config.
    """
    previous_config = before_entry.get("data")
    if not isinstance(previous_config, dict):
        previous_config = None
        manual_reason = "previous_config_unavailable"
    elif _contains_sensitive_value(previous_config):
        manual_reason = "previous_config_contains_redacted_secrets"
    else:
        manual_reason = None

    redacted_config = redact_reconfigure_value(previous_config)
    return {
        "strategy": "official_reconfigure_flow",
        "automatic": False,
        "operator_action_required": True,
        "manual_required": manual_reason is not None,
        "manual_reason": manual_reason,
        "entry_id": entry_id,
        "domain": domain,
        "previous_config": redacted_config,
        "backup_scope": "edits",
        "backup_restore_supported": False,
        "backup_restore_note": (
            "The generic integration snapshot is redacted and does not restore "
            "connection settings; HA REST config-entry data may be unavailable. "
            "Use the official reconfigure flow instead."
        ),
    }
