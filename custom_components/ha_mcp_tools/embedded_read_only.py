"""Persistent Read Only Mode state for the embedded server switch."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .const import SERVER_CONFIG_SUBDIR

_FEATURE_FLAGS_FILENAME = "feature_flags.json"
_READ_ONLY_FIELD = "read_only_mode"
_READ_ONLY_ENV = "READ_ONLY_MODE"


class ReadOnlyStateError(ValueError):
    """The persisted Read Only Mode state cannot be safely read or changed."""


def embedded_config_dir(config_dir: str) -> Path:
    """Return the embedded server's persistent data directory."""
    return Path(config_dir) / SERVER_CONFIG_SUBDIR


def read_only_mode(config_dir: Path) -> bool:
    """Read the effective embedded Read Only Mode value."""
    env_value = os.environ.get(_READ_ONLY_ENV)
    if env_value is not None:
        normalized = env_value.lower().strip()
        if normalized in ("1", "true", "yes", "on", "enable"):
            return True
        if normalized in ("0", "false", "no", "off", "disable"):
            return False
        raise ReadOnlyStateError(f"{_READ_ONLY_ENV} is not a valid boolean")

    values = _read_overrides(config_dir)
    value = values.get(_READ_ONLY_FIELD, False)
    if not isinstance(value, bool):
        raise ReadOnlyStateError(
            f"{_READ_ONLY_FIELD!r} in {_FEATURE_FLAGS_FILENAME} must be a boolean"
        )
    return value


def set_read_only_mode(config_dir: Path, enabled: bool) -> None:
    """Persist Read Only Mode without dropping other server overrides."""
    if os.environ.get(_READ_ONLY_ENV) is not None:
        raise ReadOnlyStateError(
            f"Read Only Mode is locked by the {_READ_ONLY_ENV} environment variable"
        )

    config_dir.mkdir(parents=True, exist_ok=True)
    from ha_mcp.settings_ui._persistence import _atomic_write_json
    from ha_mcp.utils.config_write_lock import config_file_lock

    with config_file_lock(config_dir):
        values = _read_overrides(config_dir)
        values[_READ_ONLY_FIELD] = enabled
        _atomic_write_json(config_dir / _FEATURE_FLAGS_FILENAME, values)

    # The read-only middleware consults get_global_settings() on each request.
    # Drop its singleton so the very next tool call sees the saved value.
    from ha_mcp.config import reset_global_settings

    reset_global_settings()


def _read_overrides(config_dir: Path) -> dict[str, Any]:
    path = config_dir / _FEATURE_FLAGS_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise ReadOnlyStateError(f"Could not read {path}: {err}") from err

    try:
        values = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ReadOnlyStateError(
            f"Existing override file at {path} is not valid JSON; refusing to overwrite"
        ) from err
    if not isinstance(values, dict):
        raise ReadOnlyStateError(
            f"Existing override file at {path} must contain a JSON object"
        )
    return values
