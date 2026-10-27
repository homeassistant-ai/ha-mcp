"""Full Home Assistant snapshots for ``ha_manage_backup(scope="snapshot")``.

Create, list and delete go through Home Assistant's own backup integration
(``backup/*`` WebSocket commands); restore is in ``backup_snapshot_restore.py``.
The tool itself is registered in ``backup.py``.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn, cast

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.websockets.exceptions import ConnectionClosed

from ..client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
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


async def _automatic_backup_settings(
    client: HomeAssistantClient, ws_client: HomeAssistantWebSocketClient
) -> tuple[dict[str, Any], bool]:
    """Return ``(create, is_supervised)`` for a new backup.

    ``create`` is the ``create_backup`` part of Home Assistant's
    automatic-backup settings: agents, contents, name and default password.
    """
    info = await ws_client.send_command("backup/config/info")
    create = cast(dict[str, Any], info["result"]["config"]["create_backup"])
    # Not the fail-open probe, and no default for a missing key: a Supervisor
    # install read as Core would pick backup.local, which Supervisor does not
    # have, and refuse the apps and folders a backup or restore names.
    config = await client.get_config()
    return create, "hassio" in config["components"]


def _require_password(create: dict[str, Any]) -> str:
    """The default backup password; ha-mcp makes no backup without it."""
    if not (password := create.get("password")):
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
    return cast(str, password)


async def _full_backup(
    client: HomeAssistantClient,
    ws_client: HomeAssistantWebSocketClient,
    ctx: Context | None,
    *,
    create: dict[str, Any],
    is_supervised: bool,
    name: str,
) -> tuple[dict[str, Any], int, list[str]]:
    """A full backup, routed the way Home Assistant routes its own before a Core update.

    With automatic-backup agents configured, it is an automatic backup:
    their contents, locations and name, and their retention counts it.
    Without them, Core rejects that command, so a manual backup goes to the
    default agent, as ``backup_core_before_update`` in hassio/backup.py does.
    Unlike Home Assistant's, it keeps the contents of the automatic-backup
    settings and the default password, so a full backup holds the same
    either way.
    """
    if create.get("agent_ids"):
        return await _generate_backup(
            ws_client, ctx, name=None, command="backup/generate_with_automatic_settings"
        )
    return await _generate_backup(
        ws_client,
        ctx,
        name=name,
        password=_require_password(create),
        agent_ids=[await _default_agent(client, is_supervised)],
        **_backup_contents(create, include_homeassistant=True),
    )


def _backup_contents(
    parts: dict[str, Any], *, include_homeassistant: bool
) -> dict[str, Any]:
    """The ``include_*`` fields of ``backup/generate`` for ``parts``.

    ``parts`` is the automatic-backup settings or a custom selection; an
    empty app or folder list is left out, as Core expects.
    """
    contents: dict[str, Any] = {
        "include_homeassistant": include_homeassistant,
        "include_database": bool(parts.get("include_database")),
        "include_all_addons": bool(parts.get("include_all_addons")),
    }
    if addons := parts.get("include_addons"):
        contents["include_addons"] = addons
    if folders := parts.get("include_folders"):
        contents["include_folders"] = folders
    return contents


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


async def _installed_apps(client: HomeAssistantClient) -> set[str]:
    """Slugs of the installed apps.

    Supervisor leaves any other slug out of a backup without reporting it.
    """
    from .tools_addons import _supervisor_api_call

    result = (await _supervisor_api_call(client, "/addons")).get("result") or {}
    return {app.get("slug") for app in result.get("addons") or []}


def _require_idle(
    queue: asyncio.Queue[dict[str, Any]], context: dict[str, Any] | None = None
) -> None:
    """Refuse unless the backup manager is idle; ``context`` goes into the error.

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
                context=context,
            )
        )


async def _generate_backup(
    ws_client: HomeAssistantWebSocketClient,
    ctx: Context | None,
    *,
    name: str | None,
    command: str = "backup/generate",
    **params: Any,
) -> tuple[dict[str, Any], int, list[str]]:
    """Run ``command`` to completion; return ``(entry, seconds, warnings)``.

    Subscribes to ``backup/subscribe_events`` before starting, as Home
    Assistant's frontend does. The job id is not the backup id on Supervisor
    installs, and the manager runs one backup at a time, so the result is the
    backup that ``backup/info`` lists after the job and did not list before
    it, named ``name``; with ``name=None`` Home Assistant names it, and an
    automatic backup of this instance (``with_automatic_settings``) is looked
    for instead, since a shared location also lists other instances' backups.
    Agents that failed the earlier listing must not be its only holders. A failed
    upload still leaves the backup on the agents whose upload worked; it is
    returned with a warning.
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
        if name is not None:
            params["name"] = name
        try:
            job = await ws_client.send_command(command, **params)
        except HomeAssistantCommandNotSent:
            raise
        except (
            HomeAssistantConnectionError,
            HomeAssistantCommandTimeout,
            OSError,
            ConnectionClosed,
        ):
            # Once sent, a lost connection or a missing reply does not prove
            # Home Assistant did not start the backup.
            raise_tool_error(
                create_error_response(
                    ErrorCode.CONNECTION_FAILED,
                    "The connection failed while sending the backup request; "
                    "Home Assistant may have started the backup",
                    suggestions=[_CHECK_LIST_SUGGESTION],
                )
            )
        job_id = job["result"]["backup_job_id"]
        outcome = await _wait_for_backup(queue, job_id, started, ctx)
    finally:
        await ws_client.release_subscription(sub_id)
    try:
        listing = await _backup_info(ws_client)
    except (
        HomeAssistantConnectionError,
        HomeAssistantCommandTimeout,
        OSError,
        ConnectionClosed,
    ):
        # The job finished, so the backup may exist although it is not listed.
        raise_tool_error(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                "The backup finished, but Home Assistant did not answer the "
                "listing that names it",
                context={"backup_job_id": job_id, "event": outcome},
                suggestions=[_CHECK_LIST_SUGGESTION],
            )
        )
    new = [
        b
        for b in listing["backups"]
        if b["backup_id"] not in known
        and (
            b.get("with_automatic_settings") is True
            if name is None
            else b.get("name") == name
        )
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
                "backups" + (f" named {name!r}" if name is not None else ""),
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


async def _check_custom_parts(
    client: HomeAssistantClient, parts: dict[str, Any], is_supervised: bool
) -> None:
    """Refuse a custom backup Home Assistant would reject, that holds nothing,
    or that names an app Supervisor would leave out."""
    reasons = []
    if not any(parts.values()):
        reasons.append("nothing is selected")
    if parts["include_database"] and not parts["include_homeassistant"]:
        reasons.append(
            "the database is part of Home Assistant's own backup; "
            "set include_homeassistant=true with it"
        )
    if parts["include_all_addons"] and parts["include_addons"]:
        reasons.append("include_all_addons and include_addons exclude each other")
    has_supervisor_parts = (
        parts["include_all_addons"]
        or parts["include_addons"]
        or parts["include_folders"]
    )
    if not is_supervised and has_supervisor_parts:
        reasons.append("apps (add-ons) and folders need a Supervisor install")
    elif parts["include_addons"]:
        installed = await _installed_apps(client)
        if missing := [a for a in parts["include_addons"] if a not in installed]:
            reasons.append(f"apps not installed: {', '.join(missing)}")
    if reasons:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Backup not started: " + "; ".join(reasons),
                context={"parts": parts},
            )
        )


async def create_backup(
    client: HomeAssistantClient,
    name: str | None = None,
    ctx: Context | None = None,
    *,
    include_homeassistant: bool | None = None,
    include_database: bool | None = None,
    include_addons: list[str] | None = None,
    include_all_addons: bool | None = None,
    include_folders: list[str] | None = None,
) -> dict[str, Any]:
    """Create a full backup, or a custom one when any ``include_*`` is given.

    A full backup is made by ``_full_backup``. A custom backup holds only the
    parts given (an unset part is left out) and goes to the default agent.

    Args:
        client: Home Assistant REST client
        name: Optional backup name (auto-generated if not provided); Home
            Assistant names an automatic backup itself
        ctx: Optional FastMCP context for progress heartbeats during the wait
        include_homeassistant, include_database, include_addons,
        include_all_addons, include_folders: the parts of a custom backup
    """
    parts = {
        "include_homeassistant": include_homeassistant,
        "include_database": include_database,
        "include_addons": include_addons,
        "include_all_addons": include_all_addons,
        "include_folders": include_folders,
    }
    custom = any(v is not None for v in parts.values())
    try:
        async with _dedicated_ws(client) as ws_client:
            create, is_supervised = await _automatic_backup_settings(client, ws_client)
            password = _require_password(create)
            # Core strips the name it stores.
            given_name = (name or "").strip()
            name = given_name or (
                f"MCP_Backup_{datetime.now().strftime('%Y-%m-%d_%H:%M:%S')}"
            )
            if custom:
                await _check_custom_parts(client, parts, is_supervised)
                entry, seconds, warnings = await _generate_backup(
                    ws_client,
                    ctx,
                    name=name,
                    password=password,
                    agent_ids=[await _default_agent(client, is_supervised)],
                    **_backup_contents(
                        parts, include_homeassistant=bool(include_homeassistant)
                    ),
                )
            else:
                entry, seconds, warnings = await _full_backup(
                    client,
                    ws_client,
                    ctx,
                    create=create,
                    is_supervised=is_supervised,
                    name=name,
                )
        if custom:
            note = "Backup uses your Home Assistant's default backup password"
        else:
            if entry.get("with_automatic_settings"):
                note = (
                    "Made with Home Assistant's automatic-backup settings (contents, "
                    "locations, name, password); their retention counts it like a "
                    "scheduled backup"
                )
                if given_name:
                    warnings.append(
                        "Home Assistant names an automatic backup itself; "
                        f"the name {given_name!r} was not used"
                    )
            else:
                note = (
                    "No automatic-backup locations are configured, so it was made "
                    "on the default location with the contents of Home Assistant's "
                    "automatic-backup settings and the default backup password"
                )
            if is_supervised and not entry.get("addons"):
                note += (
                    ". It holds no apps (add-ons); Home Assistant's automatic-backup "
                    "settings choose the apps of a full backup. Back up an app with "
                    "include_addons"
                )
        result = {
            **entry,
            "success": True,
            "duration_seconds": seconds,
            "status": "Backup completed successfully",
            "note": note,
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
