"""Resolve a writable directory for ha-mcp persistent data.

Single source of truth for "where does ha-mcp write its files?" — used
by both ``settings_ui`` (tool config) and ``usage_logger`` (rolling
JSONL).
"""

from __future__ import annotations

import functools
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def _is_addon() -> bool:
    """Return True when running inside the Home Assistant add-on container.

    Mirrors the convention in ``settings_ui.py`` of treating
    ``SUPERVISOR_TOKEN`` as the add-on detector — more reliable than
    checking for ``/data`` because some Docker setups have a ``/data``
    directory that isn't the supervisor data dir.
    """
    return bool(os.environ.get("SUPERVISOR_TOKEN"))


@functools.lru_cache(maxsize=1)
def get_data_dir() -> Path:
    """Return a writable directory for ha-mcp persistent data (memoized).

    Priority:

    1. ``HA_MCP_CONFIG_DIR`` env var — explicit override, e.g. for hardened
       Docker setups bind-mounting a writable volume into a
       ``read_only: true`` container.
    2. ``/data`` — Home Assistant add-on (writable supervisor data dir).
    3. ``~/.ha-mcp`` — standard.
    4. ``<tempdir>/ha-mcp`` — last-resort fallback when (1) and (3) fail
       (read-only filesystem, or ``HOME`` unset so ``Path.home()`` resolves
       to ``/``). Loses persistence across restarts but lets the server
       start; users wanting persistence should set ``HA_MCP_CONFIG_DIR``.

    Memoized so the fallback warning emits once at startup rather than on
    every save/load HTTP request. Tests reset via
    ``get_data_dir.cache_clear()``.
    """
    return _resolve_data_dir()


def _resolve_data_dir() -> Path:
    """Resolve the data directory (uncached); see ``get_data_dir`` for priority."""
    config_dir_env = os.environ.get("HA_MCP_CONFIG_DIR")
    preferred: Path | None = None
    if config_dir_env:
        custom_dir = Path(config_dir_env)
        try:
            custom_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(
                "HA_MCP_CONFIG_DIR=%s could not be prepared (%s: %s); "
                "falling back to a tmpdir.",
                custom_dir,
                type(e).__name__,
                e,
            )
            preferred = custom_dir
        else:
            return custom_dir

    if _is_addon():
        return Path("/data")

    if preferred is None:
        home_dir = Path.home() / ".ha-mcp"
        try:
            home_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            preferred = home_dir
        else:
            return home_dir

    fallback = Path(tempfile.gettempdir()) / "ha-mcp"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Even the tmpdir is unwritable. Return the path anyway so the
        # caller's own try/except (save_tool_config wraps writes in
        # try/except OSError; usage_logger disables itself) can degrade
        # gracefully.
        logger.warning(
            "Cannot write ha-mcp data to %s or fallback %s (%s: %s); "
            "persistence is disabled. "
            "Set HA_MCP_CONFIG_DIR to a writable path for persistence.",
            preferred,
            fallback,
            type(e).__name__,
            e,
        )
    else:
        logger.warning(
            "Cannot write ha-mcp data to %s (read-only filesystem or HOME unset). "
            "Falling back to %s — data will NOT persist across restarts. "
            "Set HA_MCP_CONFIG_DIR to a writable path for persistence.",
            preferred,
            fallback,
        )
    return fallback
