"""Shared logging setup for UAT entry points."""

from __future__ import annotations

import logging


def configure_cli_logging() -> None:
    """Silence third-party INFO chatter; keep our uat.* trace visible."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("uat").setLevel(logging.INFO)
