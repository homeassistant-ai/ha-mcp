"""The running ha-mcp version against the one installed on disk.

``__version__`` is frozen when the process first imports ha_mcp, so after an
in-place update the running process keeps reporting the old version while the
disk already carries the new one. Reporting both turns a "which server am I
talking to" hunt into a single tool call.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from ha_mcp import __version__

from .._version import get_version, is_dev_version, is_embedded

logger = logging.getLogger(__name__)


def detect_installed_version() -> str | None:
    """Return the installed ha-mcp version as ``get_version`` resolves it now.

    That is the package metadata on disk, or ``HA_MCP_BUILD_VERSION`` when set.
    None when it cannot be read.
    """
    try:
        importlib.invalidate_caches()
    except KeyError as e:
        # Python 3.14 with an editable homeassistant install (the official
        # container image, where the in-process server runs) raises here
        # (#1891); a read without the refresh still beats none.
        logger.warning("importlib.invalidate_caches() failed: %s", e)
    try:
        version = get_version()
    except Exception as e:  # noqa: BLE001
        logger.info("Installed-version probe failed: %s", e)
        return None
    return None if version == "unknown" else version


def version_mismatch(installed: str | None) -> bool:
    """True when ``installed`` is not the version this process imported.

    A failed probe (None) is no mismatch.
    """
    return bool(installed and installed != __version__)


def running_version_report() -> tuple[dict[str, Any], list[str]]:
    """``info``'s version fields, and a warning when they disagree or the
    installed version cannot be read.

    ``server_version`` is the running one. In-process the hint names a Home
    Assistant restart, as the component's own staleness warning does.
    """
    installed = detect_installed_version()
    mismatch = version_mismatch(installed)
    fields = {
        "server_version": __version__,
        "installed_version": installed,
        "version_mismatch": mismatch,
        "is_dev_build": is_dev_version(__version__),
    }
    if installed is None:
        return fields, [
            "Could not read the installed server version; the running and "
            "installed versions were not compared."
        ]
    if not mismatch:
        return fields, []
    restart = "Home Assistant" if is_embedded() else "the server"
    return fields, [
        f"Running {__version__}, but {installed} is installed; restart "
        f"{restart} to finish applying the update."
    ]
