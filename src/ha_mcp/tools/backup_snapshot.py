"""Full Home Assistant snapshots for ``ha_manage_backup(scope="snapshot")``.

Create, list, restore and delete go through Home Assistant's own backup
integration (``backup/*`` WebSocket commands). The tool itself is registered
in ``backup.py``.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn, cast

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ..client.websocket_client import HomeAssistantWebSocketClient
from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    get_connected_ws_client,
    raise_tool_error,
    safe_progress,
)

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


def _require_idle(queue: asyncio.Queue[dict[str, Any]]) -> None:
    """Refuse unless the backup manager is idle.

    Core sends the manager's last event before acknowledging the subscription;
    anything but idle is another operation whose events would be read as this
    one's.
    """
    last = queue.get_nowait().get("event") or {}
    if last.get("manager_state") != "idle":
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Home Assistant's backup manager is busy "
                f"({last.get('manager_state')}); wait for it to finish",
            )
        )


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
    list before it, unless only agents that failed the earlier listing hold
    it. A failed upload still leaves the backup on the agents whose upload
    worked; it is returned with a warning.
    """
    before = await _backup_info(ws_client)
    known = {b["backup_id"] for b in before["backups"]}
    # A backup held only by agents that failed this listing may predate the job.
    unlisted = set(before.get("agent_errors") or {})
    sub_id, queue = await ws_client.subscribe_command("backup/subscribe_events")
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        _require_idle(queue)
        job = await ws_client.send_command("backup/generate", name=name, **params)
        job_id = job["result"]["backup_job_id"]
        outcome = await _wait_for_backup(queue, job_id, started, ctx)
    finally:
        await ws_client.release_subscription(sub_id)
    listing = await _backup_info(ws_client)
    new = [
        b
        for b in listing["backups"]
        if b["backup_id"] not in known
        and b.get("name") == name
        and not (unlisted and unlisted.issuperset(b.get("agents") or {}))
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
                    "agent_errors_before_job": before.get("agent_errors") or {},
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
        warnings.append(f"Apps (add-ons) that could not be backed up: {slugs}")
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
    ws_client: HomeAssistantWebSocketClient,
    params: dict[str, Any],
    ctx: Context | None,
    started: float,
) -> bool:
    """Send ``backup/restore``; True when Home Assistant accepted it but never replied.

    Core restarts inside the restore command, and Supervisor stops Core before
    its restore job reports done, so a real restore usually ends with the
    connection closing instead of a reply. A restore event that is not
    ``failed`` tells that apart from a connection lost before Home Assistant
    took the restore; it does not prove the restore finished. Heartbeats
    continue while it waits; ``started`` keeps their progress rising after
    the safety backup's.
    """
    loop = asyncio.get_running_loop()
    sub_id, queue = await ws_client.subscribe_command("backup/subscribe_events")
    events: list[dict[str, Any]] = []

    async def _collect() -> None:
        try:
            while True:
                await safe_progress(
                    ctx,
                    progress=round(loop.time() - started),
                    message="waiting for the restore",
                )
                try:
                    message = await asyncio.wait_for(
                        queue.get(), timeout=_BACKUP_PROGRESS_INTERVAL_S
                    )
                except TimeoutError:
                    continue
                events.append(message.get("event") or {})
        except asyncio.QueueShutDown:
            return

    collector: asyncio.Task[None] | None = None
    try:
        _require_idle(queue)
        collector = asyncio.create_task(_collect())
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
        if collector is not None:
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
            safety-backup and restore waits

    Returns:
        Dictionary with restore result including safety_backup_id, status, etc.
    """
    started = asyncio.get_running_loop().time()
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
                            "The safety backup is missing apps (add-ons) or folders the "
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

            if await _send_restore(ws_client, restore_params, ctx, started):
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
