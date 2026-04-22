"""Version resolution for the ha-mcp package.

Kept as a standalone module (no other ``ha_mcp`` imports) so it can be used from
``__init__.py`` and ``config.py`` without circular-import risk.
"""

from __future__ import annotations

import importlib.metadata
import os


def get_version() -> str:
    """Return the installed ha-mcp version.

    Resolution order:
    1. ``HA_MCP_BUILD_VERSION`` env var — set by Docker/add-on builds that can't
       rewrite ``pyproject.toml`` before install, so the dev suffix still reaches
       the running process. Stable builds leave it unset.
    2. ``ha-mcp`` package metadata — stable PyPI + stable Docker.
    3. ``ha-mcp-dev`` package metadata — PyPI dev channel (renamed package).
    """
    if override := os.environ.get("HA_MCP_BUILD_VERSION"):
        return override
    for pkg_name in ("ha-mcp", "ha-mcp-dev"):
        try:
            return importlib.metadata.version(pkg_name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return "unknown"


def is_dev_version(version: str) -> bool:
    """Return True when the version string contains a PEP 440 ``.dev`` suffix."""
    return ".dev" in version


def is_running_in_addon() -> bool:
    """Return True when running inside a Home Assistant add-on container.

    The HA Supervisor injects ``SUPERVISOR_TOKEN`` into every add-on's env.
    Checked so the standalone-Docker ``:stable`` banner isn't shown to add-on
    users, who already see the dev/stable distinction in the HAOS add-on UI.
    """
    return bool(os.environ.get("SUPERVISOR_TOKEN"))
