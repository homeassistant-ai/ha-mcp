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
        )
    )


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
