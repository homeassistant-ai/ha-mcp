"""Dependency-light helpers shared by ha-mcp and its Home Assistant component.

This module deliberately has no imports from :mod:`ha_mcp`.  Home Assistant can
therefore read and update the embedded server's feature flags while a worker
thread is cold-importing the main package without participating in that import.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

FEATURE_FLAGS_FILENAME = "feature_flags.json"
READ_ONLY_FIELD = "read_only_mode"
READ_ONLY_ENV = "READ_ONLY_MODE"

_TRUE_STRINGS = frozenset({"1", "on", "t", "true", "y", "yes"})
_FALSE_STRINGS = frozenset({"0", "off", "f", "false", "n", "no"})
_LOGGER = logging.getLogger("ha_mcp.config")
_RESET_LOCK = threading.Lock()
_RESET_SETTINGS: Callable[[], None] | None = None


class FeatureFlagStateError(ValueError):
    """An override file cannot be safely read or changed."""


def _environment_value(values: Mapping[str, str], name: str) -> str | None:
    """Resolve an environment name like pydantic-settings does."""
    normalized = name.lower()
    return {key.lower(): value for key, value in values.items()}.get(normalized)


def coerce_feature_flag_bool(raw: object) -> tuple[bool, bool]:
    """Apply the server's bool-or-integer override-file rule."""
    if not isinstance(raw, bool | int):
        return False, False
    return True, bool(raw)


def parse_boolean_string(raw: str) -> bool:
    """Parse the boolean spellings accepted by the server settings model."""
    normalized = raw.lower().strip()
    if normalized in _TRUE_STRINGS:
        return True
    if normalized in _FALSE_STRINGS:
        return False
    raise FeatureFlagStateError(f"{raw!r} is not a valid boolean")


def read_only_mode(config_dir: Path, environ: Mapping[str, str] | None = None) -> bool:
    """Resolve Read Only Mode with the server's env-over-file precedence."""
    values = os.environ if environ is None else environ
    env_value = _environment_value(values, READ_ONLY_ENV)
    if env_value is not None:
        try:
            return parse_boolean_string(env_value)
        except FeatureFlagStateError as err:
            raise FeatureFlagStateError(
                f"{READ_ONLY_ENV} is not a valid boolean"
            ) from err

    overrides = read_feature_flags(config_dir)
    value = overrides.get(READ_ONLY_FIELD, False)
    valid, resolved = coerce_feature_flag_bool(value)
    if valid:
        return resolved
    raise FeatureFlagStateError(
        f"{READ_ONLY_FIELD!r} in {FEATURE_FLAGS_FILENAME} must be a boolean or integer"
    )


def read_feature_flags(config_dir: Path) -> dict[str, Any]:
    """Read an override object strictly so a later write cannot lose data."""
    path = config_dir / FEATURE_FLAGS_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as err:
        raise FeatureFlagStateError(f"Could not read {path}: {err}") from err

    try:
        values = json.loads(raw)
    except json.JSONDecodeError as err:
        raise FeatureFlagStateError(
            f"Existing override file at {path} is not valid JSON; refusing to overwrite"
        ) from err
    if not isinstance(values, dict):
        raise FeatureFlagStateError(
            f"Existing override file at {path} must contain a JSON object"
        )
    return values


def set_read_only_mode(config_dir: Path, enabled: bool) -> None:
    """Atomically persist Read Only Mode without dropping other overrides."""
    if _environment_value(os.environ, READ_ONLY_ENV) is not None:
        raise FeatureFlagStateError(
            f"Read Only Mode is locked by the {READ_ONLY_ENV} environment variable"
        )
    config_dir.mkdir(parents=True, exist_ok=True)
    with file_write_lock(config_dir / ".config_write.lock"):
        values = read_feature_flags(config_dir)
        values[READ_ONLY_FIELD] = enabled
        atomic_write_json(config_dir / FEATURE_FLAGS_FILENAME, values)
    notify_settings_changed()


def register_settings_reset(callback: Callable[[], None]) -> None:
    """Publish the loaded server generation's settings reset callback."""
    global _RESET_SETTINGS
    with _RESET_LOCK:
        _RESET_SETTINGS = callback


def notify_settings_changed() -> None:
    """Reset a loaded server without importing it from the component thread."""
    with _RESET_LOCK:
        callback = _RESET_SETTINGS
    if callback is not None:
        callback()


def atomic_write_json(
    path: Path, payload: dict[str, Any] | list[dict[str, Any]]
) -> None:
    """Write JSON through a unique same-directory temporary file and replace."""
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2))
        os.replace(tmp_name, str(path))
    except OSError, ValueError:
        with contextlib.suppress(FileNotFoundError, OSError):
            Path(tmp_name).unlink()
        raise


@contextlib.contextmanager
def file_write_lock(lock_path: Path, *, required: bool = False) -> Iterator[None]:
    """Hold an advisory cross-process lock, degrading safely when optional."""
    fd: int | None = None
    locked = False
    try:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
                locked = True
            elif msvcrt is not None:  # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                locked = True
            elif required:
                raise OSError("Cross-process file locking is unavailable")
        except OSError:
            if required:
                raise
            _LOGGER.warning(
                "config write lockfile unavailable; proceeding without "
                "cross-process lock",
                exc_info=True,
            )
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                if locked:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    elif msvcrt is not None:  # pragma: no cover - Windows
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            with contextlib.suppress(OSError):
                os.close(fd)
