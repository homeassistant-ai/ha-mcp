"""
Backup and restore tools for Home Assistant MCP Server.

Provides the polymorphic ``ha_manage_backup`` tool, which handles both:

* **Full HA snapshots** (``scope="snapshot"``) — the original
  ``ha_backup_create`` / ``ha_backup_restore`` functionality. Heavy
  tarball creation via HA's native backup integration; restore restarts
  HA. Last-resort recovery.
* **Per-edit auto-backups** (``scope="edits"``) — list / view / restore
  / delete operations against the per-entity snapshots produced by the
  ``@with_auto_backup`` decorator (#1288). Lightweight, no HA restart.

The merge consolidates the previous two tools so the LLM cannot
accidentally route "restore my automation" through the heavyweight full
HA restore path. Each call must explicitly pick its scope.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal, NoReturn, cast

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..backup_manager import (
    LEGACY_PREFIX,
    MandatoryBackupError,
    get_backup_manager,
)
from ..client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantError,
)
from ..client.websocket_client import HomeAssistantWebSocketClient, get_websocket_client
from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import (
    exception_to_structured_error,
    get_connected_ws_client,
    log_tool_usage,
    raise_tool_error,
    safe_progress,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Poll window for full HA backups. HA's own frontend imposes no timeout at
# all on backup creation — it subscribes to `backup/subscribe_events` and
# waits however long the job takes. Our poll-based design still needs a
# finite bound per tool call, so this mirrors _SAFETY_BACKUP_MAX_WAIT_S (the
# codebase's existing "generous but bounded" ceiling) rather than inventing a
# third number.
#
# Two distinct problems, both fixed in the same PR, by different mechanisms:
# the client-side abort reported in #1861 (an MCP client that received no
# progress notifications for 300s concluded the connection was dead) is
# fixed by the ctx.report_progress heartbeats in _poll_backup_completion,
# not by this constant. Raising it from 300 to 1800 instead fixes *our own*
# poll loop giving up on a legitimately >5-minute backup — the job keeps
# running server-side after our loop times out, so a too-tight ceiling here
# means _poll_backup_completion raises TIMEOUT_OPERATION on a backup that
# would otherwise have succeeded. 120s before that underfit slow HA
# instances too (#1433: poll loop exited while HA was still in
# state="create_backup", the wrapper treated that as failure, retried, and
# produced duplicate backups).
_BACKUP_MAX_WAIT_S = 1800
_BACKUP_POLL_INTERVAL_S = 2
# The pre-restore safety backup is a *full* backup (include_database=True, plus
# all add-ons on Supervised), unlike the fast create_backup path. A multi-GB
# full backup on constrained hardware (~2.1 GB on a Pi 5, #1681) can run well
# past the fast-backup window, so its completion poll gets a larger budget —
# timing out here aborts the restore and the retry spawns another full backup.
_SAFETY_BACKUP_MAX_WAIT_S = 1800
# Clock-skew tolerance when filtering backup entries by date vs job-start.
_BACKUP_DATE_FILTER_TOLERANCE_S = 5
# Minimum spacing between in-loop MCP progress heartbeats during a backup
# poll (#1861: with no progress notifications at all, a client can decide
# the connection is dead and abort mid-backup — "sent no response or
# progress for 300s"). Independent of poll_interval so a tight poll loop
# doesn't flood the client with redundant notifications.
_BACKUP_PROGRESS_INTERVAL_S = 10
# backup/delete fans out to every configured agent (including cloud/remote
# ones — S3, Google Drive, WebDAV, etc.) via asyncio.gather server-side, and
# the WS response doesn't arrive until all of them finish. The client's
# default 30s wait (HomeAssistantWebSocketClient.send_command's
# _wait_timeout) can be too tight for a slow or rate-limited remote agent —
# same class of false-timeout problem this file already fixes for creation.
_BACKUP_DELETE_WAIT_S = 60.0
# WS command name is a module-local constant per the component-routing seam
# pattern (see component_api.py / component_devices.py).
WS_BACKUP_PREP = "ha_mcp_tools/backup_prep"


def _get_backup_hint_text() -> str:
    """
    Generate dynamic backup hint text based on BACKUP_HINT config.

    Returns:
        Backup hint text appropriate for the configured hint level.
    """
    import os

    # Get hint from environment directly to avoid requiring full settings
    hint = os.getenv("BACKUP_HINT", "normal").lower()

    hints = {
        "strong": "Run this backup before the FIRST modification of the day/session. This is usually not required since most operations can be rolled back (the model fetches definitions before modifying). Users with daily backups configured should use 'normal' or 'weak' instead.",
        "normal": "Run before operations that CANNOT be undone (e.g., deleting devices). If the current definition was fetched or can be fetched, this tool is usually not needed.",
        "weak": "Backups are usually not required for configuration changes since most operations can be manually undone. Only run this if specifically requested or before irreversible system operations.",
        "auto": "Run before operations that CANNOT be undone (e.g., deleting devices). If the current definition was fetched or can be fetched, this tool is usually not needed.",
    }
    return hints.get(hint, hints["normal"])


async def _get_local_backup_agent_id(
    ws_client: HomeAssistantWebSocketClient,
) -> str:
    """Discover the local backup agent_id at call time.

    HA Supervised registers ``hassio.local`` and HA Core registers
    ``backup.local`` — both have ``name: "local"``. Hardcoding either breaks
    the other deployment. We probe ``backup/agents/info`` and pick the agent
    whose name is exactly ``"local"``, preferring ``hassio.local`` if both
    happen to be registered.

    Raises ToolError if no local agent is available.
    """
    response = await ws_client.send_command("backup/agents/info")
    if not response.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Failed to enumerate backup agents",
                context={"details": response},
            )
        )

    agents = response.get("result", {}).get("agents", [])
    if not agents:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "No backup agents registered with Home Assistant",
                suggestions=[
                    "The HA backup integration may not be fully set up; "
                    "check the backup panel in Home Assistant",
                ],
            )
        )

    local_agents: list[str] = [
        a["agent_id"] for a in agents if a.get("name") == "local" and a.get("agent_id")
    ]
    # Prefer hassio.local (Supervisor) over backup.local (Core) when both exist
    for preferred in ("hassio.local", "backup.local"):
        if preferred in local_agents:
            return preferred
    if local_agents:
        return local_agents[0]

    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "No local backup agent found",
            context={
                "available_agents": [
                    a.get("agent_id") for a in agents if a.get("agent_id")
                ]
            },
            suggestions=[
                "Backup creation requires a local agent (hassio.local on "
                "Supervised, backup.local on Core); none is registered",
            ],
        )
    )


async def _get_backup_password(
    ws_client: HomeAssistantWebSocketClient,
) -> str:
    """
    Retrieve default backup password from Home Assistant configuration.

    Args:
        ws_client: Connected WebSocket client

    Returns:
        The backup password string.

    Raises:
        ToolError: If backup config cannot be retrieved or no password is configured.
    """
    backup_config = await ws_client.send_command("backup/config/info")
    if not backup_config.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Failed to retrieve backup configuration",
                context={"details": backup_config},
            )
        )

    config_data = backup_config.get("result", {}).get("config", {})
    default_password = config_data.get("create_backup", {}).get("password")

    if not default_password:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "No default backup password configured in Home Assistant",
                suggestions=[
                    "Configure automatic backups in Home Assistant settings to set a default password"
                ],
            )
        )

    return cast(str, default_password)


async def _backup_prep_via_component(
    client: HomeAssistantClient,
) -> dict[str, Any] | None:
    """One ``ha_mcp_tools/backup_prep`` read; ``None`` ⇒ run the legacy two-call path.

    Returns the component's ``{agent_ids, local_agent_id, default_password}``
    payload, replacing the sequential ``backup/agents/info`` (local-agent
    discovery, :func:`_get_local_backup_agent_id`) + ``backup/config/info``
    (default password, :func:`_get_backup_password`) WS round-trips with one
    in-process read. ``None`` on capability miss, downgrade (``unknown_command``
    → invalidate the cached caps), or command error/timeout (logged) — the
    caller falls back to the legacy sequential calls. A
    ``HomeAssistantConnectionError`` (WS down) is not caught here; it
    propagates, since the legacy path shares the same socket and would fail
    identically. Same caps-gate discipline as
    ``component_devices.fetch_device_via_component``.
    """
    caps = await get_component_caps(client)
    if not component_supports(caps, "backup_prep"):
        return None
    try:
        ws = await get_websocket_client(url=client.base_url, token=client.token)
        raw = await ws.send_command(WS_BACKUP_PREP)
    except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
        if is_unknown_command(exc):
            invalidate_caps(client)
        else:
            logger.warning("%s failed; fell back to legacy: %r", WS_BACKUP_PREP, exc)
        return None
    result = raw.get("result")
    if not isinstance(result, dict) or "local_agent_id" not in result:
        return None
    return result


def _raise_no_local_backup_agent_error(agent_ids: list[Any] | None) -> NoReturn:
    """Same "no local agent" error ``_get_local_backup_agent_id`` raises.

    Used when the ``backup_prep`` component read comes back with
    ``local_agent_id: None`` — the SAME outcome as the legacy probe finding no
    agent named ``"local"``, so both paths fail identically.
    """
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "No local backup agent found",
            context={"available_agents": [str(a) for a in (agent_ids or [])]},
            suggestions=[
                "Backup creation requires a local agent (hassio.local on "
                "Supervised, backup.local on Core); none is registered",
            ],
        )
    )


def _raise_no_default_password_error() -> NoReturn:
    """Same error ``_get_backup_password`` raises when no default password is configured."""
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "No default backup password configured in Home Assistant",
            suggestions=[
                "Configure automatic backups in Home Assistant settings to set a default password"
            ],
        )
    )


def _parse_backup_date(raw: Any) -> datetime | None:
    """Parse an HA backup `date` (ISO-8601, may use `Z` suffix) to a tz-aware
    datetime, returning None on missing/malformed input. Naive timestamps are
    treated as UTC."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _build_success_response_if_found(
    info_result: dict[str, Any],
    *,
    name: str,
    backup_job_id: str,
    agent_id: str,
    duration_seconds: int,
    job_start_ts: datetime,
) -> dict[str, Any] | None:
    """Return the canonical success-response dict, or None.

    Single source of truth for "did this backup actually complete cleanly?".
    Called from both the in-loop branch and the post-timeout final check.

    Returns None unless ALL of:

    * `result.state == "idle"` AND `result.last_action_event.state == "completed"`
      — list-membership alone is insufficient because HA registers the entry in
      `backups` before compression/encryption finish; claiming success on a
      half-written entry would let callers immediately attempt restore on a
      partial file. The state-gate enforces "fully finalized".
    * The match resolves to *this* job, not a stale prior-run entry with the
      same name (HA does not enforce unique backup names, and the post-timeout
      window is exactly when collisions are likely after a retry). Filters to
      entries with `name == name` AND `date >= job_start_ts - tolerance`,
      then within that fresh set prefers a `last_action_event.backup_id`
      match if HA exposes one, otherwise picks the newest by date.
    """
    result_block = info_result.get("result") or {}
    state = result_block.get("state")
    last_event = result_block.get("last_action_event") or {}
    if state != "idle" or last_event.get("state") != "completed":
        return None

    backups = result_block.get("backups") or []

    # Freshness gate applies uniformly — both the `last_action_event.backup_id`
    # path and the name-only fallback are constrained to entries dated
    # at-or-after the job start. Without this, a concurrent backup (e.g. UI-
    # triggered) updating `last_action_event` between our last in-loop poll
    # and the post-timeout lookup could let us match its entry as if it were
    # ours.
    tolerance = timedelta(seconds=_BACKUP_DATE_FILTER_TOLERANCE_S)
    cutoff = job_start_ts - tolerance
    fresh = [
        b
        for b in backups
        if b.get("name") == name
        and (entry_date := _parse_backup_date(b.get("date"))) is not None
        and entry_date >= cutoff
    ]
    if not fresh:
        return None

    target_id = last_event.get("backup_id")
    authoritative = (
        next((b for b in fresh if b.get("backup_id") == target_id), None)
        if target_id
        else None
    )
    match = authoritative or max(
        fresh,
        key=lambda b: _parse_backup_date(b.get("date")) or job_start_ts,
    )

    return {
        "success": True,
        "backup_id": match.get("backup_id"),
        "backup_job_id": backup_job_id,
        "name": name,
        "date": match.get("date"),
        "size_bytes": (match.get("agents") or {}).get(agent_id, {}).get("size"),
        "status": "Backup completed successfully",
        "duration_seconds": duration_seconds,
        "note": "Backup uses your Home Assistant's default backup password",
    }


async def _poll_backup_completion(
    ws_client: HomeAssistantWebSocketClient,
    name: str,
    backup_job_id: str,
    max_wait_seconds: int,
    poll_interval: int,
    agent_id: str,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Poll backup/info until the named backup completes, fails, or times out.

    ``agent_id`` is the local agent that owns this backup (e.g.
    ``hassio.local`` on Supervised, ``backup.local`` on Core); used to look
    up the per-agent size in the backup-info payload.

    On timeout, performs one final ``backup/info`` lookup before raising. If
    that lookup confirms `state=idle` + `event_state=completed` AND finds a
    backup belonging to *this* job (by ``last_action_event.backup_id`` or
    fresh date-window name-match), returns the success response with a
    ``warnings`` entry noting the late detection (#1433). Otherwise raises
    ``TIMEOUT_OPERATION`` — and if the entry exists but state still indicates
    creation in progress, surfaces ``likely_in_progress=true`` in the error
    context so callers can back off retries instead of compounding duplicates.

    ``ctx``, when supplied, receives a ``report_progress`` heartbeat before
    the first poll and at least every ``_BACKUP_PROGRESS_INTERVAL_S`` while
    waiting, so an MCP client watching for "no response or progress" doesn't
    abort the connection mid-backup (#1861).

    Raises ToolError on backup failure or final timeout with no completion.
    """
    job_start_ts = datetime.now(UTC)
    waited = 0
    next_progress_at = _BACKUP_PROGRESS_INTERVAL_S
    await safe_progress(
        ctx,
        progress=0,
        total=max_wait_seconds,
        message="waiting for backup to complete",
    )

    while waited < max_wait_seconds:
        await asyncio.sleep(poll_interval)
        waited += poll_interval
        if waited >= next_progress_at:
            await safe_progress(
                ctx,
                progress=waited,
                total=max_wait_seconds,
                message=f"waiting for backup to complete ({waited}s elapsed)",
            )
            next_progress_at = waited + _BACKUP_PROGRESS_INTERVAL_S

        info_result = await ws_client.send_command("backup/info")
        if not info_result.get("success"):
            logger.debug(
                f"backup/info returned success=False at waited={waited}s; retrying"
            )
            continue

        result_block = info_result.get("result") or {}
        last_event = result_block.get("last_action_event") or {}
        event_state = last_event.get("state")
        logger.debug(
            f"Backup state: {result_block.get('state')}, "
            f"event_state: {event_state}, waited: {waited}s"
        )

        if event_state == "failed":
            # Surface the failure event verbatim — HA's failed event
            # carries the cause (e.g. another backup already in
            # progress), and dropping it left CI failures reading
            # "Backup creation failed: Backup creation failed" with
            # nothing to diagnose.
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Backup creation failed",
                    context={
                        "backup_job_id": backup_job_id,
                        "last_action_event": last_event,
                    },
                )
            )

        response = _build_success_response_if_found(
            info_result,
            name=name,
            backup_job_id=backup_job_id,
            agent_id=agent_id,
            duration_seconds=waited,
            job_start_ts=job_start_ts,
        )
        if response is not None:
            logger.info(f"Backup completed successfully: {response['backup_id']}")
            return response
        # state=idle+completed observed but entry not yet (or not freshly) in
        # the list — same bug class as #1433 one tick earlier; continue polling.

    logger.info(
        f"Backup did not complete within {max_wait_seconds}s; "
        "performing final backup-list lookup before raising timeout"
    )
    # send_command raises on HA-side failure rather than returning
    # success=false. Treat any `HomeAssistantError` (incl. AuthError,
    # ConnectionError, CommandError) during this best-effort verification
    # as "couldn't verify"; surface the failure in the error context via
    # `verification_error` so the auth-case stops being invisible behind a
    # misleading TIMEOUT_OPERATION. Programming errors (AttributeError,
    # TypeError, KeyError) intentionally propagate.
    verification_error: str | None = None
    likely_in_progress = False
    final_info: dict[str, Any] | None = None
    try:
        final_info = await ws_client.send_command("backup/info")
    except HomeAssistantError as e:
        verification_error = repr(e)
        logger.warning(
            f"Post-timeout backup/info lookup failed ({e!r}); "
            "falling through to TIMEOUT_OPERATION"
        )

    if final_info is not None and final_info.get("success"):
        response = _build_success_response_if_found(
            final_info,
            name=name,
            backup_job_id=backup_job_id,
            agent_id=agent_id,
            duration_seconds=max_wait_seconds,
            job_start_ts=job_start_ts,
        )
        if response is not None:
            response["warnings"] = [
                f"Backup completion observed only after the {max_wait_seconds}s "
                "poll window — the operation succeeded but took longer than "
                "expected. Increase max_wait_seconds if this recurs.",
            ]
            logger.info(
                f"Backup found in post-timeout list lookup: {response['backup_id']}"
            )
            return response
        # Helper returned None. Three cases distinguishable from the raw
        # final_info: (a) backup failed in the gap between last in-loop poll
        # and the final lookup → raise SERVICE_CALL_FAILED, the failure mode
        # is known and unambiguous; (b) state still indicates creation in
        # progress → surface `likely_in_progress` so callers back off retries
        # rather than compounding duplicates; (c) state idle+completed but no
        # fresh matching entry → genuine TIMEOUT_OPERATION, nothing extra to
        # add.
        result_block = final_info.get("result") or {}
        last_event = result_block.get("last_action_event") or {}
        state = result_block.get("state")
        event_state = last_event.get("state")
        if event_state == "failed":
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Backup creation failed (observed at post-timeout lookup)",
                    context={
                        "backup_job_id": backup_job_id,
                        "name": name,
                        "last_action_event": last_event,
                    },
                )
            )
        if state != "idle":
            likely_in_progress = True

    logger.warning(f"Backup did not complete within {max_wait_seconds} seconds")
    error_context: dict[str, Any] = {"backup_job_id": backup_job_id, "name": name}
    if verification_error is not None:
        error_context["verification_error"] = verification_error
    if likely_in_progress:
        error_context["likely_in_progress"] = True

    raise_tool_error(
        create_error_response(
            ErrorCode.TIMEOUT_OPERATION,
            f"Backup creation timed out after {max_wait_seconds} seconds",
            context=error_context,
            suggestions=[
                "Backup may still be in progress. Check Home Assistant backup status."
            ],
        )
    )


async def create_backup(
    client: HomeAssistantClient,
    name: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """
    Create a fast Home Assistant backup (local only, excludes database).

    Args:
        client: Home Assistant REST client
        name: Optional backup name (auto-generated if not provided)
        ctx: Optional FastMCP context for progress heartbeats during the wait

    Returns:
        Dictionary with backup result including backup_id, status, duration, etc.
    """
    ws_client = None

    try:
        # Connect to WebSocket
        ws_client, error = await get_connected_ws_client(
            client.base_url, client.token, verify_ssl=client.verify_ssl
        )
        if error:
            raise_tool_error(
                error
                or create_error_response(
                    ErrorCode.CONNECTION_FAILED,
                    "Failed to connect to Home Assistant WebSocket for backup",
                )
            )
        ws_client = cast(HomeAssistantWebSocketClient, ws_client)

        # One component read replaces the sequential password + local-agent
        # probes when the ha_mcp_tools component supports backup_prep.
        prep = await _backup_prep_via_component(client)
        if prep is not None:
            password = prep.get("default_password")
            if not password:
                _raise_no_default_password_error()
            local_agent = prep.get("local_agent_id")
            if not local_agent:
                _raise_no_local_backup_agent_error(prep.get("agent_ids"))
        else:
            # Get backup password (raises ToolError on failure)
            password = await _get_backup_password(ws_client)

            # Discover the local backup agent at call time. HA Core registers
            # `backup.local`; HA Supervised registers `hassio.local`. Hardcoding
            # either breaks the other deployment.
            local_agent = await _get_local_backup_agent_id(ws_client)

        # Generate backup name if not provided
        if not name:
            now = datetime.now()
            name = f"MCP_Backup_{now.strftime('%Y-%m-%d_%H:%M:%S')}"

        # Addons + addon folders are Supervisor concepts — HA Core errors
        # with "Addons and folders are not supported by core backup" if we
        # ask for them. Toggle off when we detect the Core local agent.
        is_supervised = local_agent == "hassio.local"
        logger.info(
            f"Detected {'Supervised' if is_supervised else 'Core'} install "
            f"via backup agent '{local_agent}'"
        )
        backup_params = {
            "name": name,
            "password": password,
            "agent_ids": [local_agent],
            "include_homeassistant": True,
            "include_database": False,  # Fast backup
            "include_all_addons": is_supervised,
        }

        # Send backup request
        result = await ws_client.send_command("backup/generate", **backup_params)

        if not result.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    result.get("error", "Backup creation failed"),
                )
            )

        backup_job_id = result.get("result", {}).get("backup_job_id")
        logger.info(f"Backup job started: {backup_job_id}, waiting for completion...")

        return await _poll_backup_completion(
            ws_client,
            name,
            backup_job_id,
            max_wait_seconds=_BACKUP_MAX_WAIT_S,
            poll_interval=_BACKUP_POLL_INTERVAL_S,
            agent_id=local_agent,
            ctx=ctx,
        )

    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error creating backup: {e}")
        exception_to_structured_error(
            e,
            context={"tool": "create_backup"},
            suggestions=["Check Home Assistant connection and backup configuration"],
        )
        return None  # unreachable: exception_to_structured_error always raises
    finally:
        # Always disconnect WebSocket — narrow to transport errors; a
        # programming error during cleanup should still surface.
        if ws_client:
            try:
                await ws_client.disconnect()
            except (TimeoutError, OSError, ConnectionError) as err:
                logger.debug(
                    "ws disconnect (cleanup) transport error: %s: %s",
                    type(err).__name__,
                    err,
                )


async def _create_safety_backup(
    ws_client: HomeAssistantWebSocketClient,
    password: str | None,
    agent_id: str,
    ctx: Context | None = None,
) -> tuple[str | None, list[str]]:
    """Create a pre-restore safety backup and wait for it to complete.

    ``agent_id`` is the local backup agent (Supervisor's ``hassio.local`` or
    Core's ``backup.local``) discovered by the caller.

    Blocks until the safety backup finishes. HA's ``backup/generate`` returns
    on job initiation, not completion, so waiting here lets the caller issue
    ``backup/restore`` afterwards without colliding with a still-running
    backup. Returns ``(job_id, warnings)`` — ``job_id`` is None when password
    is None (backup intentionally skipped); ``warnings`` carries any late-
    completion notice from the poll so the caller can surface it before the
    destructive restore. Raises ToolError if the backup fails to start or does
    not complete in time.
    """
    if password is None:
        return None, []

    now = datetime.now()
    safety_backup_name = f"PreRestore_Safety_{now.strftime('%Y-%m-%d_%H:%M:%S')}"

    # include_all_addons is a Supervisor concept; HA Core rejects it.
    is_supervised = agent_id == "hassio.local"
    safety_backup = await ws_client.send_command(
        "backup/generate",
        name=safety_backup_name,
        password=password,
        agent_ids=[agent_id],
        include_homeassistant=True,
        include_database=True,
        include_all_addons=is_supervised,
    )

    if not safety_backup.get("success"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                safety_backup.get(
                    "error", "Failed to create safety backup before restore"
                ),
                suggestions=["Cannot proceed with restore without safety backup"],
            )
        )

    safety_backup_id = safety_backup.get("result", {}).get("backup_job_id")
    logger.info(f"Safety backup started: {safety_backup_id}, waiting for completion...")

    # Wait for the safety backup to finish before returning. HA's
    # backup/generate WS command returns as soon as the job is *initiated*,
    # not when it completes, and the backup manager rejects any new operation
    # while a backup is running ("Backup manager busy: create_backup"). If the
    # caller issued backup/restore right after this returned, it would collide
    # with the safety backup this same call just started – a self-induced
    # deadlock (#1681). Reuse the same completion poll create_backup already
    # uses rather than adding a second wait mechanism.
    poll_result = await _poll_backup_completion(
        ws_client,
        safety_backup_name,
        cast(str, safety_backup_id),
        max_wait_seconds=_SAFETY_BACKUP_MAX_WAIT_S,
        poll_interval=_BACKUP_POLL_INTERVAL_S,
        agent_id=agent_id,
        ctx=ctx,
    )
    logger.info(f"Safety backup completed: {safety_backup_id}")
    return cast(str, safety_backup_id), poll_result.get("warnings", [])


def _backup_protected(entry: dict[str, Any]) -> bool | None:
    """Whether a ``backup/info`` entry is encrypted.

    ``protected`` is a per-agent field (AgentBackupStatus), not top-level. A
    backup is encrypted as a whole, so any agent reporting it makes the backup
    protected; returns None when the ``agents`` map is absent/malformed or no
    agent reports the flag (unknown rather than "unprotected").
    """
    agents = entry.get("agents")
    if not isinstance(agents, dict):
        return None
    flags = [
        a.get("protected")
        for a in agents.values()
        if isinstance(a, dict) and a.get("protected") is not None
    ]
    return any(flags) if flags else None


async def restore_backup(
    client: HomeAssistantClient,
    backup_id: str,
    restore_database: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """
    Restore Home Assistant from a backup (DESTRUCTIVE - use with caution).

    Creates a safety backup before restore to allow rollback if needed.

    Args:
        client: Home Assistant REST client
        backup_id: Backup ID to restore
        restore_database: Whether to restore database (historical data).
            On Supervised installs this is reconciled against the target
            backup's ``database_included`` flag, which Supervisor requires to
            match when restoring Home Assistant; a warning is returned if the
            value is overridden. On Core installs the caller's value is honoured
            as-is (Core enforces no such match).
        ctx: Optional FastMCP context for progress heartbeats during the
            pre-restore safety-backup wait

    Returns:
        Dictionary with restore result including safety_backup_id, status, etc.
    """
    ws_client = None

    try:
        # Connect to WebSocket
        ws_client, error = await get_connected_ws_client(
            client.base_url, client.token, verify_ssl=client.verify_ssl
        )
        if error:
            raise_tool_error(
                error
                or create_error_response(
                    ErrorCode.CONNECTION_FAILED,
                    "Failed to connect to Home Assistant WebSocket for restore",
                )
            )
        ws_client = cast(HomeAssistantWebSocketClient, ws_client)

        # Verify backup exists
        backup_info = await ws_client.send_command("backup/info")
        if not backup_info.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    backup_info.get("error", "Failed to retrieve backup information"),
                )
            )

        backups = backup_info.get("result", {}).get("backups", [])
        matched = next((b for b in backups if b.get("backup_id") == backup_id), None)

        if matched is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Backup '{backup_id}' not found",
                    suggestions=[
                        "Inspect available snapshots in Home Assistant's "
                        "backup panel before retrying"
                    ],
                )
            )

        # One component read replaces the sequential local-agent + password
        # probes when the ha_mcp_tools component supports backup_prep.
        prep = await _backup_prep_via_component(client)
        if prep is not None:
            # Local backup agent (Supervisor's hassio.local on Supervised,
            # backup.local on Core). Used for both the safety backup and the
            # restore call below.
            local_agent = prep.get("local_agent_id")
            if not local_agent:
                _raise_no_local_backup_agent_error(prep.get("agent_ids"))

            # Create safety backup BEFORE restoring
            logger.info("Creating safety backup before restore...")
            password = prep.get("default_password")
            if not password:
                # No default password - log warning but continue (restore might still work)
                logger.warning("No default password - proceeding without safety backup")
                password = None
        else:
            # Discover the local backup agent (Supervisor's hassio.local on
            # Supervised, backup.local on Core). Used for both the safety backup
            # and the restore call below.
            local_agent = await _get_local_backup_agent_id(ws_client)

            # Create safety backup BEFORE restoring
            logger.info("Creating safety backup before restore...")
            try:
                password = await _get_backup_password(ws_client)
            except ToolError:
                # Password error - log warning but continue (restore might still work)
                logger.warning("No default password - proceeding without safety backup")
                password = None

        safety_backup_id, safety_warnings = await _create_safety_backup(
            ws_client, password, local_agent, ctx=ctx
        )

        # `backup/info` returns ManagerBackup entries: `database_included` is a
        # top-level field (inherited from BaseBackup), but `protected` is NOT —
        # it lives per-agent under the entry's `agents` map (AgentBackupStatus),
        # so a top-level `matched.get("protected")` is always None against real
        # HA. Derive it from the agents map (shared with _summarize_backup).
        target_protected = _backup_protected(matched)

        # Reconcile restore_database with the target only on Supervised. HA's
        # Supervisor raises "Restore database must match backup" when
        # restore_homeassistant is set and restore_database != the backup's
        # database_included flag (hassio/backup.py). HA Core's restore path
        # (CoreBackupReaderWriter) has no such constraint and writes the caller's
        # value verbatim, so overriding it there would silently discard an
        # explicit request for no HA-side reason. Fall back to the caller's
        # request when not Supervised or when the field is absent. Surface a
        # warning when the derived value overrides what the caller asked for so
        # the override isn't silent on a destructive op.
        is_supervised = local_agent == "hassio.local"
        target_database_included = matched.get("database_included")
        if is_supervised and target_database_included is not None:
            effective_restore_database = target_database_included
        else:
            effective_restore_database = restore_database

        # Perform restore
        restore_params: dict[str, Any] = {
            "backup_id": backup_id,
            "agent_id": local_agent,
            "restore_database": effective_restore_database,
            "restore_homeassistant": True,
            "restore_addons": [],  # Restore all addons from backup
            "restore_folders": [],  # Restore all folders from backup
        }
        # Forward the default backup password ONLY for a protected (encrypted)
        # target. `password` here is HA's default create_backup.password, which
        # is independent of whether *this* backup is encrypted: HA validates the
        # password against the target unconditionally and rejects a password on
        # an unprotected backup ("Invalid password for backup" →
        # IncorrectPasswordError). Gate on the target's per-agent `protected`
        # flag (read above) so an unprotected snapshot still restores on a
        # default-password instance. HA's backup/restore schema types `password`
        # as `str` (not `str | None`), so only include the key when we actually
        # forward one — passing None would fail voluptuous validation. Without
        # this a protected backup cannot be restored even though the HA UI,
        # which applies the stored key, succeeds (#1681).
        if password is not None and target_protected:
            restore_params["password"] = password

        result = await ws_client.send_command("backup/restore", **restore_params)

        if result.get("success"):
            # Honest note + warnings depending on whether the safety
            # backup actually landed. When ``password is None`` (default
            # password unavailable / not configured), ``_create_safety_backup``
            # returned None; telling the user "a safety backup was created"
            # in that case is user-visible misinformation.
            warnings = [
                "Home Assistant is restarting. Connection will be temporarily lost."
            ]
            # Surface any late-completion notice from the safety-backup poll —
            # a slow backup subsystem right before a destructive restore is
            # exactly the signal a caller wants to see.
            warnings.extend(safety_warnings)
            if effective_restore_database != restore_database:
                warnings.append(
                    "restore_database was adjusted to "
                    f"{effective_restore_database} to match the target backup "
                    "(Home Assistant's Supervisor requires it to match the "
                    "backup's database_included flag when restoring Home "
                    "Assistant)."
                )
            if safety_backup_id is None:
                warnings.append(
                    "No safety backup was created (the default backup "
                    "password is not set). If the restore corrupts state, "
                    "there is no automatic rollback — configure the "
                    "default password in Settings → System → Backups and "
                    "retry to get a safety net."
                )
                note = "Restore proceeding WITHOUT a safety backup. See warnings above."
            else:
                note = (
                    "A safety backup was created before restore. You can "
                    "restore from it if needed."
                )
            return {
                "success": True,
                "backup_id": backup_id,
                "status": "Restore initiated - Home Assistant will restart",
                "safety_backup_id": safety_backup_id,
                "restore_database": effective_restore_database,
                "warnings": warnings,
                "note": note,
            }
        else:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    result.get("error", "Restore operation failed"),
                    context={"backup_id": backup_id},
                )
            )

    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error restoring backup: {e}")
        exception_to_structured_error(
            e,
            context={"tool": "restore_backup", "backup_id": backup_id},
            suggestions=["Check Home Assistant connection and backup availability"],
        )
        return None  # unreachable: exception_to_structured_error always raises
    finally:
        # Always disconnect WebSocket — narrow to transport errors; a
        # programming error during cleanup should still surface.
        if ws_client:
            try:
                await ws_client.disconnect()
            except (TimeoutError, OSError, ConnectionError) as err:
                logger.debug(
                    "ws disconnect (cleanup) transport error: %s: %s",
                    type(err).__name__,
                    err,
                )
    return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable


def _summarize_backup(entry: dict[str, Any]) -> dict[str, Any]:
    """Project one HA ``backup/info`` entry to the fields a caller needs to
    identify and choose a snapshot (issue #1586).

    Size is reported per backup agent; we surface the largest reported size
    across agents. Every field is ``.get`` so a schema the running HA version
    doesn't populate yields ``None`` rather than raising.
    """
    agents = entry.get("agents") or {}
    size_bytes: int | None = None
    if isinstance(agents, dict):
        sizes: list[int] = []
        for a in agents.values():
            if isinstance(a, dict):
                size = a.get("size")
                # Accept int or float (some agents report byte counts as float);
                # bool is an int subclass but never a real size, so exclude it.
                if isinstance(size, (int, float)) and not isinstance(size, bool):
                    sizes.append(int(size))
        if sizes:
            size_bytes = max(sizes)
    return {
        "backup_id": entry.get("backup_id"),
        "name": entry.get("name"),
        "date": entry.get("date"),
        "size_bytes": size_bytes,
        # per-agent field, derived from the agents map (see _backup_protected)
        "protected": _backup_protected(entry),
        "database_included": entry.get("database_included"),
        "homeassistant_included": entry.get("homeassistant_included"),
        "homeassistant_version": entry.get("homeassistant_version"),
        "with_automatic_settings": entry.get("with_automatic_settings"),
        "agent_ids": list(agents.keys()) if isinstance(agents, dict) else [],
    }


async def list_backups(client: HomeAssistantClient, limit: int = 200) -> dict[str, Any]:
    """List the full HA snapshot tarballs known to Home Assistant (issue #1586).

    Surfaces the inventory HA already returns from its WebSocket ``backup/info``
    command — the same data ``restore_backup`` uses internally to verify a
    ``backup_id`` exists. Before this, the snapshot scope exposed only
    ``create`` and ``restore``, so a caller had no way to discover backup IDs or
    confirm a specific backup landed; they had to already know the ID. Newest
    first. Read-only: no safety backup, no restart.
    """
    try:
        # Route through the shared pooled WebSocket (issue #1813) rather than a
        # dedicated connect/auth handshake per call. ``list_backups`` issues a
        # single request/response ``backup/info`` command; the pooled client
        # owns the connection lifecycle, so there is no per-call connect or
        # disconnect. A failed command surfaces as ``success=False`` and is
        # handled by the guard below (send_websocket_message never raises for
        # WS command failures).
        info = await client.send_websocket_message({"type": "backup/info"})
        if not info.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    info.get("error", "Failed to retrieve backup information"),
                )
            )

        result_block = info.get("result") or {}
        raw_backups = result_block.get("backups") or []
        summarized = [_summarize_backup(b) for b in raw_backups]
        # Newest first so "did my backup land?" is answered by the top entry.
        # Parse via _parse_backup_date (handles `Z`/naive) rather than sorting
        # the raw strings lexicographically — `'Z'` > `'+'` would misorder a mix
        # of `...Z` and `...+00:00`. Undated entries sink to the bottom.
        _date_floor = datetime.min.replace(tzinfo=UTC)
        summarized.sort(
            key=lambda b: _parse_backup_date(b.get("date")) or _date_floor,
            reverse=True,
        )
        total = len(summarized)
        if limit and total > limit:
            summarized = summarized[:limit]
        return {
            "success": True,
            "count": len(summarized),
            "total": total,
            "backups": summarized,
        }

    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error listing backups: {e}")
        exception_to_structured_error(
            e,
            context={"tool": "list_backups"},
            suggestions=["Check Home Assistant connection and the backup integration"],
        )
        return None  # unreachable: exception_to_structured_error always raises


def _refuse_snapshot_delete(message: str, **context: Any) -> NoReturn:
    """Raise the shared VALIDATION_INVALID_PARAMETER shape for every
    snapshot-delete guard rejection (gate, confirm, automatic-settings, age
    floor, newest-snapshot protection)."""
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            message,
            context=context,
        )
    )


def _newest_backup_id(backups: list[dict[str, Any]]) -> str | None:
    """Return the ``backup_id`` of the single most-recent entry by date, or
    None if no entry has a parseable date.

    Entries with a missing/malformed date are excluded from consideration —
    they can never be "the newest" — but that does not make them safe to
    delete; ``delete_backup`` fails closed on an unparseable target date
    separately, so an undated target can never be deleted regardless of
    this function's result.
    """
    dated = [
        (b.get("backup_id"), parsed)
        for b in backups
        if (parsed := _parse_backup_date(b.get("date"))) is not None
    ]
    if not dated:
        return None
    return max(dated, key=lambda pair: pair[1])[0]


async def delete_backup(
    client: HomeAssistantClient,
    backup_id: str,
    confirm: bool = False,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Delete one full HA snapshot tarball (#1861).

    Off by default (``enable_snapshot_delete``) — a human must opt in via
    env var, the web settings UI override file, or (in the add-on) the
    Supervisor options; an agent cannot enable this itself. Layered guards
    beyond the gate, applied in order:

    1. ``confirm=True`` is required.
    2. ``backup/info`` must not report ``agent_errors`` — evaluated before
       the "does it exist" check right below, since an agent that failed
       to enumerate its backups could make that list (and every guard
       below) incomplete. The backup must then exist (verified against
       ``backup/info``, mirroring ``restore_backup`` — HA's own
       ``backup/delete`` silently no-ops on an unknown ID rather than
       erroring).
    3. Scheduled backups (``with_automatic_settings=True``) are never
       deletable. Fails closed the same way when HA can't confirm the
       backup is manual (``with_automatic_settings=None`` — not created
       by this HA instance, or predates this metadata field): only an
       explicit ``False`` is treated as deletable.
    4. The single newest remaining snapshot, of any type, is never
       deletable — guarantees at least one recovery point always survives.
       Checked before the age floor below so a backup that is both the
       newest AND too young reports "newest" (with its own remedy), not
       a "too young" message whose suggested remedy (lower the floor)
       would not actually unblock it.
    5. The target must be older than ``snapshot_delete_min_age_days`` (0
       unconditionally disables this floor, even across clock skew between
       this host and the HA instance that stamped the backup's date). A
       missing/unparseable date fails closed (treated as too new to delete).

    The actual ``backup/delete`` call uses an elevated WS wait timeout
    (``_BACKUP_DELETE_WAIT_S``) since it fans out to every configured agent
    — including slow/rate-limited remote ones — server-side. ``ctx``, when
    supplied, receives one ``report_progress`` heartbeat immediately before
    that call.

    Raises ToolError if disabled, unconfirmed, not found, blocked by a
    guard, or if HA reports a per-agent deletion failure.
    """
    settings = get_global_settings()
    if not settings.enable_snapshot_delete:
        _refuse_snapshot_delete(
            "Snapshot deletion is disabled on this server "
            "(enable_snapshot_delete=false). A human must enable it via the "
            "ENABLE_SNAPSHOT_DELETE env var, the web settings UI, or (in "
            "the add-on) the Supervisor options — this cannot be turned on "
            "from a tool call.",
        )
    if not confirm:
        _refuse_snapshot_delete(
            "Deletion not confirmed. Set confirm=True to delete a snapshot.",
            backup_id=backup_id,
        )

    ws_client = None
    try:
        ws_client, error = await get_connected_ws_client(
            client.base_url, client.token, verify_ssl=client.verify_ssl
        )
        if error:
            raise_tool_error(
                error
                or create_error_response(
                    ErrorCode.CONNECTION_FAILED,
                    "Failed to connect to Home Assistant WebSocket for backup deletion",
                )
            )
        ws_client = cast(HomeAssistantWebSocketClient, ws_client)

        info_result = await ws_client.send_command("backup/info")
        if not info_result.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    info_result.get("error", "Failed to retrieve backup information"),
                )
            )
        info_result_block = info_result.get("result") or {}
        info_agent_errors = info_result_block.get("agent_errors") or {}
        if info_agent_errors:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Cannot verify the backup inventory is complete: one or "
                    "more backup agents failed to respond, so the newest-"
                    "snapshot and scheduled-backup guards can't be trusted "
                    "against a possibly-partial list.",
                    context={"backup_id": backup_id, "agent_errors": info_agent_errors},
                )
            )
        backups = info_result_block.get("backups") or []
        matched = next((b for b in backups if b.get("backup_id") == backup_id), None)
        if matched is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"Backup '{backup_id}' not found",
                    suggestions=[
                        "Use ha_manage_backup(scope='snapshot', action='list') "
                        "to see available backup IDs",
                    ],
                )
            )

        if matched.get("with_automatic_settings") is not False:
            _refuse_snapshot_delete(
                "Refusing to delete a scheduled (automatic) backup, or one "
                "whose automatic/manual origin Home Assistant cannot "
                "confirm (with_automatic_settings=None — not created by "
                "this HA instance, or predates this metadata) — treated "
                "as not provably manual, and therefore not deletable.",
                backup_id=backup_id,
            )

        # Checked before the age floor below: a backup that is BOTH the
        # newest AND too young must report "newest" (create a new one
        # first), not "too young" — the age-floor message's remedy
        # ("lower snapshot_delete_min_age_days") would be actively
        # misleading advice for the newest snapshot, which stays
        # protected at any age floor including 0.
        newest_id = _newest_backup_id(backups)
        if newest_id == backup_id:
            _refuse_snapshot_delete(
                "Refusing to delete the newest snapshot — at least one "
                "recovery point must remain. Create a new snapshot first "
                "if you specifically need to free this one's space.",
                backup_id=backup_id,
            )

        min_age_days = settings.snapshot_delete_min_age_days
        target_date = _parse_backup_date(matched.get("date"))
        if target_date is None:
            _refuse_snapshot_delete(
                "Refusing to delete a backup with a missing or unparseable "
                "date — cannot verify it clears the minimum-age floor.",
                backup_id=backup_id,
            )
        cutoff = datetime.now(UTC) - timedelta(days=min_age_days)
        # min_age_days=0 must unconditionally disable the floor, even if
        # target_date is slightly ahead of this host's clock (clock skew
        # vs the HA instance that stamped it) — guard explicitly rather
        # than relying on the arithmetic reduction, which clock skew breaks.
        if min_age_days > 0 and target_date > cutoff:
            _refuse_snapshot_delete(
                f"Refusing to delete a backup younger than "
                f"snapshot_delete_min_age_days={min_age_days} days — at "
                "least one recent recovery point must remain. Delete an "
                "older backup, or lower snapshot_delete_min_age_days if "
                "this recurs.",
                backup_id=backup_id,
            )

        await safe_progress(
            ctx,
            progress=0,
            total=_BACKUP_DELETE_WAIT_S,
            message="deleting backup",
        )
        delete_result = await ws_client.send_command(
            "backup/delete",
            backup_id=backup_id,
            _wait_timeout=_BACKUP_DELETE_WAIT_S,
        )
        if not delete_result.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    delete_result.get("error", "Backup deletion failed"),
                    context={"backup_id": backup_id},
                )
            )
        agent_errors = (delete_result.get("result") or {}).get("agent_errors") or {}
        if agent_errors:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Backup deletion failed on one or more agents",
                    context={"backup_id": backup_id, "agent_errors": agent_errors},
                )
            )

        return {
            "success": True,
            "backup_id": backup_id,
            "name": matched.get("name"),
            "status": "Backup deleted successfully",
        }

    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error deleting backup: {e}")
        exception_to_structured_error(
            e,
            context={"tool": "delete_backup", "backup_id": backup_id},
            suggestions=["Check Home Assistant connection and backup availability"],
        )
        return None  # unreachable: exception_to_structured_error always raises
    finally:
        if ws_client:
            try:
                await ws_client.disconnect()
            except (TimeoutError, OSError, ConnectionError) as err:
                logger.debug(
                    "ws disconnect (cleanup) transport error: %s: %s",
                    type(err).__name__,
                    err,
                )


# Valid (scope, action) combinations. Anything outside this set is
# rejected with a structured VALIDATION_INVALID_PARAMETER error.
_VALID_COMBOS: set[tuple[str, str]] = {
    ("snapshot", "create"),
    ("snapshot", "list"),
    ("snapshot", "restore"),
    ("snapshot", "delete"),
    ("edits", "create"),
    ("edits", "list"),
    ("edits", "view"),
    ("edits", "diff"),
    ("edits", "restore"),
    ("edits", "delete"),
}


def _gate_combo(scope: str, action: str) -> None:
    """Reject (scope, action) combinations that do not exist.

    Strong gating defends against the LLM accidentally routing "restore
    my automation" through ``(snapshot, restore)`` (which would restart
    HA). The error response lists every legal combo so the LLM can
    self-correct on the next call.
    """
    if (scope, action) in _VALID_COMBOS:
        return
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"Invalid combination: scope={scope!r}, action={action!r}",
            context={"scope": scope, "action": action},
            suggestions=[
                "Valid combinations: "
                + ", ".join(sorted(f"({s},{a})" for s, a in _VALID_COMBOS)),
                "scope='snapshot' is for full HA tarball backups (heavy, restart on restore)",
                "scope='edits' is for per-entity auto-backups produced by write tools (lightweight)",
            ],
        )
    )


def _require(param_name: str, value: Any, scope: str, action: str) -> Any:
    """Validate a required parameter for the picked (scope, action) cell."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"{param_name!r} is required for scope={scope!r}, action={action!r}",
                context={"scope": scope, "action": action, "missing_param": param_name},
            )
        )
    return value


def register_backup_tools(
    mcp: "FastMCP", client: HomeAssistantClient, **kwargs: Any
) -> None:
    """Register the polymorphic ``ha_manage_backup`` tool.

    Replaces the previous ``ha_backup_create`` and ``ha_backup_restore``
    pair (merged into one tool, separated by ``scope``). All existing
    snapshot functionality is preserved under ``scope="snapshot"``.
    """
    backup_hint_text = _get_backup_hint_text()
    manage_backup_description = f"""Manage Home Assistant backups — both full HA snapshots AND per-edit auto-backups.

**Pick the scope first**, then the action. Wrong scope routes through the wrong code path:

| scope | action | What it does |
|---|---|---|
| `snapshot` | `create` | Create a full HA tarball (config + addons, no DB by default). Can take a while on a large instance; progress heartbeats are sent while waiting. |
| `snapshot` | `list` | List full HA tarball snapshots (id, name, date, size). Read-only — use to discover a `backup_id` or confirm a backup landed. |
| `snapshot` | `restore` | Restore a full HA tarball. **Restarts HA.** Last-resort recovery. |
| `snapshot` | `delete` | Delete one full HA tarball by `backup_id` (`confirm=True` required). **Disabled by default** (`enable_snapshot_delete` setting) and layered with guards even when enabled — see below. |
| `edits` | `create` | On-demand snapshot of one entity (`domain` + `entity_id` required). Use before the user manually edits in the HA UI. Same handler path the decorator takes on writes; bypasses the `enable_auto_backup` toggle. |
| `edits` | `list` | List per-entity auto-backups (lightweight). Filter by `domain` and/or `entity_id`. |
| `edits` | `view` | Read one auto-backup file by name; returns YAML and parsed `config`. |
| `edits` | `diff` | Compare one auto-backup against the entity's current config. RFC 6902 JSON-Patch + add/remove/replace counts; bounded output. Read-only — fetches the live config, makes no changes. |
| `edits` | `restore` | Re-apply one auto-backup. Creates a fresh safety snapshot first. **No HA restart.** |
| `edits` | `delete` | Delete one auto-backup by `backup_name`, or bulk-delete by filter. |

**When to use which scope:**
- Use `scope="edits"` to undo a recent automation/script/scene/dashboard/helper edit by the agent. Lightweight, fast, no restart.
- Use `scope="snapshot"` only for system-wide recovery (botched add-on update, mass config corruption, etc.).

**`scope="snapshot"` backup-hint:**
{backup_hint_text}

**`(snapshot, delete)` is off by default and layered even when enabled:** a human must
set `enable_snapshot_delete=true` (env var, web settings UI, or add-on Supervisor
options) — an agent cannot turn this on itself. When enabled, a delete call is still
refused if: the target is a scheduled/automatic backup; it's younger than
`snapshot_delete_min_age_days` (default 7, 0 disables the floor); or it's the single
newest snapshot remaining. These guarantee at least one recovery point always
survives an agent's own mistakes.

**`enable_auto_backup` and `scope="edits"`:** the automatic-on-write capture (every wrapped tool call) is gated by `enable_auto_backup=true` — if the listing is empty, check the toggle (web settings UI or `ENABLE_AUTO_BACKUP=true` env var). The explicit `(edits, create)` action bypasses the toggle since the request is explicit; `list` / `view` / `restore` / `delete` operate on whatever's already on disk regardless of the toggle's current state.

**Examples:**
- Snapshot before risky op: `ha_manage_backup(scope="snapshot", action="create", name="Before_Big_Change")`
- List snapshots (to discover a backup_id or confirm one landed): `ha_manage_backup(scope="snapshot", action="list")`
- Restore full snapshot: `ha_manage_backup(scope="snapshot", action="restore", backup_id="dd7550ed")`
- Delete an old snapshot (requires `enable_snapshot_delete=true`): `ha_manage_backup(scope="snapshot", action="delete", backup_id="dd7550ed", confirm=True)`
- On-demand entity snapshot before a manual UI edit: `ha_manage_backup(scope="edits", action="create", domain="helper_input_boolean", entity_id="kitchen_lights_active")`
- List recent auto-backups for one automation: `ha_manage_backup(scope="edits", action="list", domain="automation", entity_id="kitchen_lights")`
- View an auto-backup: `ha_manage_backup(scope="edits", action="view", backup_name="automation.kitchen_lights.20260521_153000.yaml")`
- Diff an auto-backup vs current state: `ha_manage_backup(scope="edits", action="diff", backup_name="automation.kitchen_lights.20260521_153000.yaml")`
- Restore an auto-backup: `ha_manage_backup(scope="edits", action="restore", backup_name="automation.kitchen_lights.20260521_153000.yaml")`
- Delete one auto-backup: `ha_manage_backup(scope="edits", action="delete", backup_name="...")`
- Bulk-delete old auto-backups: `ha_manage_backup(scope="edits", action="delete", older_than_days=30)`
"""

    @mcp.tool(
        description=manage_backup_description,
        tags={"System"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Manage Backups",
        },
    )
    @log_tool_usage
    async def ha_manage_backup(
        scope: Annotated[
            Literal["snapshot", "edits"],
            Field(
                description="'snapshot' for full HA tarballs; 'edits' for per-entity auto-backups."
            ),
        ],
        action: Annotated[
            Literal["create", "restore", "list", "view", "diff", "delete"],
            Field(
                description="Operation to perform. Valid (scope, action) combinations are listed in the tool description."
            ),
        ],
        # snapshot scope params
        name: Annotated[
            str | None,
            Field(
                default=None,
                description="(snapshot.create) Tarball name. Auto-generated if not provided.",
            ),
        ] = None,
        backup_id: Annotated[
            str | None,
            Field(
                default=None,
                description="(snapshot.restore / snapshot.delete) Tarball ID (e.g. 'dd7550ed').",
            ),
        ] = None,
        restore_database: Annotated[
            bool,
            Field(
                default=False,
                description="(snapshot.restore) Include database in the restore. Default false (config-only).",
            ),
        ] = False,
        confirm: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "(snapshot.delete) Must be True to confirm deletion — a "
                    "safety measure against accidental calls."
                ),
            ),
        ] = False,
        # edits scope params
        domain: Annotated[
            str | None,
            Field(
                default=None,
                description="(edits.list / edits.delete) Filter auto-backups by domain (e.g. 'automation', 'helper_timer').",
            ),
        ] = None,
        entity_id: Annotated[
            str | None,
            Field(
                default=None,
                description="(edits.list / edits.delete) Filter auto-backups by entity ID.",
            ),
        ] = None,
        backup_name: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "(edits.view / edits.restore / edits.delete) Auto-backup filename "
                    "(format '<domain>.<entity_id>.<timestamp>.yaml'). Not a tarball ID."
                ),
            ),
        ] = None,
        older_than_days: Annotated[
            int | None,
            Field(
                default=None,
                ge=0,
                description="(edits.delete) Bulk-delete auto-backups older than this many days.",
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                default=200,
                ge=1,
                le=10_000,
                description="(edits.list / snapshot.list) Maximum number of entries to return.",
            ),
        ] = 200,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Polymorphic backup tool. See the tool description for the routing matrix."""
        _gate_combo(scope, action)

        if scope == "snapshot":
            if action == "create":
                return await create_backup(client, name, ctx=ctx)
            if action == "list":
                return await list_backups(client, limit)
            if action == "delete":
                bid = _require("backup_id", backup_id, scope, action)
                return await delete_backup(client, bid, confirm, ctx=ctx)
            # action == "restore"
            bid = _require("backup_id", backup_id, scope, action)
            return await restore_backup(client, bid, restore_database, ctx=ctx)

        # scope == "edits"
        settings = get_global_settings()
        mgr = get_backup_manager(client, settings)

        if action == "create":
            # On-demand snapshot for "I'm about to edit this in the HA UI,
            # save the current state first." Drives the same handler path
            # the ``@with_auto_backup`` decorator uses on writes, but goes
            # through ``mgr.maybe_snapshot(force=True)`` which bypasses
            # both the ``enable_auto_backup`` toggle and the per-entity
            # throttle window — the request is explicit, so neither
            # should suppress it.
            dom = _require("domain", domain, scope, action)
            eid = _require("entity_id", entity_id, scope, action)
            if mgr.handler_for(dom) is None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"No backup handler registered for domain={dom!r}",
                        context={"domain": dom, "entity_id": eid},
                        suggestions=[
                            "Supported domains: " + ", ".join(mgr.supported_domains()),
                        ],
                    )
                )
            path = await mgr.maybe_snapshot(
                dom,
                eid,
                tool_name="ha_manage_backup.edits.create",
                force=True,
            )
            if path is None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"Could not snapshot {dom}:{eid} — entity not found "
                        + "or fetch returned no config",
                        context={"domain": dom, "entity_id": eid},
                        suggestions=[
                            "Verify the entity exists via the matching "
                            + "ha_config_get_* tool first",
                            "For helpers, pass domain='helper_<helper_type>' "
                            + "(e.g. 'helper_input_boolean')",
                        ],
                    )
                )
            return {
                "success": True,
                "data": {
                    "backup_name": path.name,
                    "domain": dom,
                    "entity_id": eid,
                    "size": path.stat().st_size,
                },
            }

        if action == "list":
            # Edits-store snapshots + pre-#1579 legacy .bak entries (#1579).
            # The manager owns the merge (sync dir-glob off-thread + async
            # legacy service call) so this layer stays source-agnostic.
            entries = await mgr.list_edits_and_legacy(
                domain=domain,
                entity_id=entity_id,
                limit=limit,
            )
            return {
                "success": True,
                "data": {
                    "backups": entries,
                    "count": len(entries),
                    "backup_dir": str(mgr.backup_dir),
                    "enabled": mgr.enabled,
                    "throttle_minutes": settings.auto_backup_throttle_minutes,
                    "retain_per_entity": settings.auto_backup_retain_per_entity,
                },
            }

        if action == "view":
            bname = _require("backup_name", backup_name, scope, action)
            try:
                if bname.startswith(LEGACY_PREFIX):
                    # Legacy read is an async service call (component-side store),
                    # not a local-dir read; same FileNotFound/ValueError contract.
                    data = await mgr.read_legacy(bname[len(LEGACY_PREFIX) :])
                else:
                    data = await asyncio.to_thread(mgr.read_snapshot, bname)
            except FileNotFoundError:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"Backup {bname!r} not found",
                        context={"backup_name": bname},
                    )
                )
            except ValueError as err:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        str(err),
                        context={"backup_name": bname},
                    )
                )
            return {"success": True, "data": data}

        if action == "diff":
            bname = _require("backup_name", backup_name, scope, action)
            try:
                diff = await mgr.diff_snapshot(bname)
            except FileNotFoundError:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"Backup {bname!r} not found",
                        context={"backup_name": bname},
                    )
                )
            except (ValueError, LookupError) as err:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        str(err),
                        context={"backup_name": bname},
                    )
                )
            except ToolError:
                raise
            except Exception as err:
                # Fetching the live config for diff goes through the
                # same domain handler ``restore`` uses, so the same
                # HA-side failure modes (4xx/5xx, WS errors, schema
                # drift) apply. Funnel through
                # ``exception_to_structured_error`` so the structured
                # response carries enough context to retry.
                exception_to_structured_error(
                    err,
                    context={"backup_name": bname, "action": "diff"},
                    suggestions=[
                        "Verify the entity referenced by the backup still "
                        + "exists; diff fetches its current config",
                        "Inspect the snapshot YAML via "
                        + "ha_manage_backup(scope='edits', action='view', "
                        + "backup_name=...) to confirm it parses",
                    ],
                )
                return None  # unreachable: exception_to_structured_error always raises
            warnings: list[str] = []
            if diff.get("entity_missing"):
                # ``restore_snapshot`` outcome on a missing entity is
                # domain-dependent: upsert paths (automation, script,
                # dashboard) recreate it, but helper / label / category
                # restores go through ``<domain>/update`` WS commands
                # that expect the entity to exist and would surface a
                # WS error if it does not. Hedge rather than promise
                # one specific outcome.
                warnings.append(
                    "Entity is missing from HA; restore behaviour is "
                    "domain-dependent (upsert paths recreate it; "
                    "update-only paths return an error)"
                )
            if diff.get("truncated"):
                # Text snapshots (file/YAML) carry a unified diff, not a
                # JSON-Patch — name it accordingly.
                noun = "Diff" if diff.get("kind") == "text" else "Patch"
                warnings.append(
                    f"{noun} truncated; the target has more changes than the "
                    "bounded diff captures — view the snapshot for the full state"
                )
            return {
                "success": True,
                "data": diff,
                **({"warnings": warnings} if warnings else {}),
            }

        if action == "restore":
            bname = _require("backup_name", backup_name, scope, action)
            try:
                result = await mgr.restore_snapshot(bname)
            except FileNotFoundError:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"Backup {bname!r} not found",
                        context={"backup_name": bname},
                    )
                )
            except (ValueError, LookupError) as err:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        str(err),
                        context={"backup_name": bname},
                    )
                )
            except MandatoryBackupError as err:
                # A legacy restore's mandatory pre-restore safety snapshot
                # genuinely failed — the overwrite was blocked, nothing changed.
                # Same fail-closed contract + error code as the @with_auto_backup
                # write path (#1579), so callers see one consistent failure mode.
                raise_tool_error(
                    create_error_response(
                        ErrorCode.BACKUP_CAPTURE_FAILED,
                        f"Restore blocked: the pre-restore safety snapshot could "
                        f"not be captured: {err}. Nothing was changed.",
                        context={"backup_name": bname},
                        suggestions=err.suggestions
                        or ["Retry once the underlying issue is resolved"],
                    )
                )
            except ToolError:
                raise
            except Exception as err:
                # ``handler.restore`` is domain-specific and can surface
                # HA-side rejections (schema-validation failures, 4xx/5xx
                # responses, WS command errors). Without this catch those
                # propagate as opaque INTERNAL_ERROR with no
                # ``backup_name`` / ``domain`` context — the user is left
                # to read the FastMCP traceback. Funnel through
                # ``exception_to_structured_error`` so the structured
                # response carries enough context to retry.
                exception_to_structured_error(
                    err,
                    context={"backup_name": bname, "action": "restore"},
                    suggestions=[
                        "Verify the entity referenced by the backup still "
                        + "exists; restore re-POSTs to its current registry "
                        + "key",
                        "Compare the captured schema vs current HA — HA "
                        + "minor versions occasionally drop/rename fields",
                        "Inspect the snapshot YAML via "
                        + "ha_manage_backup(scope='edits', action='view', "
                        + "backup_name=...)",
                    ],
                )
                return None  # unreachable: exception_to_structured_error always raises
            return {
                "success": True,
                "data": result,
                "warnings": [
                    "This restore did NOT restart HA. To revert, restore the safety_backup."
                ]
                if result.get("safety_backup")
                else [],
            }

        # action == "delete"
        if backup_name:
            try:
                await asyncio.to_thread(mgr.delete_snapshot, backup_name)
            except FileNotFoundError:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"Backup {backup_name!r} not found",
                        context={"backup_name": backup_name},
                    )
                )
            except ValueError as err:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        str(err),
                        context={"backup_name": backup_name},
                    )
                )
            return {"success": True, "data": {"deleted": [backup_name]}}
        # Bulk
        if domain is None and entity_id is None and older_than_days is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "edits.delete requires backup_name OR at least one filter "
                    "(domain, entity_id, older_than_days)",
                    suggestions=[
                        "Pass backup_name to delete one auto-backup",
                        "Pass domain/entity_id/older_than_days to bulk-delete (requires at least one)",
                    ],
                )
            )
        bulk = await asyncio.to_thread(
            mgr.delete_bulk,
            domain=domain,
            entity_id=entity_id,
            older_than_days=older_than_days,
        )
        deleted = bulk["deleted"]
        failed = bulk["failed"]
        return {
            "success": True,
            "data": {
                "deleted": deleted,
                "failed": failed,
                "count": len(deleted),
                "failed_count": len(failed),
            },
            "warnings": (
                [f"Failed to delete {len(failed)} backup(s); see server log"]
                if failed
                else []
            ),
        }
