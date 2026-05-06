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

from .._version import is_running_in_addon

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def get_data_dir() -> Path:
    """Return a writable directory for ha-mcp persistent data (memoized).

    Resolution order:

    1. ``HA_MCP_CONFIG_DIR`` env var — explicit override, e.g. for hardened
       Docker setups bind-mounting a writable volume into a
       ``read_only: true`` container.
    2. ``/data`` — Home Assistant add-on (``SUPERVISOR_TOKEN`` set; writable
       supervisor data dir).
    3. ``~/.ha-mcp`` — standard install. Skipped when ``HA_MCP_CONFIG_DIR``
       was set but failed: an explicit override means "use this exact
       location", and silently writing to ``$HOME`` instead would surprise
       users who chose the override deliberately.
    4. ``<tempdir>/ha-mcp`` — last-resort fallback when the previously
       chosen step fails (read-only filesystem; ``HOME`` unset so
       ``Path.home()`` resolves to ``/``; or ``HA_MCP_CONFIG_DIR`` set but
       its mkdir raises). Loses persistence across restarts but lets the
       server start; users wanting persistence should set
       ``HA_MCP_CONFIG_DIR`` to a writable path.

    Memoized so the fallback warning typically emits once at startup
    rather than on every save/load HTTP request. ``lru_cache`` serializes
    its internal dict but does not serialize the wrapped call when the
    cache is empty, so two threads racing on first access (e.g.
    ``UsageLogger.__init__`` from a worker thread plus a settings UI HTTP
    handler) may each run ``_resolve_data_dir`` once and emit the warning
    twice. The mkdir calls are idempotent, so this is cosmetic.
    Tests reset via ``get_data_dir.cache_clear()``.
    """
    return _resolve_data_dir()


def _resolve_data_dir() -> Path:
    """Resolve the data directory (uncached); see ``get_data_dir`` for priority."""
    # ``.strip()``: ``HA_MCP_CONFIG_DIR="   "`` is truthy and ``Path("   ")``
    # resolves cwd-relative, which would mkdir a literal whitespace-named
    # directory next to whatever cwd happens to be at startup.
    config_dir_env = os.environ.get("HA_MCP_CONFIG_DIR", "").strip()
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

    if is_running_in_addon():
        addon_dir = Path("/data")
        try:
            addon_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(
                "/data is not writable in add-on mode (%s: %s); "
                "falling back. Set HA_MCP_CONFIG_DIR to override.",
                type(e).__name__,
                e,
            )
            if preferred is None:
                preferred = addon_dir
        else:
            # Honor an explicit HA_MCP_CONFIG_DIR override even in add-on
            # mode: if the user set it and its mkdir failed (preferred is
            # not None), fall through to the tmpdir fallback rather than
            # silently writing to /data — they chose the override
            # deliberately.
            if preferred is None:
                return addon_dir

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
        # Even the tmpdir is unwritable. Return the path anyway: callers
        # that wrap writes in try/except OSError can degrade gracefully
        # (no persistence, but the server still starts). ``error`` rather
        # than ``warning`` because persistence is silently disabled — the
        # supervisor log viewer surfaces errors more prominently.
        logger.error(
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
