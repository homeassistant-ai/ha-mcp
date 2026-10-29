"""
Backup and restore tools for Home Assistant MCP Server.

Provides the polymorphic ``ha_manage_backup`` tool, which handles both:

* **Full HA snapshots** (``scope="snapshot"``) — the original
  ``ha_backup_create`` / ``ha_backup_restore`` functionality. Heavy
  tarball creation via HA's native backup integration; restore restarts
  HA. Last-resort recovery. Implemented in ``backup_snapshot.py`` and
  ``backup_snapshot_restore.py``.
* **Per-edit auto-backups** (``scope="edits"``) — list / view / restore
  / delete operations against the per-entity snapshots produced by the
  ``@with_auto_backup`` decorator (#1288). Lightweight, no HA restart.

The merge consolidates the previous two tools so the LLM cannot
accidentally route "restore my automation" through the heavyweight full
HA restore path. Each call must explicitly pick its scope.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Annotated, Any, Literal

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
    restore_error_code,
)
from ..backup_todo import TodoListNotLoaded
from ..client.rest_client import HomeAssistantClient
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
from .backup_snapshot import create_backup, delete_backup, list_backups
from .backup_snapshot_restore import restore_backup
from .coercion import JSON_STRING_COERCION
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
)
from .tool_hints import write_hints

if TYPE_CHECKING:
    from ha_mcp._vendor.fastmcp import FastMCP

logger = logging.getLogger(__name__)


def register_backup_tools(
    mcp: "FastMCP", client: HomeAssistantClient, **kwargs: Any
) -> None:
    """Register the polymorphic ``ha_manage_backup`` tool.

    Replaces the previous ``ha_backup_create`` and ``ha_backup_restore``
    pair (merged into one tool, separated by ``scope``). All existing
    snapshot functionality is preserved under ``scope="snapshot"``.
    """
    manage_backup_description = """Manage Home Assistant backups — both full HA snapshots AND per-edit auto-backups.

**Pick the scope first**, then the action. Wrong scope routes through the wrong code path:

| scope | action | What it does |
|---|---|---|
| `snapshot` | `create` | Without any `include_*`: a full Home Assistant backup, made as Home Assistant's own automatic backup (its contents and locations, Home Assistant Cloud included) and counted by its retention like a scheduled backup; with no automatic-backup locations configured, a manual backup on the default location with the contents of the automatic-backup settings. Either way it holds the apps those settings select: Home Assistant's backup setup selects every app, but settings that select none give a full backup with no apps (on a Supervisor install the configuration, usually its database, the `ssl` folder and any folders the settings select). The result's `addons` lists the apps it holds, and on a Supervisor install its `note` says when there are none. With any `include_*`: a custom backup of just those parts (e.g. one app; on Supervisor installs Home Assistant's configuration brings the `ssl` folder along) on the default location. Needs the default backup password. Can take a while on a large instance; progress heartbeats are sent while waiting. |
| `snapshot` | `list` | List full HA tarball snapshots as Home Assistant reports them (id, name, date, per-location size). Read-only — use to discover a `backup_id` or confirm a backup landed; a non-empty `agent_errors` means the list is partial. |
| `snapshot` | `restore` | Restore from a full HA tarball. Replaces Home Assistant's configuration plus what `restore_database` / `restore_addons` / `restore_folders` select; other apps and folders stay as they are. `safety_backup` first backs up what the restore replaces. **Restarts HA.** Last-resort recovery. |
| `snapshot` | `delete` | Delete one full HA tarball by `backup_id` (`confirm=True` required). **Disabled by default** (`enable_snapshot_delete` setting) and layered with guards even when enabled — see below. |
| `edits` | `create` | On-demand snapshot of one entity (`domain` + `entity_id` required). Use before the user manually edits in the HA UI. Same handler path the decorator takes on writes; bypasses the `enable_auto_backup` toggle. |
| `edits` | `list` | List per-entity auto-backups (lightweight). Filter by `domain` and/or `entity_id`. |
| `edits` | `view` | Read one auto-backup file by name; returns YAML and parsed `config`. |
| `edits` | `diff` | Compare one auto-backup against the entity's current config. RFC 6902 JSON-Patch + add/remove/replace counts; bounded output. Read-only — fetches the live config, makes no changes. |
| `edits` | `restore` | Re-apply one auto-backup. Existing flow helpers (template, group, utility_meter, …) and config subentries require a fresh per-entity backup; other domains follow auto-backup settings and may proceed without one. A deleted flow helper is recreated with a new config-entry ID and its saved entity IDs are restored if unoccupied; a deleted subentry is recreated with a new subentry ID. A deleted integration config entry cannot be recreated: its snapshot holds only metadata and the enabled state. **No HA restart.** |
| `edits` | `delete` | Delete one auto-backup by `backup_name`, or bulk-delete by filter. |

**When to use which scope:**
- `scope="edits"`: ha-mcp's own per-entity YAML backups of automations, scripts, scenes, dashboards and helpers. Use them to undo a recent edit by the agent. Lightweight, fast, no restart.
- `scope="snapshot"`: Home Assistant's own backups — a full backup (`create` without `include_*`), or a custom backup of some parts, such as one or more apps (`include_addons`). Restore one only for system-wide recovery (mass config corruption, a broken app update with that app in `restore_addons`, etc.).

**A snapshot backup is the user's call:** before a change that cannot be undone, you may ask the user whether they want one first; make it only when they say so. A full backup is large, and an unrequested one can push an older backup out of retention.

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
- Full backup, when the user asks for one: `ha_manage_backup(scope="snapshot", action="create")`
- Backup of one app: `ha_manage_backup(scope="snapshot", action="create", include_addons=["core_mosquitto"])`
- List snapshots (to discover a backup_id or confirm one landed): `ha_manage_backup(scope="snapshot", action="list")`
- Restore a snapshot's configuration (the database follows the backup): `ha_manage_backup(scope="snapshot", action="restore", backup_id="dd7550ed")`
- Restore a snapshot with one of its apps: `ha_manage_backup(scope="snapshot", action="restore", backup_id="dd7550ed", restore_addons=["core_mosquitto"])`
- Restore after a backup of what it replaces, when the user chose one: `ha_manage_backup(scope="snapshot", action="restore", backup_id="dd7550ed", safety_backup=True)`
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
                description="(snapshot.create) Tarball name. Auto-generated if not provided. "
                "Home Assistant names a full backup made with its automatic-backup settings itself.",
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
                "restore it when the backup contains it. Must be false for a backup "
                "without one; on Supervisor installs it must match the backup.",
            ),
        ] = None,
        restore_addons: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description="(snapshot.restore) Slugs of apps (add-ons) in the backup to "
                "restore, from its `addons` in the snapshot list. Default: none. "
                "Supervisor installs only.",
            ),
        ] = None,
        restore_folders: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description="(snapshot.restore) Folders in the backup to restore, from its "
                "`folders` in the snapshot list (`share`, `addons/local`, `ssl`, `media`). "
                "Default: none. Supervisor installs only.",
            ),
        ] = None,
        include_homeassistant: Annotated[
            bool | None,
            Field(
                default=None,
                description="(snapshot.create) Custom backup: include Home Assistant's "
                "configuration.",
            ),
        ] = None,
        include_database: Annotated[
            bool | None,
            Field(
                default=None,
                description="(snapshot.create) Custom backup: include the database "
                "(history); needs include_homeassistant=True.",
            ),
        ] = None,
        include_addons: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description="(snapshot.create) Custom backup: slugs of the installed apps "
                "(add-ons) to include. Supervisor installs only.",
            ),
        ] = None,
        include_all_addons: Annotated[
            bool | None,
            Field(
                default=None,
                description="(snapshot.create) Custom backup: include every app (add-on); "
                "not together with include_addons. Supervisor installs only.",
            ),
        ] = None,
        include_folders: Annotated[
            list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description="(snapshot.create) Custom backup: folders to include "
                "(`share`, `addons/local`, `ssl`, `media`). Supervisor installs only.",
            ),
        ] = None,
        safety_backup: Annotated[
            bool,
            Field(
                default=False,
                description="(snapshot.restore) Back up what the restore replaces "
                "(configuration, plus the database, apps and folders it restores) first, "
                "as a manual backup on the default location. Off by default; offer it "
                "to the user, who decides. Needs the default backup password.",
            ),
        ] = False,
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
                restore_addons=restore_addons,
                restore_folders=restore_folders,
                safety_backup=safety_backup,
                parts={
                    "include_homeassistant": include_homeassistant,
                    "include_database": include_database,
                    "include_addons": include_addons,
                    "include_all_addons": include_all_addons,
                    "include_folders": include_folders,
                },
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
    restore_addons: list[str] | None,
    restore_folders: list[str] | None,
    safety_backup: bool,
    parts: dict[str, Any],
    confirm: bool,
    limit: int,
    ctx: Context | None,
) -> dict[str, Any]:
    """Route a ``scope="snapshot"`` action to its full-tarball handler.

    ``parts`` holds the ``include_*`` parameters of a custom backup.
    """
    if action == "create":
        return await create_backup(client, name, ctx=ctx, **parts)
    if action == "list":
        return await list_backups(client, limit)
    if action == "delete":
        bid = _require("backup_id", backup_id, scope, action)
        return await delete_backup(client, bid, confirm, ctx=ctx)
    # action == "restore"
    bid = _require("backup_id", backup_id, scope, action)
    return await restore_backup(
        client,
        bid,
        restore_database,
        ctx=ctx,
        restore_addons=restore_addons,
        restore_folders=restore_folders,
        safety_backup=safety_backup,
    )


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
    except TodoListNotLoaded as err:
        raise_tool_error(
            create_error_response(
                ErrorCode.ENTITY_UNAVAILABLE,
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
        code = restore_error_code(err)
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
