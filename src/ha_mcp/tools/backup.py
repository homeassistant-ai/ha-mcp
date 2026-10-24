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
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal, NoReturn, cast

from pydantic import Field

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..backup_manager import (
    LEGACY_PREFIX,
    BackupManager,
    BackupRestoreError,
    MandatoryBackupError,
    SnapshotInUseError,
    get_backup_manager,
)
from ..client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ..client.websocket_client import HomeAssistantWebSocketClient
from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response
from .backup_access import (
    gate_backup_combo as _gate_combo,
)
from .backup_access import (
    require_backup_access,
)
from .backup_access import (
    require_backup_param as _require,
)
from .backup_on_demand import capture_on_demand
from .helpers import (
    exception_to_structured_error,
    get_connected_ws_client,
    log_tool_usage,
    raise_tool_error,
    safe_progress,
)
from .tool_hints import write_hints

if TYPE_CHECKING:
    from ha_mcp._vendor.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Upper bound for one backup wait. Home Assistant's frontend waits on
# backup/subscribe_events without a timeout; a tool call needs a finite bound,
# and a multi-GB full backup on slow hardware takes many minutes (#1433, #1681).
_BACKUP_MAX_WAIT_S = 1800
# Longest gap between MCP progress heartbeats while a backup runs (#1861: a
# client that saw no progress for 300s concluded the connection was dead).
_BACKUP_PROGRESS_INTERVAL_S = 10
# backup/delete fans out to every configured agent (including cloud/remote
# ones — S3, Google Drive, WebDAV, etc.) via asyncio.gather server-side, and
# the WS response doesn't arrive until all of them finish. The client's
# default 30s wait (HomeAssistantWebSocketClient.send_command's
# _wait_timeout) can be too tight for a slow or rate-limited remote agent —
# same class of false timeout that _BACKUP_MAX_WAIT_S covers for creation.
_BACKUP_DELETE_WAIT_S = 60.0
# Home Assistant Cloud's backup agent (domain and unique_id are both "cloud").
_CLOUD_AGENT_ID = "cloud.cloud"
_CHECK_LIST_SUGGESTION = (
    "Check the snapshot list before retrying: the backup may exist or still run"
)


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
    if hint not in hints:
        # Silent fallback used to be harmless; it isn't now. This value is
        # interpolated into BOTH the full and the lite ha_manage_backup
        # description, so a typo ("medium", "high", a stray trailing space)
        # quietly ships the wrong guidance in two places while the docs say
        # the setting is honoured. Env-var deployments only — the app UI is
        # a dropdown.
        logger.warning(
            "BACKUP_HINT=%r is not one of %s — falling back to 'normal'.",
            hint,
            sorted(hints),
        )
        hint = "normal"
    return hints[hint]


@asynccontextmanager
async def _dedicated_ws(
    client: HomeAssistantClient,
) -> AsyncIterator[HomeAssistantWebSocketClient]:
    """A WebSocket of its own for one snapshot action, closed afterwards."""
    ws_client, error = await get_connected_ws_client(
        client.base_url, client.token, verify_ssl=client.verify_ssl
    )
    if error or ws_client is None:
        raise_tool_error(
            error
            or create_error_response(
                ErrorCode.CONNECTION_FAILED,
                "Failed to connect to Home Assistant WebSocket",
            )
        )
    try:
        yield ws_client
    finally:
        # Narrow to transport errors; a programming error during cleanup
        # should still surface.
        try:
            await ws_client.disconnect()
        except (TimeoutError, OSError, ConnectionError) as err:
            logger.debug(
                "ws disconnect (cleanup) transport error: %s: %s",
                type(err).__name__,
                err,
            )


async def _backup_plan(
    client: HomeAssistantClient, ws_client: HomeAssistantWebSocketClient
) -> tuple[str | None, list[str], bool]:
    """Return ``(password, agent_ids, is_supervised)`` for a new backup.

    Agents are picked the way Home Assistant picks them for its own backup
    before a Core update (``backup_core_before_update`` in hassio/backup.py):
    the configured automatic-backup agents other than Home Assistant Cloud,
    else the default agent. Home Assistant keeps the cloud agent there, but
    Cloud stores a single backup per instance, which an ha-mcp backup would
    replace. Without a password no backup is made, so no agent is picked.
    """
    info = await ws_client.send_command("backup/config/info")
    create = info["result"]["config"]["create_backup"]
    # Not the fail-open probe, and no default for a missing key: a Supervisor
    # install read as Core would back up without add-ons and report success.
    config = await client.get_config()
    is_supervised = "hassio" in config["components"]
    password = create.get("password")
    if not password:
        return None, [], is_supervised
    agent_ids = [a for a in create["agent_ids"] if a != _CLOUD_AGENT_ID]
    if not agent_ids:
        agent_ids = [await _default_agent(client, is_supervised)]
    return password, agent_ids, is_supervised


async def _default_agent(client: HomeAssistantClient, is_supervised: bool) -> str:
    """Home Assistant's default backup agent (``_default_agent`` in hassio/backup.py).

    Supervisor's default backup mount, else local storage; a Core install
    always has the backup integration's local agent.
    """
    if not is_supervised:
        return "backup.local"
    from .tools_addons import _supervisor_api_call

    mounts = (await _supervisor_api_call(client, "/mounts")).get("result") or {}
    return f"hassio.{mounts.get('default_backup_mount') or 'local'}"


async def _generate_backup(
    ws_client: HomeAssistantWebSocketClient,
    ctx: Context | None,
    *,
    name: str,
    **params: Any,
) -> tuple[dict[str, Any], int, list[str]]:
    """Run ``backup/generate`` to completion; return ``(entry, seconds, warnings)``.

    Subscribes to ``backup/subscribe_events`` before starting, as Home
    Assistant's frontend does. The job id is not the backup id on Supervisor
    installs, and the manager runs one backup at a time, so the result is the
    backup with this name that ``backup/info`` lists after the job and did not
    list before it. A failed upload still leaves the backup on the agents
    whose upload worked; it is returned with a warning.
    """
    known = {b["backup_id"] for b in (await _backup_info(ws_client))["backups"]}
    sub_id, queue = await ws_client.subscribe_command("backup/subscribe_events")
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        # Core sends the manager's last event before acknowledging the
        # subscription; anything but idle is another operation whose events
        # would be read as this job's.
        last = queue.get_nowait().get("event") or {}
        if last.get("manager_state") != "idle":
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Home Assistant's backup manager is busy "
                    f"({last.get('manager_state')}); wait for it to finish",
                )
            )
        job = await ws_client.send_command("backup/generate", name=name, **params)
        job_id = job["result"]["backup_job_id"]
        outcome = await _wait_for_backup(queue, job_id, started, ctx)
    finally:
        await ws_client.release_subscription(sub_id)
    listing = await _backup_info(ws_client)
    new = [
        b
        for b in listing["backups"]
        if b["backup_id"] not in known and b.get("name") == name
    ]
    # A failed upload still leaves the backup on the agents whose upload worked.
    kept = outcome.get("reason") == "upload_failed" and len(new) == 1
    if outcome.get("state") == "failed" and not kept:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Backup creation failed",
                context={"backup_job_id": job_id, "event": outcome},
            )
        )
    # An agent that fails to list drops its backups from either listing, so
    # anything but exactly one match is not a backup ha-mcp can name.
    if len(new) != 1:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Backup finished, but Home Assistant lists {len(new)} new "
                f"backups named {name!r}",
                context={
                    "backup_job_id": job_id,
                    "agent_errors": listing.get("agent_errors") or {},
                },
                suggestions=[_CHECK_LIST_SUGGESTION],
            )
        )
    entry = new[0]
    warnings = _backup_warnings(entry)
    if outcome.get("state") == "failed":
        warnings.append(
            "Home Assistant reported the upload as failed; the backup is "
            f"stored on: {', '.join(entry.get('agents') or {})}"
        )
    return entry, round(loop.time() - started), warnings


async def _backup_info(ws_client: HomeAssistantWebSocketClient) -> dict[str, Any]:
    info = await ws_client.send_command("backup/info")
    return cast(dict[str, Any], info.get("result") or {})


async def _wait_for_backup(
    queue: asyncio.Queue[dict[str, Any]],
    job_id: str,
    started: float,
    ctx: Context | None,
) -> dict[str, Any]:
    """Return the backup manager's terminal create event, sending heartbeats.

    Heartbeats keep an MCP client from dropping the connection during a long
    backup (#1861).
    """
    loop = asyncio.get_running_loop()
    may_still_run = {"backup_job_id": job_id, "likely_in_progress": True}
    while (waited := loop.time() - started) < _BACKUP_MAX_WAIT_S:
        await safe_progress(
            ctx,
            progress=round(waited),
            total=_BACKUP_MAX_WAIT_S,
            message="waiting for backup to complete",
        )
        try:
            message = await asyncio.wait_for(
                queue.get(),
                timeout=min(_BACKUP_PROGRESS_INTERVAL_S, _BACKUP_MAX_WAIT_S - waited),
            )
        except TimeoutError:
            continue
        except asyncio.QueueShutDown:
            raise_tool_error(
                create_error_response(
                    ErrorCode.CONNECTION_FAILED,
                    "Connection to Home Assistant closed while waiting for the backup",
                    context=may_still_run,
                    suggestions=[_CHECK_LIST_SUGGESTION],
                )
            )
        event = message.get("event") or {}
        if event.get("manager_state") == "create_backup" and event.get("state") in (
            "completed",
            "failed",
        ):
            return cast(dict[str, Any], event)
    raise_tool_error(
        create_error_response(
            ErrorCode.TIMEOUT_OPERATION,
            f"Backup did not finish within {_BACKUP_MAX_WAIT_S} seconds",
            context=may_still_run,
            suggestions=[_CHECK_LIST_SUGGESTION],
        )
    )


def _backup_warnings(entry: dict[str, Any]) -> list[str]:
    """What a finished backup left out, from its ``backup/info`` entry.

    Core reports a backup as done when add-ons or folders failed, and keeps
    it on the agents whose upload worked.
    """
    warnings = []
    if failed := entry.get("failed_agent_ids"):
        warnings.append(f"Not stored on backup agents: {', '.join(failed)}")
    if failed := entry.get("failed_addons"):
        slugs = ", ".join(a.get("slug", "?") for a in failed)
        warnings.append(f"Add-ons that could not be backed up: {slugs}")
    if failed := entry.get("failed_folders"):
        warnings.append(f"Folders that could not be backed up: {', '.join(failed)}")
    return warnings


async def create_backup(
    client: HomeAssistantClient,
    name: str | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Create a fast backup (no database) on the agents ``_backup_plan`` picks.

    Args:
        client: Home Assistant REST client
        name: Optional backup name (auto-generated if not provided)
        ctx: Optional FastMCP context for progress heartbeats during the wait
    """
    try:
        async with _dedicated_ws(client) as ws_client:
            password, agent_ids, is_supervised = await _backup_plan(client, ws_client)
            if not password:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        "No default backup password configured in Home Assistant",
                        suggestions=[
                            "Configure automatic backups in Home Assistant "
                            "settings to set a default password"
                        ],
                    )
                )
            # Core strips the name it stores.
            name = (name or "").strip() or (
                f"MCP_Backup_{datetime.now().strftime('%Y-%m-%d_%H:%M:%S')}"
            )
            entry, seconds, warnings = await _generate_backup(
                ws_client,
                ctx,
                name=name,
                password=password,
                agent_ids=agent_ids,
                include_homeassistant=True,
                include_database=False,
                # Add-ons are a Supervisor concept; Core rejects them.
                include_all_addons=is_supervised,
            )
        result = {
            **entry,
            "success": True,
            "duration_seconds": seconds,
            "status": "Backup completed successfully",
            "note": "Backup uses your Home Assistant's default backup password",
        }
        if warnings:
            result["warnings"] = warnings
        return result

    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error creating backup: {e}")
        exception_to_structured_error(
            e,
            context={"tool": "create_backup"},
            suggestions=["Check Home Assistant connection and backup configuration"],
        )
        return None  # unreachable: exception_to_structured_error always raises


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


def _backup_protected(entry: dict[str, Any]) -> bool:
    """Whether a ``backup/info`` or ``backup/details`` entry is encrypted.

    ``protected`` is per agent (AgentBackupStatus), not top-level; a backup
    is encrypted as a whole, so any agent reporting it decides.
    """
    agents = entry.get("agents")
    if not isinstance(agents, dict):
        return False
    return any(a.get("protected") for a in agents.values() if isinstance(a, dict))


def _restore_agent(target: dict[str, Any]) -> str:
    """An agent that holds ``target``; local and Supervisor locations need no download."""
    agent_ids: list[str] = list(target.get("agents") or {})
    return next(
        (a for a in agent_ids if a.startswith(("hassio.", "backup."))),
        agent_ids[0],
    )


async def _send_restore(
    ws_client: HomeAssistantWebSocketClient, params: dict[str, Any]
) -> bool:
    """Send ``backup/restore``; True when Home Assistant accepted it but never replied.

    Core restarts inside the restore command, and Supervisor stops Core before
    its restore job reports done, so a real restore usually ends with the
    connection closing instead of a reply. A restore event that is not
    ``failed`` tells that apart from a connection lost before Home Assistant
    took the restore; it does not prove the restore finished.
    """
    sub_id, queue = await ws_client.subscribe_command("backup/subscribe_events")
    events: list[dict[str, Any]] = []

    async def _collect() -> None:
        try:
            while True:
                events.append((await queue.get()).get("event") or {})
        except asyncio.QueueShutDown:
            return

    collector = asyncio.create_task(_collect())
    try:
        # A Supervisor restore can run for minutes before Core goes down.
        await ws_client.send_command(
            "backup/restore", _wait_timeout=_BACKUP_MAX_WAIT_S, **params
        )
        return False
    except (HomeAssistantConnectionError, HomeAssistantCommandTimeout):
        restore = [e for e in events if e.get("manager_state") == "restore_backup"]
        if not restore or restore[-1].get("state") == "failed":
            raise
        return True
    finally:
        collector.cancel()
        await ws_client.release_subscription(sub_id)


async def restore_backup(
    client: HomeAssistantClient,
    backup_id: str,
    restore_database: bool | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """
    Restore Home Assistant from a backup (DESTRUCTIVE - use with caution).

    Creates a safety backup first when the default backup password is set.

    Args:
        client: Home Assistant REST client
        backup_id: Backup ID to restore
        restore_database: Whether to restore the database; ``None`` restores
            it when the backup contains it. On Supervisor installs Home
            Assistant rejects a value that does not match the backup.
        ctx: Optional FastMCP context for progress heartbeats during the
            pre-restore safety-backup wait

    Returns:
        Dictionary with restore result including safety_backup_id, status, etc.
    """
    try:
        async with _dedicated_ws(client) as ws_client:
            details = await ws_client.send_command(
                "backup/details", backup_id=backup_id
            )
            target = details["result"]["backup"]
            if target is None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        f"Backup '{backup_id}' not found",
                        context={
                            "agent_errors": details["result"].get("agent_errors") or {}
                        },
                        suggestions=[
                            "List snapshots with ha_manage_backup(scope='snapshot', "
                            "action='list') before retrying"
                        ],
                    )
                )

            password, agent_ids, is_supervised = await _backup_plan(client, ws_client)
            if restore_database is None:
                restore_database = bool(target.get("database_included"))
            # Add-ons and folders are restored as the backup holds them; Core
            # installs restore neither and reject a non-empty list.
            if is_supervised:
                addons = [a["slug"] for a in target.get("addons") or []]
                folders = list(target.get("folders") or [])
            else:
                addons, folders = [], []
            restore_params: dict[str, Any] = {
                "backup_id": backup_id,
                "agent_id": _restore_agent(target),
                "restore_homeassistant": True,
                "restore_database": restore_database,
                "restore_addons": addons,
                "restore_folders": folders,
            }
            # Home Assistant checks a given password against the backup, and
            # the default password is the only one ha-mcp knows.
            if password and _backup_protected(target):
                restore_params["password"] = password

            warnings = [
                "Home Assistant is restarting. Connection will be temporarily lost."
            ]
            safety_backup_id = None
            if password:
                # Holds what the restore replaces: configuration, database,
                # add-ons and the folders the restore writes.
                safety, _, safety_warnings = await _generate_backup(
                    ws_client,
                    ctx,
                    name=f"PreRestore_Safety_{datetime.now().strftime('%Y-%m-%d_%H:%M:%S')}",
                    password=password,
                    agent_ids=agent_ids,
                    include_homeassistant=True,
                    include_database=True,
                    include_all_addons=is_supervised,
                    **({"include_folders": folders} if folders else {}),
                )
                safety_backup_id = safety["backup_id"]
                if safety.get("failed_addons") or safety.get("failed_folders"):
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.SERVICE_CALL_FAILED,
                            "The safety backup is missing add-ons or folders the "
                            "restore would overwrite; the restore was not started",
                            context={
                                "safety_backup_id": safety_backup_id,
                                "warnings": safety_warnings,
                            },
                        )
                    )
                warnings.extend(safety_warnings)
                note = (
                    "A safety backup was created before restore. You can "
                    "restore from it if needed."
                )
            else:
                warnings.append(
                    "No safety backup was created (the default backup "
                    "password is not set). If the restore corrupts state, "
                    "there is no automatic rollback — configure the "
                    "default password in Settings → System → Backups and "
                    "retry to get a safety net."
                )
                note = "Restore proceeding WITHOUT a safety backup. See warnings above."

            if await _send_restore(ws_client, restore_params):
                warnings.append(
                    "Home Assistant accepted the restore but did not confirm it "
                    "before the connection closed, as its restart does; check "
                    "its state once it is back before retrying."
                )
        return {
            "success": True,
            "backup_id": backup_id,
            "status": "Restore initiated - Home Assistant will restart",
            "safety_backup_id": safety_backup_id,
            "restore_database": restore_database,
            "warnings": warnings,
            "note": note,
        }

    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error restoring backup: {e}")
        exception_to_structured_error(
            e,
            context={"tool": "restore_backup", "backup_id": backup_id},
            suggestions=["Check Home Assistant connection and backup availability"],
        )
        return None  # unreachable: exception_to_structured_error always raises


async def list_backups(client: HomeAssistantClient, limit: int = 200) -> dict[str, Any]:
    """List the full HA snapshots Home Assistant reports, newest first (#1586).

    Returns Core's ``backup/info`` reply as is, with ``backups`` sorted and cut
    to ``limit`` (``count`` / ``total``), so per-agent sizes and encryption,
    failed agents, add-ons and folders reach the caller. A non-empty
    ``agent_errors`` means the list is partial; a warning says so.
    Read-only: no safety backup, no restart.
    """
    try:
        # Shared pooled WebSocket (#1813): one request/response command, so no
        # per-call connect. A failed command surfaces as ``success=False``.
        info = await client.send_websocket_message({"type": "backup/info"})
        if not info.get("success"):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    info.get("error", "Failed to retrieve backup information"),
                )
            )

        result = dict(info.get("result") or {})
        # Parsed dates, not raw strings: `'Z'` sorts after `'+'`. Undated
        # entries sink to the bottom.
        date_floor = datetime.min.replace(tzinfo=UTC)
        backups = sorted(
            result.get("backups") or [],
            key=lambda b: _parse_backup_date(b.get("date")) or date_floor,
            reverse=True,
        )
        listing: dict[str, Any] = {
            **result,
            "success": True,
            "count": min(len(backups), limit),
            "total": len(backups),
            "backups": backups[:limit],
        }
        if failed := result.get("agent_errors"):
            listing["warnings"] = [
                "Partial list: these backup agents failed to list their "
                f"backups: {', '.join(failed)}"
            ]
        return listing

    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
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
       ``backup/info`` — HA's own
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

    try:
        async with _dedicated_ws(client) as ws_client:
            matched, backups = await _verify_delete_inventory(ws_client, backup_id)
            _check_snapshot_deletable(matched, backups, settings, backup_id)
            return await _execute_snapshot_delete(ws_client, matched, backup_id, ctx)

    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error deleting backup: {e}")
        exception_to_structured_error(
            e,
            context={"tool": "delete_backup", "backup_id": backup_id},
            suggestions=["Check Home Assistant connection and backup availability"],
        )
        return None  # unreachable: exception_to_structured_error always raises


async def _verify_delete_inventory(
    ws_client: HomeAssistantWebSocketClient, backup_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Fetch ``backup/info`` for a delete: return ``(matched entry, all backups)`` or raise.

    Rejects when any agent failed to enumerate its backups (a partial list
    would make the newest/scheduled guards untrustworthy) and when the target
    backup does not exist.
    """
    info_result_block = await _backup_info(ws_client)
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
    return matched, backups


def _check_snapshot_deletable(
    matched: dict[str, Any],
    backups: list[dict[str, Any]],
    settings: Any,
    backup_id: str,
) -> None:
    """Apply the scheduled / newest / age-floor delete guards; raise to refuse."""
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


async def _execute_snapshot_delete(
    ws_client: HomeAssistantWebSocketClient,
    matched: dict[str, Any],
    backup_id: str,
    ctx: Context | None,
) -> dict[str, Any]:
    """Issue ``backup/delete`` with an elevated WS wait and return the result."""
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
| `snapshot` | `create` | Create an HA tarball (config, plus all add-ons on Supervisor installs; no DB, no share/media folders) on the backup locations of Home Assistant's automatic-backup settings other than Home Assistant Cloud; without those, on the default location. Can take a while on a large instance; progress heartbeats are sent while waiting. |
| `snapshot` | `list` | List full HA tarball snapshots as Home Assistant reports them (id, name, date, per-location size). Read-only — use to discover a `backup_id` or confirm a backup landed; a non-empty `agent_errors` means the list is partial. |
| `snapshot` | `restore` | Restore a full HA tarball. **Restarts HA.** Last-resort recovery. |
| `snapshot` | `delete` | Delete one full HA tarball by `backup_id` (`confirm=True` required). **Disabled by default** (`enable_snapshot_delete` setting) and layered with guards even when enabled — see below. |
| `edits` | `create` | On-demand snapshot of one entity (`domain` + `entity_id` required). Use before the user manually edits in the HA UI. Same handler path the decorator takes on writes; bypasses the `enable_auto_backup` toggle. |
| `edits` | `list` | List per-entity auto-backups (lightweight). Filter by `domain` and/or `entity_id`. |
| `edits` | `view` | Read one auto-backup file by name; returns YAML and parsed `config`. |
| `edits` | `diff` | Compare one auto-backup against the entity's current config. RFC 6902 JSON-Patch + add/remove/replace counts; bounded output. Read-only — fetches the live config, makes no changes. |
| `edits` | `restore` | Re-apply one auto-backup. Existing flow helpers (template, group, utility_meter, …) and config subentries require a fresh safety snapshot; other domains follow auto-backup settings and may proceed without one. A deleted flow helper is recreated with a new config-entry ID and its saved entity IDs are restored if unoccupied; a deleted subentry is recreated with a new subentry ID. A deleted integration config entry cannot be recreated: its snapshot holds only metadata and the enabled state. **No HA restart.** |
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

**Human-managed backup controls:** `enable_snapshot_actions=false` (ENABLE_SNAPSHOT_ACTIONS) blocks every snapshot action, including listing. `backup_read_only=true` (BACKUP_READ_ONLY) allows edits list/view/diff and snapshot list when snapshots are enabled; it blocks explicit create/restore/delete in both scopes. Automatic pre-edit capture still follows `enable_auto_backup`. A human must change these controls in the Backups tab, app configuration, or environment; developer tools cannot change them. Global and connection Read Only Mode still restrict writes.

**`enable_auto_backup` and `scope="edits"`:** the automatic-on-write capture (every wrapped tool call) is gated by `enable_auto_backup=true` — if the listing is empty, check the toggle (web settings UI or `ENABLE_AUTO_BACKUP=true` env var). The explicit `(edits, create)` action bypasses the toggle since the request is explicit; `list` / `view` / `restore` / `delete` operate on whatever's already on disk regardless of the toggle's current state.

**Flow-helper filters:** `edits.create` accepts a flow helper's entity ID and returns its stable config-entry ID as `entity_id`. Use that returned ID for `edits.list` and bulk `edits.delete`; those filters do not resolve entity aliases. After recreation, the restore result reports the replacement config-entry ID and `entity_id_mapping` separately.

**Examples:**
- Snapshot before risky op: `ha_manage_backup(scope="snapshot", action="create", name="Before_Big_Change")`
- List snapshots (to discover a backup_id or confirm one landed): `ha_manage_backup(scope="snapshot", action="list")`
- Restore full snapshot: `ha_manage_backup(scope="snapshot", action="restore", backup_id="dd7550ed")`
- Delete an old snapshot (requires `enable_snapshot_delete=true`): `ha_manage_backup(scope="snapshot", action="delete", backup_id="dd7550ed", confirm=True)`
- On-demand entity snapshot before a manual UI edit: `ha_manage_backup(scope="edits", action="create", domain="helper_input_boolean", entity_id="welcome_home_active")`
- List recent auto-backups for one automation: `ha_manage_backup(scope="edits", action="list", domain="automation", entity_id="welcome_home")`
- View an auto-backup: `ha_manage_backup(scope="edits", action="view", backup_name="automation.welcome_home.20260521_153000.yaml")`
- Diff an auto-backup vs current state: `ha_manage_backup(scope="edits", action="diff", backup_name="automation.welcome_home.20260521_153000.yaml")`
- Restore an auto-backup: `ha_manage_backup(scope="edits", action="restore", backup_name="automation.welcome_home.20260521_153000.yaml")`
- Delete one auto-backup: `ha_manage_backup(scope="edits", action="delete", backup_name="...")`
- Bulk-delete old auto-backups: `ha_manage_backup(scope="edits", action="delete", older_than_days=30)`
"""

    @mcp.tool(
        description=manage_backup_description,
        tags={"System"},
        annotations=write_hints(
            "Manage Backups", destructive=True, idempotent=False, open_world=False
        ),
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
            bool | None,
            Field(
                default=None,
                description="(snapshot.restore) Include the database in the restore. Default: "
                "restore it when the backup contains it. On Supervisor installs it must "
                "match whether the backup contains the database.",
            ),
        ] = None,
        confirm: Annotated[
            bool,
            Field(
                default=False,
                description=("(snapshot.delete) Must be True to confirm deletion."),
            ),
        ] = False,
        # edits scope params
        domain: Annotated[
            str | None,
            Field(
                default=None,
                description="(edits.create / edits.list / edits.delete) Filter auto-backups by domain (e.g. 'automation', 'helper_timer'). "
                "Required for edits.create.",
            ),
        ] = None,
        entity_id: Annotated[
            str | None,
            Field(
                default=None,
                description="(edits.create / edits.list / edits.delete) Filter auto-backups by entity ID. "
                "Required for edits.create.",
            ),
        ] = None,
        backup_name: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "(edits.view / edits.restore / edits.delete) Auto-backup filename "
                    "(format '<domain>.<entity_id>.<timestamp>[_NN].yaml'). Not a tarball ID."
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
        settings = get_global_settings()
        require_backup_access(settings, scope, action)

        if scope == "snapshot":
            return await _dispatch_snapshot_action(
                client,
                action,
                scope=scope,
                name=name,
                backup_id=backup_id,
                restore_database=restore_database,
                confirm=confirm,
                limit=limit,
                ctx=ctx,
            )

        # scope == "edits"
        mgr = get_backup_manager(client, settings)
        return await _dispatch_edits_action(
            mgr,
            settings,
            action,
            scope=scope,
            domain=domain,
            entity_id=entity_id,
            backup_name=backup_name,
            older_than_days=older_than_days,
            limit=limit,
        )


async def _dispatch_snapshot_action(
    client: HomeAssistantClient,
    action: str,
    *,
    scope: str,
    name: str | None,
    backup_id: str | None,
    restore_database: bool | None,
    confirm: bool,
    limit: int,
    ctx: Context | None,
) -> dict[str, Any]:
    """Route a ``scope="snapshot"`` action to its full-tarball handler."""
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


async def _dispatch_edits_action(
    mgr: BackupManager,
    settings: Any,
    action: str,
    *,
    scope: str,
    domain: str | None,
    entity_id: str | None,
    backup_name: str | None,
    older_than_days: int | None,
    limit: int,
) -> dict[str, Any]:
    """Route a ``scope="edits"`` action to its per-entity auto-backup handler."""
    if action == "create":
        return await _edits_create(mgr, scope, action, domain, entity_id)
    if action == "list":
        return await _edits_list(mgr, settings, domain, entity_id, limit)
    if action == "view":
        return await _edits_view(mgr, scope, action, backup_name)
    if action == "diff":
        return await _edits_diff(mgr, scope, action, backup_name)
    if action == "restore":
        return await _edits_restore(mgr, scope, action, backup_name)
    # action == "delete"
    return await _edits_delete(mgr, domain, entity_id, backup_name, older_than_days)


async def _edits_create(
    mgr: BackupManager,
    scope: str,
    action: str,
    domain: str | None,
    entity_id: str | None,
) -> dict[str, Any]:
    """(edits, create) On-demand force-snapshot of one entity."""
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
    path = await capture_on_demand(mgr, dom, eid)
    # The id the snapshot is stored under: a flow helper's config entry id
    # when the caller passed an entity_id alias, else the id as given.
    eid = await asyncio.to_thread(mgr._payload_entity_id, path) or eid
    return {
        "success": True,
        "data": {
            "backup_name": path.name,
            "domain": dom,
            "entity_id": eid,
            "size": path.stat().st_size,
        },
    }


async def _edits_list(
    mgr: BackupManager,
    settings: Any,
    domain: str | None,
    entity_id: str | None,
    limit: int,
) -> dict[str, Any]:
    """(edits, list) List per-entity auto-backups plus legacy .bak entries."""
    # Edits-store snapshots + pre-#1579 legacy .bak entries (#1579).
    # The manager owns the merge (sync dir-glob off-thread + async
    # legacy service call) so this layer stays source-agnostic.
    entries, warnings = await mgr.list_edits_and_legacy(
        domain=domain,
        entity_id=entity_id,
        limit=limit,
    )
    response: dict[str, Any] = {
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
    if warnings:
        response["warnings"] = warnings
    return response


async def _edits_view(
    mgr: BackupManager,
    scope: str,
    action: str,
    backup_name: str | None,
) -> dict[str, Any]:
    """(edits, view) Read one auto-backup file by name."""
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


async def _edits_diff(
    mgr: BackupManager,
    scope: str,
    action: str,
    backup_name: str | None,
) -> dict[str, Any]:
    """(edits, diff) Compare one auto-backup against the entity's live config."""
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
    except Exception as err:  # noqa: BLE001
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


async def _edits_restore(
    mgr: BackupManager,
    scope: str,
    action: str,
    backup_name: str | None,
) -> dict[str, Any]:
    """Re-apply an edit backup, or recreate a deleted flow helper or subentry."""
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
    except BackupRestoreError as err:
        code = {
            "snapshot_not_found": ErrorCode.RESOURCE_NOT_FOUND,
            "invalid_snapshot": ErrorCode.VALIDATION_INVALID_PARAMETER,
            "unsupported_domain": ErrorCode.VALIDATION_INVALID_PARAMETER,
            "backup_capture_failed": ErrorCode.BACKUP_CAPTURE_FAILED,
            "entry_deleted": ErrorCode.RESOURCE_NOT_FOUND,
        }.get(err.outcome.get("reason") or "", ErrorCode.SERVICE_CALL_FAILED)
        hint = "Inspect the current configuration and restore outcome before retrying"
        suggestions = list(err.outcome.get("suggestions") or [hint])
        if err.outcome.get("safety_backup"):
            suggestions.append(
                "Use safety_backup to inspect or restore the captured previous state"
            )
        raise_tool_error(
            create_error_response(
                code,
                str(err),
                context={"backup_name": bname, "data": err.outcome},
                suggestions=suggestions,
            )
        )
    except MandatoryBackupError as err:
        # A restore's mandatory pre-restore safety snapshot genuinely
        # failed — the overwrite was blocked, nothing changed. Same
        # fail-closed contract + error code as the @with_auto_backup write
        # path (#1579), so callers see one consistent failure mode.
        logger.warning("Restore of %s blocked: %s", bname, err.safe_cause)
        raise_tool_error(
            create_error_response(
                ErrorCode.BACKUP_CAPTURE_FAILED,
                "Restore blocked: the pre-restore safety snapshot could not be "
                f"captured ({err.safe_cause}). Nothing was changed.",
                context={
                    "backup_name": bname,
                    "data": {
                        "restored_from": bname,
                        "safety_backup": None,
                        "apply_status": "not_applied",
                        "verification_status": "not_run",
                    },
                },
                suggestions=err.suggestions
                or ["Retry once the underlying issue is resolved"],
            )
        )
    except ToolError:
        raise
    except Exception as err:  # noqa: BLE001
        # ``handler.restore`` is domain-specific and can surface HA-side
        # rejections (schema-validation failures, 4xx/5xx responses, WS command
        # errors). Without this catch those propagate as opaque INTERNAL_ERROR
        # with no ``backup_name`` / ``domain`` context. Funnel through
        # ``exception_to_structured_error``, with the manager's outcome (the
        # safety backup to revert with, warnings) as ``data``.
        exception_to_structured_error(
            err,
            context={
                "backup_name": bname,
                "action": "restore",
                "data": getattr(err, "restore_outcome", None),
            },
            suggestions=[
                "Verify the entity referenced by the backup still exists; "
                + "restore re-POSTs to its current registry key",
                "Compare the captured schema vs current HA — HA minor versions "
                + "occasionally drop/rename fields",
                "Inspect the snapshot YAML via ha_manage_backup(scope='edits', "
                + "action='view', backup_name=...)",
            ],
        )
        return None  # unreachable: exception_to_structured_error always raises
    # The manager also serves Settings, which consumes handler warnings in place.
    # Copy its result before moving warnings to the MCP response envelope.
    data = dict(result)
    warnings: list[str] = list(data.pop("warnings", []))
    if isinstance(result.get("result"), dict):
        handler_result = dict(result["result"])
        warnings.extend(handler_result.pop("warnings", []))
        data["result"] = handler_result
    if result.get("safety_backup"):
        warnings.append(
            "This restore did NOT restart HA. To revert, restore the safety_backup."
        )
    return {
        "success": True,
        "data": data,
        **({"warnings": list(dict.fromkeys(warnings))} if warnings else {}),
    }


async def _edits_delete(
    mgr: BackupManager,
    domain: str | None,
    entity_id: str | None,
    backup_name: str | None,
    older_than_days: int | None,
) -> dict[str, Any]:
    """(edits, delete) Delete one auto-backup by name, or bulk-delete by filter."""
    if backup_name:
        try:
            await asyncio.to_thread(mgr.delete_snapshot, backup_name)
        except SnapshotInUseError as err:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(err),
                    context={
                        "backup_name": backup_name,
                        "data": {"reason": "snapshot_in_use"},
                    },
                    suggestions=[
                        "Retry deletion after the active capture or restore finishes"
                    ],
                )
            )
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
    warnings = (
        [f"Failed to delete {len(failed)} backup(s); see server log"] if failed else []
    )
    in_use_count = sum(
        reason == "snapshot_in_use"
        for reason in bulk.get("failure_reasons", {}).values()
    )
    if in_use_count:
        warnings.append(
            f"{in_use_count} backup(s) are in use by a capture or restore; "
            "retry deletion after it finishes."
        )
    return {
        "success": True,
        "data": {
            **bulk,
            "count": len(deleted),
            "failed_count": len(failed),
        },
        **({"warnings": warnings} if warnings else {}),
    }
