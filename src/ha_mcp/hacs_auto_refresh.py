"""Ask HACS to surface the paired component update after a server update.

HACS refreshes a *custom* repository's release data from GitHub only about
every 48 hours, and the ha_mcp_tools component ships in lockstep with each
server release — so a freshly updated server can sit for two days next to a
component update HACS has not noticed yet. The component's own nudge
(``custom_components/ha_mcp_tools/hacs_nudge.py``) closes that gap only for the
embedded server, which is where it runs; add-on, Docker, pip and stdio installs
had no trigger at all. This module is that trigger, server-side.

On startup, when the server version changed since the last recorded nudge (the
just-updated case) or a newer release is pending, it sends HACS's "Update
information" refresh (``hacs/repository/refresh``) over the existing admin
WebSocket for every INSTALLED candidate repository entry.

Fully advisory: every failure degrades to a debug log and nothing here can
fault startup. A marker file in the data dir records the state the last
completed pass ran against, so the common case — a per-conversation stdio
spawn on an unchanged version — costs one file read and no WebSocket traffic.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from ._vendor import websockets
from ._version import get_version, is_embedded
from .client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from .config import OAUTH_MODE_TOKEN, get_global_settings
from .update_check import UpdateInfo, get_update_info, is_update_check_disabled
from .utils.data_paths import get_data_dir

logger = logging.getLogger(__name__)

# The repository full_names HACS may track the ha_mcp_tools component under —
# the dedicated mirror first, the legacy main-repo path second. Values are
# duplicated from custom_components/ha_mcp_tools/const.py (HACS_MIRROR_ /
# HACS_LEGACY_REPO_FULL_NAME): the server package cannot import the component.
CANDIDATE_REPO_FULL_NAMES = (
    "homeassistant-ai/ha-mcp-integration",
    "homeassistant-ai/ha-mcp",
)

# Marker file (in the ha-mcp data dir) recording the state the last completed
# nudge ran against. Written only on a completed pass, so a failed pass (HA
# still booting, HACS not loaded yet) is retried on the next startup. One file
# PER Home Assistant target: server configs sharing a data dir but pointing at
# different instances must neither suppress each other's nudge nor invalidate
# each other's marker on every alternating spawn (review finding).
MARKER_FILENAME_PREFIX = "hacs_refresh_marker"

# The add-on can start before HA Core finishes booting (and before HACS is
# loaded), so the first attempts may fail; spread retries over ~8 minutes,
# then give up until the next startup (the unwritten marker retries then).
RETRY_DELAYS = (30.0, 60.0, 120.0, 300.0)


def _marker_path(ha_url: str) -> Path:
    digest = hashlib.sha256(ha_url.rstrip("/").encode()).hexdigest()[:12]
    return get_data_dir() / f"{MARKER_FILENAME_PREFIX}_{digest}.json"


def _read_marker(ha_url: str) -> dict[str, Any] | None:
    """Return the last completed pass's marker, or None when there isn't one."""
    path = _marker_path(ha_url)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as err:
        logger.debug("Ignoring unreadable HACS refresh marker %s: %s", path, err)
        return None
    return payload if isinstance(payload, dict) else None


def _write_marker(ha_url: str, marker: dict[str, Any]) -> None:
    """Record the state this pass ran against; an unwritable dir just retries."""
    path = _marker_path(ha_url)
    try:
        path.write_text(json.dumps(marker), encoding="utf-8")
    except OSError as err:
        logger.debug("Could not write HACS refresh marker %s: %s", path, err)


def _nudge_due(
    current_version: str,
    info: UpdateInfo | None,
    marker: dict[str, Any] | None,
) -> bool:
    """Decide whether this startup should ask HACS for a refresh.

    Instance scoping lives in the marker PATH (one file per HA target), so a
    marker handed in here always describes the current instance.
    """
    if marker is None:
        # First run ever, or no pass has ever completed for this HA target.
        return True
    if marker.get("server_version") != current_version:
        # The server was just updated, and the paired component release is
        # exactly what HACS needs to surface.
        return True
    # A release appeared while this build sat idle — surface its component side.
    return bool(
        info is not None
        and info.update_available
        and marker.get("latest") != info.latest
    )


async def _refresh_installed_candidates() -> dict[str, Any] | None:
    """Refresh every installed candidate repository; one attempt, no retries.

    Returns the pass result (``hacs`` presence plus the full_names refreshed),
    or None when the outcome could not be determined.
    """
    from .client.websocket_client import get_websocket_client
    from .tools.hacs_registration import send_hacs_repository_refresh

    ws_client = await get_websocket_client()
    try:
        response = await ws_client.send_command("hacs/repositories/list")
    except HomeAssistantCommandError as err:
        # HACS not installed at all — HA rejects its commands as unknown. Same
        # detection as ``_assert_hacs_available`` in tools/tools_hacs.py. Any
        # other command failure is a real error: let the retry loop see it.
        if err.code == "unknown_command" or "unknown command" in str(err).lower():
            return {"hacs": "absent", "refreshed": []}
        raise

    wanted = {name.lower() for name in CANDIDATE_REPO_FULL_NAMES}
    refreshed: list[str] = []
    attempted = 0
    for repo in response.get("result", []):
        full_name = (repo.get("full_name") or "").lower()
        # HACS keeps a record for every ADDED repo but only creates an update
        # entity for downloaded ones, so refreshing an uninstalled record
        # lights up nothing.
        if full_name not in wanted or not repo.get("installed"):
            continue
        attempted += 1
        try:
            await send_hacs_repository_refresh(ws_client, str(repo["id"]))
        except Exception as err:
            # Both entries read installed only when someone downloaded the
            # mirror without removing the legacy record — HACS guards against
            # adding the same repository twice but not against two
            # repositories sharing a domain. Rare, but one failing must not
            # cost the other its refresh.
            logger.debug("HACS refresh failed for %s: %s", full_name, err)
            continue
        refreshed.append(full_name)
    if attempted and not refreshed:
        # Every attempted refresh failed — the pass is undetermined, not
        # complete. Returning a result here would write the marker and cancel
        # the retry schedule and the next startup's pass (review finding).
        return None
    return {"hacs": "present", "refreshed": refreshed}


async def _refresh_with_retries() -> dict[str, Any] | None:
    """Run the refresh pass, retrying transport failures over ``RETRY_DELAYS``."""
    absent_result: dict[str, Any] | None = None
    for delay in (0.0, *RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        try:
            result = await _refresh_installed_candidates()
        except (
            HomeAssistantConnectionError,
            HomeAssistantCommandError,
            HomeAssistantCommandTimeout,
            # send_command deliberately re-raises the ORIGINAL exception when
            # the socket closes mid-send (ambiguous-write contract), so raw
            # websocket closures reach this loop unwrapped (review finding).
            websockets.exceptions.WebSocketException,
            OSError,
        ) as err:
            logger.debug("HACS repository refresh attempt failed: %s", err)
            continue
        if result is None:
            continue
        if result["hacs"] == "absent":
            # ``unknown_command`` also happens while HA is still booting,
            # before HACS registers its WS handlers — indistinguishable from
            # no HACS at all. Keep retrying; record absence only once the
            # schedule is exhausted (review finding).
            absent_result = result
            continue
        return result
    return absent_result


async def maybe_refresh_hacs_after_update() -> None:
    """Nudge HACS once per startup when the update picture changed."""
    try:
        if is_embedded():
            # The component's own hacs_nudge already covers embedded installs;
            # running both would double HACS's GitHub fetches.
            return
        if is_update_check_disabled():
            return

        settings = get_global_settings()
        if settings.homeassistant_token == OAUTH_MODE_TOKEN:
            # Per-user OAuth mode holds no server-level HA credential — the
            # nudge would only burn its retry schedule on auth failures.
            return
        ha_url = settings.homeassistant_url

        current = get_version()
        # Normally a cache hit — the startup banner warms the lru_cache — but
        # offloaded anyway so a cold call can never touch the event loop.
        info = await asyncio.to_thread(get_update_info)
        marker = await asyncio.to_thread(_read_marker, ha_url)
        if not _nudge_due(current, info, marker):
            # DEBUG, not INFO: the not-due return is the per-conversation
            # stdio hot path and must stay quiet at default levels. The line
            # exists so a DEBUG-level launcher (the HAOS add-on lane) can
            # prove scheduling even when a completed earlier pass makes every
            # later boot legitimately not due.
            logger.debug(
                "HACS auto-refresh: pass not due (marker current for server %s)",
                current,
            )
            return

        # The one unconditional line a due pass emits BEFORE any WebSocket
        # work: it proves the launcher scheduled the nudge even where HACS
        # is absent and the pass ends silently — the observable the HAOS
        # add-on lane greps for, and the line whose absence exposed the
        # launcher gap this module's lifespan wiring closed.
        logger.info(
            "HACS auto-refresh: startup pass due (server %s); "
            "asking HACS for repository state",
            current,
        )

        result = await _refresh_with_retries()
        if result is None:
            logger.debug(
                "Could not reach HACS to refresh the component repository; "
                "giving up until next startup"
            )
            return

        await asyncio.to_thread(
            _write_marker,
            ha_url,
            {
                "server_version": current,
                "latest": info.latest if info else None,
                "hacs": result["hacs"],
            },
        )
        if result["refreshed"]:
            logger.info(
                "Asked HACS to refresh component repository info: %s",
                ", ".join(result["refreshed"]),
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("HACS auto-refresh nudge skipped", exc_info=True)


@asynccontextmanager
async def hacs_refresh_lifespan(_server: Any) -> AsyncIterator[dict[str, Any]]:
    """Schedule the startup nudge for the lifetime of any server run.

    Attached as the FastMCP ``lifespan`` so it runs on EVERY launcher —
    stdio, the HTTP CLI entry points, and the add-on's ``start.py``, which
    calls ``mcp.run()`` directly and never passes through ``__main__``'s
    ``_run_with_shutdown`` (the wiring this replaces; the add-on gap was
    found live, not by CI, because the e2e suites launch via the CLI
    entry points).
    """
    task = asyncio.create_task(maybe_refresh_hacs_after_update())
    try:
        yield {}
    finally:
        # The nudge may be mid-retry-sleep; a cancelled task must not
        # stall shutdown. maybe_refresh_hacs_after_update re-raises
        # CancelledError by design, so this await returns promptly.
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
