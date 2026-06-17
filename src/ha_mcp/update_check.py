"""Self-update notifier for standalone ha-mcp deployments.

The HA add-on is kept current by the Supervisor, but the standalone channels
(pip / Docker / stdio) have no auto-update — an operator can sit on an old build
without ever knowing. This does a fail-silent PyPI check, holds the result in
memory for the process, and surfaces a newer release via a startup log banner
and the ``ha_get_overview`` / ``ha_get_system_health`` / ``ha_get_updates`` tool
fields.

Both channels are covered, matching the HA add-on (which surfaces every dev
update): a stable install (the ``ha-mcp`` package) is compared against the latest
stable on PyPI; a dev install (``ha-mcp-dev``, a ``.dev`` version) against the
latest dev build.

The check runs once per process — memoized in memory, no disk, no throttle. For
stdio that is once per session (the server is spawned per conversation); for a
long-running Docker/web server, once at boot. It is suppressed only for the
``unknown`` version (nothing to compare) and the ``HA_MCP_DISABLE_UPDATE_CHECK``
opt-out. Separately, only the startup *banner* is suppressed under the add-on
(the Supervisor already prompts there) — the tool fields still surface it, for a
user who missed the prompt. Every network call is best-effort: any failure
yields "no update info" rather than raising, so callers never need to guard it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from dataclasses import dataclass

import httpx
from packaging.version import InvalidVersion, Version

from ._version import get_version, is_dev_version

logger = logging.getLogger(__name__)

DISABLE_ENV = "HA_MCP_DISABLE_UPDATE_CHECK"
_PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"
_STABLE_PACKAGE = "ha-mcp"
_DEV_PACKAGE = "ha-mcp-dev"
# Tight timeout so the once-per-process check can never add more than a blip of
# latency to a cold stdio spawn.
_HTTP_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class UpdateInfo:
    """Result of a self-update check.

    ``update_available`` is the field callers branch on; ``current`` and
    ``latest`` are carried so a banner or tool field can show both versions.
    """

    current: str
    latest: str
    update_available: bool


def _is_disabled() -> bool:
    return bool(os.environ.get(DISABLE_ENV))


def _is_newer(latest: str, current: str) -> bool:
    """Return True only when ``latest`` is a strictly higher PEP 440 release.

    ``packaging.version`` orders dev/pre/post correctly — e.g.
    ``7.8.0.dev714 < 7.8.0.dev720 < 7.8.0`` and ``7.8.0 < 7.9.0`` — so this works
    for both the stable and dev channels. An unparseable version on either side
    reads as "can't compare" → not newer (never a bogus banner).
    """
    try:
        return Version(latest) > Version(current)
    except InvalidVersion:
        return False


def _fetch_latest_from_pypi(package: str) -> str | None:
    """Fetch ``package``'s latest published version from PyPI, or None on failure."""
    try:
        # follow_redirects=True mirrors the sibling fetch in tools_updates.py and
        # survives any future PyPI redirect (the JSON API serves 200 directly today).
        resp = httpx.get(
            _PYPI_JSON_URL.format(package=package),
            timeout=_HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
        resp.raise_for_status()
        version = resp.json()["info"]["version"]
        return version if isinstance(version, str) else None
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as err:
        logger.debug("ha-mcp update check skipped (PyPI fetch failed: %s)", err)
        return None


def _resolve_update_info() -> UpdateInfo | None:
    """Update-check logic; see ``get_update_info`` for the memo + never-raises wrapper."""
    if _is_disabled():
        return None
    current = get_version()
    if current == "unknown":
        return None
    # A dev install (``.dev`` version) tracks the renamed ``ha-mcp-dev`` package;
    # a stable install tracks ``ha-mcp``. Compare like-for-like so dev users get
    # newer-dev-build parity with the add-on's dev channel.
    package = _DEV_PACKAGE if is_dev_version(current) else _STABLE_PACKAGE
    latest = _fetch_latest_from_pypi(package)
    if latest is None:
        return None
    return UpdateInfo(
        current=current,
        latest=latest,
        update_available=_is_newer(latest, current),
    )


@functools.lru_cache(maxsize=1)
def get_update_info() -> UpdateInfo | None:
    """Return self-update info for this process, or None when no check applies.

    Memoized in memory (``lru_cache``) so the network check runs once per process
    — the startup banner warms it and the tool fields reuse it without re-hitting
    PyPI. No disk, no throttle: a long-running server reflects its boot-time check
    until restart, which is fine (those operators aren't watching startup logs).
    Never raises — an unexpected failure degrades to None so the unguarded
    startup-banner call can't break startup. Tests reset via
    ``get_update_info.cache_clear()``.
    """
    try:
        return _resolve_update_info()
    except Exception as err:  # pragma: no cover - contract backstop
        logger.debug("ha-mcp update check skipped (unexpected error: %s)", err)
        return None


async def get_update_field() -> dict[str, str | bool] | None:
    """Return an embeddable self-update dict for tool responses, or None.

    Off-loads the (memoized, networks-at-most-once-per-process) check to a thread
    so it never blocks the event loop, and shapes the result for embedding under
    an ``ha_mcp_update`` key. Never raises — a hiccup yields None (the tool omits
    the field) and is logged at debug. Shared by every status tool that surfaces
    the notice (``ha_get_overview`` / ``ha_get_system_health`` / ``ha_get_updates``)
    so the shaping and event-loop offload live in one place.
    """
    try:
        info = await asyncio.to_thread(get_update_info)
    except Exception as err:  # pragma: no cover - defensive
        logger.debug("ha-mcp self-update check skipped: %s", err)
        return None
    if info is None:
        return None
    return {
        "current": info.current,
        "latest": info.latest,
        "update_available": info.update_available,
    }


def _running_in_docker() -> bool:
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def update_command_hint(current: str) -> str:
    """Return a deployment- and channel-aware one-liner for how to upgrade."""
    dev = is_dev_version(current)
    if _running_in_docker():
        # Dev images are tagged :dev (rolling); :stable is the stable channel.
        tag = "dev" if dev else "stable"
        return (
            f"Pull the new image: docker pull "
            f"ghcr.io/homeassistant-ai/ha-mcp:{tag} (then restart)."
        )
    package = _DEV_PACKAGE if dev else _STABLE_PACKAGE
    return f"Upgrade with: pip install -U {package}."
