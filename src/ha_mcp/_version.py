"""Version resolution for the ha-mcp package.

Kept as a standalone module (no other ``ha_mcp`` imports) so it can be used from
``__init__.py`` and ``config.py`` without circular-import risk.
"""

from __future__ import annotations

import importlib.metadata
import logging
import os

logger = logging.getLogger(__name__)


def get_version() -> str:
    """Return the installed ha-mcp version.

    Resolution order:
    1. ``HA_MCP_BUILD_VERSION`` env var — set by Docker/add-on builds that can't
       rewrite ``pyproject.toml`` before install, so the dev suffix still reaches
       the running process. Stable builds leave it unset.
    2. ``ha-mcp`` package metadata — stable PyPI + stable Docker.
    3. ``ha-mcp-dev`` package metadata — PyPI dev channel (renamed package).

    If none of the above resolve, logs a warning and returns ``"unknown"``.
    The "unknown" string is itself diagnostic in bug reports and startup logs
    — it tells triagers the install didn't register package metadata (e.g. a
    source checkout without ``pip install -e .``, or a broken Docker layer).
    """
    if override := os.environ.get("HA_MCP_BUILD_VERSION"):
        return override
    # Prefer the distribution that actually OWNS the installed ha_mcp package.
    # Both channel dists (ha-mcp / ha-mcp-dev) can leave metadata behind — an
    # interrupted channel switch, a best-effort uninstall that failed — and a
    # fixed name order then reports the leftover dist's version instead of the
    # one whose files are really installed.
    try:
        owners = importlib.metadata.packages_distributions().get("ha_mcp", [])
        unique = sorted(set(owners))
        if len(unique) == 1:
            return importlib.metadata.version(unique[0])
    except Exception as exc:
        logger.debug("packages_distributions probe failed: %s", exc)
    for pkg_name in ("ha-mcp", "ha-mcp-dev"):
        try:
            return importlib.metadata.version(pkg_name)
        except importlib.metadata.PackageNotFoundError:
            continue
    logger.warning(
        "ha-mcp package metadata not found and HA_MCP_BUILD_VERSION unset — "
        "version will be reported as 'unknown'. Reinstall the package or set "
        "HA_MCP_BUILD_VERSION if this is an intentional source-tree run."
    )
    return "unknown"


def is_dev_version(version: str) -> bool:
    """Return True when the version string contains a PEP 440 ``.dev`` suffix."""
    return ".dev" in version


def is_embedded() -> bool:
    """Return True when ha-mcp runs in-process inside Home Assistant core.

    Set to ``1`` by the ha_mcp_tools custom component's in-process server entry
    (``HA_MCP_EMBEDDED``) before the first ``ha_mcp`` import. On HAOS the
    HA core container itself carries ``SUPERVISOR_TOKEN``, so without this flag
    :func:`is_running_in_addon` would report True in-process and route log,
    add-on-management, and settings-persistence logic down the add-on path. The
    embedded server is an ordinary admin client of HA core (loopback URL +
    provisioned token), so those call sites must take their non-add-on branch.
    """
    return bool(os.environ.get("HA_MCP_EMBEDDED"))


def is_running_in_addon() -> bool:
    """Return True when running inside a Home Assistant add-on container.

    The HA Supervisor injects ``SUPERVISOR_TOKEN`` into every add-on's env.
    Checked so the standalone-Docker ``:stable`` banner isn't shown to add-on
    users, who already see the dev/stable distinction in the HAOS add-on UI.

    Returns False in embedded mode (:func:`is_embedded`) even though the HA core
    container carries ``SUPERVISOR_TOKEN``: the in-process server is a plain
    admin client of HA core, not a Supervisor-managed add-on, so add-on-only
    behavior (Supervisor-direct log fetch, add-on settings routing) must not
    apply.
    """
    return bool(os.environ.get("SUPERVISOR_TOKEN")) and not is_embedded()


def get_supervisor_base_url() -> str:
    """Return the base URL for direct Supervisor REST calls.

    Defaults to ``http://supervisor`` (the in-addon Supervisor hostname). The
    ``SUPERVISOR_BASE_URL`` env var override exists so E2E tests can point the
    direct-Supervisor httpx call sites at a local mock without /etc/hosts or
    DNS hacks. Production add-ons leave it unset.
    """
    return os.environ.get("SUPERVISOR_BASE_URL", "http://supervisor")
