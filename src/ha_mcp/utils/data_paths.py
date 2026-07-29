"""Resolve a writable directory for ha-mcp persistent data.

Single source of truth for "where does ha-mcp write its files?" — used
by both ``settings_ui`` (tool config) and ``usage_logger`` (rolling
JSONL).
"""

from __future__ import annotations

import contextlib
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
       chosen step cannot be created *or written to* (read-only filesystem;
       ``HOME`` unset so ``Path.home()`` resolves to ``/``; a Docker volume
       whose mount point ended up owned by another user; or
       ``HA_MCP_CONFIG_DIR`` set but unusable). Loses persistence across
       restarts but lets the server start; users wanting persistence should
       set ``HA_MCP_CONFIG_DIR`` to a writable path.

    Each candidate is probed with a real write, not just a ``mkdir`` — see
    :func:`_try_write` for why existence alone is not enough.

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


def _try_mkdir(path: Path) -> OSError | None:
    """Create ``path`` and confirm it is writable.

    Returns the ``OSError`` on failure, else ``None``.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return e
    return _try_write(path)


def _try_write(path: Path) -> OSError | None:
    """Confirm ``path`` actually accepts writes; return the ``OSError``, else ``None``.

    ``Path.mkdir(exist_ok=True)`` reports success for a directory that already
    exists no matter who owns it or whether the filesystem is read-only — it
    only re-stats the path. So "mkdir worked" does not mean "we can write
    here", and Docker deployments live entirely inside that gap: the mount
    point always exists by the time Python runs, because the image ships it
    and Docker creates it when mounting a volume. Without this probe a
    root-owned volume, a ``read_only: true`` filesystem or a ``--user``
    override would each resolve as a perfectly good data dir and the fallback
    warning below would never fire — persistence would just vanish with no
    startup signal, which is the failure issue #2078 reported.

    Probes by creating a real file rather than calling ``os.access``.
    ``access(2)`` answers for the real UID rather than the effective one, and
    it reports success for a process holding ``CAP_DAC_OVERRIDE`` (root in a
    default container) whose writes a later ``--user`` drop would reject. It
    does catch a read-only mount — Linux returns ``EROFS`` for ``W_OK`` — so
    that is not the reason to avoid it. ``mkstemp`` is chosen because it
    exercises the same syscalls the actual writes use (see
    ``settings_ui._persistence``), which no permission query can stand in for.
    """
    try:
        fd, name = tempfile.mkstemp(prefix=".ha-mcp-write-probe-", dir=path)
    except OSError as e:
        return e
    os.close(fd)
    with contextlib.suppress(OSError):
        os.unlink(name)
    return None


def _prepare_fallback(preferred: Path | None) -> Path:
    """Return the last-resort ``<tempdir>/ha-mcp``, logging why ``preferred`` was rejected."""
    fallback = Path(tempfile.gettempdir()) / "ha-mcp"
    err = _try_mkdir(fallback)
    if err is not None:
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
            type(err).__name__,
            err,
        )
    else:
        logger.warning(
            "Cannot write ha-mcp data to %s (read-only filesystem, HOME unset, "
            "or the directory is owned by another user — a Docker volume "
            "mounted there must be writable by the container's user). "
            "Falling back to %s — data will NOT persist across restarts. "
            "Set HA_MCP_CONFIG_DIR to a writable path for persistence.",
            preferred,
            fallback,
        )
    return fallback


def _resolve_data_dir() -> Path:
    """Resolve the data directory (uncached); see ``get_data_dir`` for priority."""
    # ``.strip()``: ``HA_MCP_CONFIG_DIR="   "`` is truthy and ``Path("   ")``
    # resolves cwd-relative, which would mkdir a literal whitespace-named
    # directory next to whatever cwd happens to be at startup.
    config_dir_env = os.environ.get("HA_MCP_CONFIG_DIR", "").strip()
    preferred: Path | None = None
    if config_dir_env:
        custom_dir = Path(config_dir_env)
        err = _try_mkdir(custom_dir)
        if err is None:
            return custom_dir
        logger.warning(
            "HA_MCP_CONFIG_DIR=%s is not usable — it must exist or be "
            "creatable AND be writable by this process (%s: %s). It will NOT "
            "be read from either; falling back to a tmpdir.",
            custom_dir,
            type(err).__name__,
            err,
        )
        preferred = custom_dir

    if is_running_in_addon():
        addon_dir = Path("/data")
        err = _try_mkdir(addon_dir)
        if err is not None:
            logger.warning(
                "/data is not writable in add-on mode (%s: %s); "
                "falling back. Set HA_MCP_CONFIG_DIR to override.",
                type(err).__name__,
                err,
            )
            if preferred is None:
                preferred = addon_dir
        # Honor an explicit HA_MCP_CONFIG_DIR override even in add-on mode:
        # if the user set it and its mkdir failed (preferred is not None),
        # fall through to the tmpdir fallback rather than silently writing
        # to /data — they chose the override deliberately.
        elif preferred is None:
            return addon_dir

    if preferred is None:
        home_dir = Path.home() / ".ha-mcp"
        if _try_mkdir(home_dir) is None:
            return home_dir
        preferred = home_dir

    return _prepare_fallback(preferred)
