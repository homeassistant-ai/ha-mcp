"""Self-update notifier for ha-mcp.

An operator can sit on an old build without ever knowing. This does a
fail-silent check, holds the result in memory for the process, and surfaces a
newer release via a startup log banner and the ``ha_get_overview`` /
``ha_get_system_health`` / ``ha_manage_updates`` tool fields.

The comparison reference depends on how ha-mcp is deployed, because the running
version only means something against the matching source:

* **pip / Docker / stdio** — the running version IS a PyPI version, so it is
  compared against PyPI: stable installs against ``ha-mcp``, dev installs
  (``.dev``) against ``ha-mcp-dev``.
* **HA add-on (stable AND dev)** — the add-on is built from source and updated
  through the Supervisor add-on store, so its ``HA_MCP_BUILD_VERSION`` is on the
  add-on's own counter, NOT PyPI's. The reference is therefore the Supervisor
  add-on store (``GET /addons/self/info`` → ``version`` / ``version_latest`` /
  ``update_available``), which is the same counter — so the dev add-on, like
  every other deployment, correctly says "you're on X, Y is out."

The check runs once per process — memoized in memory, no disk, no throttle. For
stdio that is once per session (the server is spawned per conversation); for a
long-running Docker/web server or the add-on, once at boot. It is a no-op for the
``unknown`` version (PyPI path) and the ``HA_MCP_DISABLE_UPDATE_CHECK`` opt-out.
Every network call is best-effort: any failure yields "no update info" rather
than raising, so callers never need to guard it.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from dataclasses import dataclass
from typing import Any, TypedDict

import httpx
from packaging.version import InvalidVersion, Version

from ._version import (
    get_supervisor_base_url,
    get_version,
    is_dev_version,
    is_running_in_addon,
)
from .stdio_settings_sidecar import _TRUTHY  # shared HA_MCP_DISABLE_* truthy set

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
    # Reuse the shared _TRUTHY set so HA_MCP_DISABLE_UPDATE_CHECK parses
    # identically to the sibling HA_MCP_DISABLE_SETTINGS_UI flag: only a truthy
    # value disables; 0/false/no/off/blank (and anything unrecognized) keep the
    # check enabled, so a user who sets =0 to "keep it on" isn't surprised.
    return os.environ.get(DISABLE_ENV, "").strip().lower() in _TRUTHY


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


def _fetch_supervisor_addon_info() -> dict[str, Any] | None:
    """Return this add-on's Supervisor info dict, or None on any failure.

    GET ``/addons/self/info`` carries ``version`` (installed), ``version_latest``
    (the add-on store's latest), and ``update_available`` — all on the add-on's
    own version counter. Sync + fail-silent, mirroring ``_fetch_latest_from_pypi``.
    """
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        resp = httpx.get(
            f"{get_supervisor_base_url()}/addons/self/info",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        body = resp.json()
        # Supervisor REST envelope is {"result": "ok", "data": {...}}; some mocks
        # return the data dict directly — handle both (mirrors settings_ui).
        data = body.get("data") if isinstance(body, dict) and "data" in body else body
        return data if isinstance(data, dict) else None
    except (httpx.HTTPError, ValueError, TypeError) as err:
        logger.debug("ha-mcp update check skipped (Supervisor fetch failed: %s)", err)
        return None


def _resolve_from_supervisor() -> UpdateInfo | None:
    """Build UpdateInfo for the add-on from the Supervisor add-on store."""
    info = _fetch_supervisor_addon_info()
    if info is None:
        return None
    current = info.get("version")
    latest = info.get("version_latest")
    if not isinstance(current, str) or not isinstance(latest, str):
        return None
    return UpdateInfo(
        current=current,
        latest=latest,
        update_available=bool(info.get("update_available")),
    )


def _resolve_update_info() -> UpdateInfo | None:
    """Update-check logic; see ``get_update_info`` for the memo + never-raises wrapper."""
    if _is_disabled():
        return None
    # In the add-on (stable OR dev), the authoritative reference is the
    # Supervisor add-on store, which tracks the SAME version counter as the
    # installed add-on. PyPI is the wrong reference there: the add-on builds
    # ha-mcp from source on its own cadence (HA_MCP_BUILD_VERSION), unrelated to
    # the ha-mcp/ha-mcp-dev PyPI counters — comparing them yields false
    # positives/negatives. Non-add-on deployments (pip / Docker / stdio) report a
    # real PyPI version and take the PyPI path below.
    if is_running_in_addon():
        return _resolve_from_supervisor()
    current = get_version()
    if current == "unknown":
        return None
    # A dev install (``.dev`` version) tracks the renamed ``ha-mcp-dev`` package;
    # a stable install tracks ``ha-mcp``.
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


class UpdateField(TypedDict):
    """The ``ha_mcp_update`` object embedded in status-tool responses."""

    current: str
    latest: str
    update_available: bool


async def get_update_field() -> UpdateField | None:
    """Return an embeddable self-update dict for tool responses, or None.

    Off-loads the (memoized, networks-at-most-once-per-process) check to a thread
    so it never blocks the event loop, and shapes the result for embedding under
    an ``ha_mcp_update`` key. Never raises — a hiccup yields None (the tool omits
    the field) and is logged at debug. Shared by every status tool that surfaces
    the notice (``ha_get_overview`` / ``ha_get_system_health`` / ``ha_manage_updates``)
    so the shaping and event-loop offload live in one place.
    """
    try:
        # Once the lru_cache is warm (normally at startup), the result is a
        # sub-microsecond cache hit, so call it directly. Only the cold first
        # call — which may hit PyPI — is offloaded to a thread so it can't block
        # the event loop.
        if get_update_info.cache_info().currsize > 0:
            info = get_update_info()
        else:
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
    if is_running_in_addon():
        # Add-on users update through the Supervisor UI, not pip/docker.
        return "Update from Settings -> Add-ons -> Home Assistant MCP Server."
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
