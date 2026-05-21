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
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..backup_manager import get_backup_manager
from ..client.rest_client import HomeAssistantClient
from ..client.websocket_client import HomeAssistantWebSocketClient
from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    get_connected_ws_client,
    log_tool_usage,
    raise_tool_error,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)

_BACKUP_MAX_WAIT_S = 120
_BACKUP_POLL_INTERVAL_S = 2


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


async def _poll_backup_completion(
    ws_client: HomeAssistantWebSocketClient,
    name: str,
    backup_job_id: str,
    max_wait_seconds: int,
    poll_interval: int,
    agent_id: str,
) -> dict[str, Any]:
    """Poll backup/info until the named backup completes, fails, or times out.

    ``agent_id`` is the local agent that owns this backup (e.g.
    ``hassio.local`` on Supervised, ``backup.local`` on Core); used to look
    up the per-agent size in the backup-info payload.

    Raises ToolError on backup failure or timeout.
    """
    waited = 0

    while waited < max_wait_seconds:
        await asyncio.sleep(poll_interval)
        waited += poll_interval

        info_result = await ws_client.send_command("backup/info")
        if info_result.get("success"):
            state = info_result.get("result", {}).get("state")
            last_event = info_result.get("result", {}).get("last_action_event", {})
            event_state = last_event.get("state")

            logger.debug(
                f"Backup state: {state}, event_state: {event_state}, waited: {waited}s"
            )

            if state == "idle" and event_state == "completed":
                backups = info_result.get("result", {}).get("backups", [])
                created_backup = None
                for backup in backups:
                    if backup.get("name") == name:
                        created_backup = backup
                        break

                if created_backup:
                    logger.info(
                        f"Backup completed successfully: {created_backup.get('backup_id')}"
                    )
                    return {
                        "success": True,
                        "backup_id": created_backup.get("backup_id"),
                        "backup_job_id": backup_job_id,
                        "name": name,
                        "date": created_backup.get("date"),
                        "size_bytes": created_backup.get("agents", {})
                        .get(agent_id, {})
                        .get("size"),
                        "status": "Backup completed successfully",
                        "duration_seconds": waited,
                        "note": "Backup uses your Home Assistant's default backup password",
                    }
                else:
                    logger.warning(
                        "Backup completed but not found in backup list yet, waiting..."
                    )
                    continue

            elif event_state == "failed":
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        "Backup creation failed",
                        context={"backup_job_id": backup_job_id},
                    )
                )

    logger.warning(f"Backup did not complete within {max_wait_seconds} seconds")
    raise_tool_error(
        create_error_response(
            ErrorCode.TIMEOUT_OPERATION,
            f"Backup creation timed out after {max_wait_seconds} seconds",
            context={"backup_job_id": backup_job_id, "name": name},
            suggestions=[
                "Backup may still be in progress. Check Home Assistant backup status."
            ],
        )
    )


async def create_backup(
    client: HomeAssistantClient, name: str | None = None
) -> dict[str, Any]:
    """
    Create a fast Home Assistant backup (local only, excludes database).

    Args:
        client: Home Assistant REST client
        name: Optional backup name (auto-generated if not provided)

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
) -> str | None:
    """Create a pre-restore safety backup.

    ``agent_id`` is the local backup agent (Supervisor's ``hassio.local`` or
    Core's ``backup.local``) discovered by the caller.

    Returns the safety backup ID, or None when password is None (backup intentionally
    skipped). Raises ToolError if backup creation fails.
    """
    if password is None:
        return None

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
    logger.info(f"Safety backup created: {safety_backup_id}")
    return cast(str, safety_backup_id)


async def restore_backup(
    client: HomeAssistantClient, backup_id: str, restore_database: bool = False
) -> dict[str, Any]:
    """
    Restore Home Assistant from a backup (DESTRUCTIVE - use with caution).

    Creates a safety backup before restore to allow rollback if needed.

    Args:
        client: Home Assistant REST client
        backup_id: Backup ID to restore
        restore_database: Whether to restore database (historical data)

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
        backup_exists = any(b.get("backup_id") == backup_id for b in backups)

        if not backup_exists:
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

        safety_backup_id = await _create_safety_backup(ws_client, password, local_agent)

        # Perform restore
        restore_params = {
            "backup_id": backup_id,
            "agent_id": local_agent,
            "restore_database": restore_database,
            "restore_homeassistant": True,
            "restore_addons": [],  # Restore all addons from backup
            "restore_folders": [],  # Restore all folders from backup
        }

        result = await ws_client.send_command("backup/restore", **restore_params)

        if result.get("success"):
            return {
                "success": True,
                "backup_id": backup_id,
                "status": "Restore initiated - Home Assistant will restart",
                "safety_backup_id": safety_backup_id,
                "restore_database": restore_database,
                "warnings": [
                    "Home Assistant is restarting. Connection will be temporarily lost."
                ],
                "note": "A safety backup was created before restore. You can restore from it if needed.",
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


# Valid (scope, action) combinations. Anything outside this set is
# rejected with a structured VALIDATION_INVALID_PARAMETER error.
_VALID_COMBOS: set[tuple[str, str]] = {
    ("snapshot", "create"),
    ("snapshot", "restore"),
    ("edits", "list"),
    ("edits", "view"),
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
| `snapshot` | `create` | Create a full HA tarball (config + addons, no DB by default). Heavy, seconds-long. |
| `snapshot` | `restore` | Restore a full HA tarball. **Restarts HA.** Last-resort recovery. |
| `edits` | `list` | List per-entity auto-backups (lightweight). Filter by `domain` and/or `entity_id`. |
| `edits` | `view` | Read one auto-backup file by name; returns YAML and parsed `config`. |
| `edits` | `restore` | Re-apply one auto-backup. Creates a fresh safety snapshot first. **No HA restart.** |
| `edits` | `delete` | Delete one auto-backup by `backup_name`, or bulk-delete by filter. |

**When to use which scope:**
- Use `scope="edits"` to undo a recent automation/script/scene/dashboard/helper edit by the agent. Lightweight, fast, no restart.
- Use `scope="snapshot"` only for system-wide recovery (botched add-on update, mass config corruption, etc.).

**`scope="snapshot"` backup-hint:**
{backup_hint_text}

**`scope="edits"` requires `enable_auto_backup=true`** in settings to populate the backup directory in the first place. If the listing is empty, check that the toggle is on (web settings UI or `ENABLE_AUTO_BACKUP=true` env var).

**Examples:**
- Snapshot before risky op: `ha_manage_backup(scope="snapshot", action="create", name="Before_Big_Change")`
- Restore full snapshot: `ha_manage_backup(scope="snapshot", action="restore", backup_id="dd7550ed")`
- List recent auto-backups for one automation: `ha_manage_backup(scope="edits", action="list", domain="automation", entity_id="kitchen_lights")`
- View an auto-backup: `ha_manage_backup(scope="edits", action="view", backup_name="automation.kitchen_lights.20260521_153000.yaml")`
- Restore an auto-backup: `ha_manage_backup(scope="edits", action="restore", backup_name="automation.kitchen_lights.20260521_153000.yaml")`
- Delete one auto-backup: `ha_manage_backup(scope="edits", action="delete", backup_name="...")`
- Bulk-delete old auto-backups: `ha_manage_backup(scope="edits", action="delete", older_than_days=30)`
"""

    @mcp.tool(
        description=manage_backup_description,
        tags={"System"},
        annotations={"destructiveHint": True, "title": "Manage Backups"},
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
            Literal["create", "restore", "list", "view", "delete"],
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
                description="(snapshot.restore) Tarball ID to restore (e.g. 'dd7550ed').",
            ),
        ] = None,
        restore_database: Annotated[
            bool,
            Field(
                default=False,
                description="(snapshot.restore) Include database in the restore. Default false (config-only).",
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
                description="(edits.list) Maximum number of entries to return.",
            ),
        ] = 200,
    ) -> dict[str, Any]:
        """Polymorphic backup tool. See the tool description for the routing matrix."""
        _gate_combo(scope, action)

        if scope == "snapshot":
            if action == "create":
                return await create_backup(client, name)
            # action == "restore"
            bid = _require("backup_id", backup_id, scope, action)
            return await restore_backup(client, bid, restore_database)

        # scope == "edits"
        settings = get_global_settings()
        mgr = get_backup_manager(client, settings)

        if action == "list":
            # list_snapshots does sync directory globbing + per-file stat;
            # offload to the executor so it doesn't block the event loop
            # when the backup dir holds many files.
            entries = await asyncio.to_thread(
                mgr.list_snapshots,
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
