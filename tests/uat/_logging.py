"""Shared logging setup for UAT entry points."""

from __future__ import annotations

import logging
import sys


def configure_cli_logging() -> None:
    """Silence third-party INFO chatter; keep our uat.* trace visible.

    The ``uat`` logger gets its own stderr handler. Importing ha_mcp
    installs a handler on the root logger, which turns ``basicConfig`` into
    a no-op, so relying on the root handler would drop the ``[tool]`` trace
    and error lines that the BAT runner reads from stderr.
    """
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    uat_logger = logging.getLogger("uat")
    uat_logger.setLevel(logging.INFO)
    if not any(getattr(h, "_uat_cli", False) for h in uat_logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._uat_cli = True  # type: ignore[attr-defined]
        uat_logger.addHandler(handler)
    uat_logger.propagate = False
