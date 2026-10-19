"""The running ha-mcp version against the one installed on disk.

``__version__`` is frozen when the process first imports ha_mcp, so after an
in-place update the running process keeps reporting the old version while the
disk already carries the new one. ``ha_report_issue`` and
``ha_dev_manage_server(action="info")`` both report the two side by side, which
turns a "which server am I talking to" hunt into a single tool call.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from ha_mcp import __version__

from .._version import get_version, is_dev_version, is_embedded

logger = logging.getLogger(__name__)


def detect_installed_version() -> str | None:
    """Return the ha-mcp version installed ON DISK right now."""
    try:
        importlib.invalidate_caches()
        return get_version()
    except Exception as e:  # noqa: BLE001
        logger.info("Installed-version probe failed: %s", e)
        return None


def version_mismatch(installed: str | None) -> bool:
    """True when ``installed`` is not the version this process imported.

    A failed probe (None) is no mismatch; reports show it as unverified.
    """
    return bool(installed and installed != __version__)


def running_version_report() -> tuple[dict[str, Any], list[str]]:
    """``info``'s version fields, and a restart hint when the disk is newer.

    ``server_version`` is the running one. The hint names the restart the
    in-process component's own staleness warning names.
    """
    installed = detect_installed_version()
    fields = {
        "server_version": __version__,
        "installed_version": installed,
        "version_mismatch": version_mismatch(installed),
        "is_dev_build": is_dev_version(__version__),
    }
    if not fields["version_mismatch"]:
        return fields, []
    restart = "Home Assistant" if is_embedded() else "the server"
    return fields, [
        f"Running {__version__}, but {installed} is installed; restart "
        f"{restart} to finish applying the update."
    ]
