"""Snapshot restore for ``ha_manage_backup(scope="snapshot", action="restore")``.

Restores through Home Assistant's ``backup/restore`` after a safety backup of
what the restore replaces. Shared backup plumbing lives in
``backup_snapshot.py``.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ..client.websocket_client import HomeAssistantWebSocketClient
from ..errors import ErrorCode, create_error_response
from .backup_snapshot import (
    _BACKUP_MAX_WAIT_S,
    _BACKUP_PROGRESS_INTERVAL_S,
    _backup_plan,
    _dedicated_ws,
    _generate_backup,
    _require_idle,
)
from .helpers import exception_to_structured_error, raise_tool_error, safe_progress

logger = logging.getLogger(__name__)


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
    safety_backup_id: str | None = None,
) -> str | None:
    """Send ``backup/restore``; return a warning when Home Assistant took it but never replied.

    Core restarts inside the restore command, and Supervisor stops Core before
    its restore job reports done, so a real restore usually ends the wait with
    the connection closing instead of a reply, or, for a long Supervisor
    restore, with the command timeout. A restore event that is not ``failed``
    tells that apart from a connection lost before Home Assistant took the
    restore; it does not prove the restore finished. Heartbeats continue while
    it waits; ``started`` keeps their progress rising after the safety
    backup's. A busy backup manager is refused, naming ``safety_backup_id``.
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
        _require_idle(
            queue, {"safety_backup_id": safety_backup_id} if safety_backup_id else None
        )
        collector = asyncio.create_task(_collect())
        # A Supervisor restore can run for minutes before Core goes down.
        await ws_client.send_command(
            "backup/restore", _wait_timeout=_BACKUP_MAX_WAIT_S, **params
        )
        return None
    except HomeAssistantCommandNotSent:
        # Another client's restore event must not read as this one's.
        raise
    except (HomeAssistantConnectionError, HomeAssistantCommandTimeout) as err:
        restore = [e for e in events if e.get("manager_state") == "restore_backup"]
        if not restore or restore[-1].get("state") == "failed":
            raise
        if isinstance(err, HomeAssistantCommandTimeout):
            return (
                "Home Assistant took the restore but did not answer within "
                f"{_BACKUP_MAX_WAIT_S} seconds; it may still be running. "
                "Check its state before retrying."
            )
        return (
            "Home Assistant accepted the restore but did not confirm it "
            "before the connection closed, as its restart does; check "
            "its state once it is back before retrying."
        )
    finally:
        if collector is not None:
            collector.cancel()
        await ws_client.release_subscription(sub_id)


def _check_restore_request(
    target: dict[str, Any],
    *,
    restore_database: bool,
    addons: list[str],
    folders: list[str],
    is_supervised: bool,
) -> None:
    """Refuse a restore Home Assistant would reject or mishandle, before the safety backup.

    An HA-MCP safeguard: Home Assistant checks these only inside
    ``backup/restore``, which runs after the safety backup, so each retry
    would leave another one behind; some it does not reject at all. All
    reasons are reported at once.
    """
    reasons = []
    has_database = bool(target.get("database_included"))
    if restore_database and not has_database:
        # Rejected on Supervisor installs; elsewhere Home Assistant deletes the
        # current database and restores none.
        reasons.append("restore_database=true, but the backup has no database")
    elif is_supervised and not restore_database and has_database:
        reasons.append(
            "restore_database=false, but the backup has a database, and on "
            "Supervisor installs Home Assistant requires the two to match"
        )
    if not is_supervised and (addons or folders):
        reasons.append(
            "restore_addons and restore_folders need a Supervisor install; "
            "Home Assistant rejects them elsewhere"
        )
    else:
        # Supervisor logs an app or folder the backup lacks and carries on, so
        # the restore would report success without it.
        held = {a.get("slug") for a in target.get("addons") or []}
        if missing := [a for a in addons if a not in held]:
            reasons.append(f"the backup holds no apps {missing}")
        held = set(target.get("folders") or [])
        if missing := [f for f in folders if f not in held]:
            reasons.append(f"the backup holds no folders {missing}")
    if reasons:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Restore not started: " + "; ".join(reasons),
                context={"backup_id": target.get("backup_id")},
                suggestions=[
                    "List snapshots with ha_manage_backup(scope='snapshot', "
                    "action='list') to see what the backup holds"
                ],
            )
        )


async def _safety_backup(
    ws_client: HomeAssistantWebSocketClient,
    ctx: Context | None,
    *,
    password: str,
    agent_ids: list[str],
    include_database: bool,
    addons: list[str],
    folders: list[str],
) -> tuple[str, list[str]]:
    """Back up what the restore replaces; return ``(backup_id, warnings)``.

    That is the configuration, plus the database, apps and folders the restore
    takes from the backup.
    """
    safety, _, warnings = await _generate_backup(
        ws_client,
        ctx,
        name=f"PreRestore_Safety_{datetime.now().strftime('%Y-%m-%d_%H:%M:%S')}",
        password=password,
        agent_ids=agent_ids,
        include_homeassistant=True,
        include_database=include_database,
        **({"include_addons": addons} if addons else {}),
        **({"include_folders": folders} if folders else {}),
    )
    if safety.get("failed_addons") or safety.get("failed_folders"):
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "The safety backup is missing apps (add-ons) or folders the "
                "restore would overwrite; the restore was not started",
                context={"safety_backup_id": safety["backup_id"], "warnings": warnings},
            )
        )
    return safety["backup_id"], warnings


async def restore_backup(
    client: HomeAssistantClient,
    backup_id: str,
    restore_database: bool | None = None,
    ctx: Context | None = None,
    *,
    restore_addons: list[str] | None = None,
    restore_folders: list[str] | None = None,
) -> dict[str, Any]:
    """
    Restore Home Assistant from a backup (DESTRUCTIVE - use with caution).

    Creates a safety backup first when the default backup password is set.

    Args:
        client: Home Assistant REST client
        backup_id: Backup ID to restore
        restore_database: Whether to restore the database; ``None`` restores
            it when the backup contains it.
        ctx: Optional FastMCP context for progress heartbeats during the
            safety-backup and restore waits
        restore_addons: Slugs of the apps (add-ons) to restore; none by
            default, as in Home Assistant's own restore dialog
        restore_folders: Folders to restore; none by default

    Returns:
        Dictionary with restore result including safety_backup_id, status, etc.
    """
    started = asyncio.get_running_loop().time()
    addons = restore_addons or []
    folders = restore_folders or []
    safety_backup_id = None
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
            _check_restore_request(
                target,
                restore_database=restore_database,
                addons=addons,
                folders=folders,
                is_supervised=is_supervised,
            )
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
            if password:
                safety_backup_id, safety_warnings = await _safety_backup(
                    ws_client,
                    ctx,
                    password=password,
                    agent_ids=agent_ids,
                    include_database=restore_database,
                    addons=addons,
                    folders=folders,
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

            if warning := await _send_restore(
                ws_client, restore_params, ctx, started, safety_backup_id
            ):
                warnings.append(warning)
        return {
            "success": True,
            "backup_id": backup_id,
            "status": "Restore initiated - Home Assistant will restart",
            "safety_backup_id": safety_backup_id,
            "restore_database": restore_database,
            "restore_addons": addons,
            "restore_folders": folders,
            "warnings": warnings,
            "note": note,
        }

    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error restoring backup: {e}")
        context: dict[str, Any] = {"tool": "restore_backup", "backup_id": backup_id}
        suggestion = "Check Home Assistant connection and backup availability"
        if safety_backup_id:
            context["safety_backup_id"] = safety_backup_id
            suggestion = (
                "Home Assistant may be restoring: check its state and the snapshot "
                f"list before retrying; safety backup {safety_backup_id} holds the "
                "state before this call"
            )
        exception_to_structured_error(e, context=context, suggestions=[suggestion])
        return None  # unreachable: exception_to_structured_error always raises
