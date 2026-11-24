"""Persistent Read Only Mode state for the embedded server switch.

The implementation lives in a dependency-light server module. This component
loads that one file directly rather than importing :mod:`ha_mcp`, whose eager
package import belongs exclusively to the embedded worker thread.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

from .const import SERVER_CONFIG_SUBDIR

__all__ = (
    "ReadOnlyStateError",
    "embedded_config_dir",
    "prepare_runtime_config",
    "read_only_mode",
    "set_read_only_mode",
)

_RUNTIME_MODULE = "ha_mcp.runtime_config"
_RUNTIME_LOCK = threading.Lock()


class ReadOnlyStateError(ValueError):
    """The server's Read Only Mode state cannot be safely read or changed."""


def _runtime_config() -> ModuleType:
    """Load the server's dependency-light helper without importing its package."""
    with _RUNTIME_LOCK:
        loaded = sys.modules.get(_RUNTIME_MODULE)
        if loaded is not None:
            return loaded

        try:
            package = importlib.util.find_spec("ha_mcp")
        except (ImportError, ValueError) as err:
            raise ReadOnlyStateError(
                "the ha-mcp server package is unavailable"
            ) from err
        locations = None if package is None else package.submodule_search_locations
        if not locations:
            raise ReadOnlyStateError("the ha-mcp server package is unavailable")
        path = Path(next(iter(locations))) / "runtime_config.py"
        if not path.is_file():
            raise ReadOnlyStateError(
                "the installed ha-mcp server does not provide runtime configuration"
            )

        spec = importlib.util.spec_from_file_location(_RUNTIME_MODULE, path)
        if spec is None or spec.loader is None:
            raise ReadOnlyStateError(
                f"could not load runtime configuration from {path}"
            )
        module = importlib.util.module_from_spec(spec)
        sys.modules[_RUNTIME_MODULE] = module
        try:
            spec.loader.exec_module(module)
        except Exception as err:
            if sys.modules.get(_RUNTIME_MODULE) is module:
                sys.modules.pop(_RUNTIME_MODULE, None)
            raise ReadOnlyStateError(
                f"could not load runtime configuration from {path}: {err}"
            ) from err
        return module


def prepare_runtime_config() -> None:
    """Load the helper before the worker begins importing :mod:`ha_mcp`."""
    _runtime_config()


def _translate_error(runtime: ModuleType, err: Exception) -> ReadOnlyStateError:
    error_type = getattr(runtime, "FeatureFlagStateError", ())
    if isinstance(err, error_type):
        return ReadOnlyStateError(str(err))
    raise err


def read_only_mode(config_dir: Path, environ: Mapping[str, str] | None = None) -> bool:
    """Resolve Read Only Mode with the server's canonical rules."""
    runtime = _runtime_config()
    try:
        return bool(runtime.read_only_mode(config_dir, environ))
    except Exception as err:
        raise _translate_error(runtime, err) from err


def set_read_only_mode(config_dir: Path, enabled: bool) -> None:
    """Persist Read Only Mode with the server's canonical rules."""
    runtime = _runtime_config()
    try:
        runtime.set_read_only_mode(config_dir, enabled)
    except Exception as err:
        raise _translate_error(runtime, err) from err


def embedded_config_dir(config_dir: str) -> Path:
    """Return the embedded server's persistent data directory."""
    return Path(config_dir) / SERVER_CONFIG_SUBDIR
