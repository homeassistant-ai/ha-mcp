"""Persistent Read Only Mode state for the embedded server switch.

The implementation lives in a dependency-light module shipped beside the
server package.  Importing it never cold-imports :mod:`ha_mcp` on Home
Assistant's executor while the embedded worker is importing that package.
"""

from __future__ import annotations

from pathlib import Path

from hamcp_runtime_config import (
    FeatureFlagStateError as ReadOnlyStateError,
)
from hamcp_runtime_config import (
    read_only_mode,
    set_read_only_mode,
)

from .const import SERVER_CONFIG_SUBDIR

__all__ = (
    "ReadOnlyStateError",
    "embedded_config_dir",
    "read_only_mode",
    "set_read_only_mode",
)


def embedded_config_dir(config_dir: str) -> Path:
    """Return the embedded server's persistent data directory."""
    return Path(config_dir) / SERVER_CONFIG_SUBDIR
