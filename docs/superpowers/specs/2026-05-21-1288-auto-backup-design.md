# Auto-backup edited entities (closes #1288)

**Status:** approved 2026-05-21 (user override on spec-review-subagent and writing-plans gates — direct directive to execute to draft PR)
**Issue:** [#1288](https://github.com/homeassistant-ai/ha-mcp/issues/1288)
**Branch:** `1288-auto-backup` (worktree `worktree/1288-auto-backup/`)

## Goal

Capture per-entity backups before every write/destructive MCP tool call,
expose them for LLM-driven and UI-driven inspection, restore, and delete.
Works in every ha-mcp deployment mode (addon, Docker, uvx) with no custom
component dependency and no addon manifest privilege change.

## Non-goals

- Full HA snapshot rollback — `ha_backup_create` / `ha_backup_restore` already cover that
- Cross-entity transactional restore (each restore is one entity)
- Preserving user comments / whitespace from source YAML files (API-derived format)

## Settings

| Name | Env var | Default | Purpose |
|---|---|---|---|
| `enable_auto_backup` | `ENABLE_AUTO_BACKUP` | `False` | Master toggle |
| `auto_backup_throttle_minutes` | `AUTO_BACKUP_THROTTLE_MINUTES` | `0` | Per-entity throttle window; 0 = no throttle, backup every write |
| `auto_backup_retain_per_entity` | `AUTO_BACKUP_RETAIN_PER_ENTITY` | `20` | Max snapshots kept per entity; oldest rotated out |
| `auto_backup_dir` | `HAMCP_BACKUP_DIR` | `""` (auto) | Override backup directory |

Auto-default for `auto_backup_dir`:
- Addon (`SUPERVISOR_TOKEN` set + `/data` exists): `/data/ha_mcp_backups/`
- Else (Docker/uvx): `${XDG_DATA_HOME:-~/.local/share}/ha_mcp/backups/`

## Architecture

```
write/destructive tool  →  @with_auto_backup  →  BackupManager.maybe_snapshot
                                                       │
                                                       ├── DomainHandler.fetch (HA REST/WS)
                                                       └── write yaml, rotate, return path

ha_manage_auto_backup tool  ─┐
/api/settings/backups        ├──→  BackupManager  (list/read/diff/restore/delete)
Settings UI Backups tab      ─┘
```

**`BackupManager`** (single instance per server, cached on the client object) owns:
- Backup dir resolution and creation
- Per-entity-key `asyncio.Lock` map to serialize fetch+write
- Per-entity-key `last_snapshot_ts` for throttle
- Domain handler registry: `{domain: DomainHandler}`
- File operations (list, read, diff, write, delete, rotate)

**`DomainHandler`** is a frozen dataclass per backup-domain:
```python
@dataclass(frozen=True)
class DomainHandler:
    domain: str                                              # "automation", "helper_input_boolean", "dashboard", ...
    fetch: Callable[[Client, str, dict], Awaitable[Any]]    # (client, entity_id, tool_kwargs) -> current config
    restore: Callable[[Client, str, Any, dict], Awaitable[Any]]  # (client, entity_id, config, restore_kwargs) -> result
```

One handler per backed-up domain; helper types each get their own handler keyed `helper_<type>` so each helper type lists/restores independently.

## Write flow (the decorator)

```python
@with_auto_backup(domain="automation", id_param="identifier")
@mcp.tool(...)
@log_tool_usage
async def ha_config_set_automation(self, identifier: str, ...):
    ...
```

`id_param` names the kwarg that carries the entity ID. For helpers, the
decorator takes `domain_fn` instead — a callable that computes the domain
key from kwargs (so `helper_type="timer"` → domain `helper_timer`).

Decorator behavior (best-effort, never raises):
1. Look up manager off `self._client._auto_backup_manager` (lazy-built on first use).
2. If `enable_auto_backup` is False → call wrapped function and return.
3. Resolve `entity_key = f"{domain}:{entity_id}"`. If entity ID is `None`/empty (a *create* call with no ID) → skip backup, call wrapped function.
4. Inside per-key lock: check throttle; if elapsed, fetch + write + rotate.
5. All exceptions from steps 3-4 are logged as WARNING and swallowed.
6. Call wrapped function with original args.

**Why this shape:** the original tool body is fully decoupled from backup;
removing the decorator should not change tool behavior. Backup is never
on the critical path of the write.

## File format

```yaml
# ha_mcp_backup
schema_version: 1
domain: automation
entity_id: kitchen_lights
captured: 2026-05-21T15:30:00+00:00
tool: ha_config_set_automation
config:
  alias: Kitchen lights
  trigger: ...
  action: ...
```

Filename: `<domain>.<safe_entity_id>.<YYYYMMDD_HHMMSS>.yaml`. `safe_entity_id`
replaces `os.sep` and any non-`[A-Za-z0-9._-]` character with `_`. Backup
dir is flat (no per-domain subdirs) so glob retention is simple.

## Restore flow

User-facing entry points (both call `BackupManager.restore`):
- LLM: `ha_manage_auto_backup(action="restore", name=...)`
- UI: `POST /api/settings/backups/<name>/restore`

`BackupManager.restore(name)`:
1. Load file, parse YAML, validate schema (version, required keys).
2. Resolve `DomainHandler` from `domain` field; if missing → return RESTORE_FAILED with suggestions.
3. Take a fresh *safety backup* of the entity's CURRENT state via the same path as the decorator (`maybe_snapshot` with throttle disabled). The safety backup is recorded so the user can undo the restore.
4. Call `handler.restore(client, entity_id, config)` (which under the hood calls the same HA REST/WS endpoint the original `set_*` tool uses).
5. Return `{success, entity_key, safety_backup, restored_from}`.

Restore is upsert: re-creates entities that were deleted between backup and restore. If HA rejects (validation error, etc.), error propagates with structured suggestions.

## Polymorphic `ha_manage_backup` tool

`ha_backup_create` and `ha_backup_restore` are **merged** into a single
polymorphic tool `ha_manage_backup` that handles BOTH the existing full-HA-
snapshot functionality AND the new per-edit auto-backup operations. Net tool
count change: **-1** (remove 2 tools, add 1).

```python
ha_manage_backup(
    scope: Literal["snapshot", "edits"],
    action: Literal["create", "restore", "list", "view", "delete"],
    # snapshot scope params
    name: str | None = None,          # snapshot.create: tarball name
    backup_id: str | None = None,     # snapshot.restore: tarball ID
    restore_database: bool = False,   # snapshot.restore: include DB
    # edits scope params
    domain: str | None = None,        # edits.list / edits.delete: filter
    entity_id: str | None = None,     # edits.list / edits.delete: filter
    backup_name: str | None = None,   # edits.view / edits.restore / edits.delete
    older_than_days: int | None = None,  # edits.delete: bulk-by-age
)
```

### Routing matrix

| scope | action | Behavior |
|---|---|---|
| `snapshot` | `create` | Existing `ha_backup_create` behavior — full HA tarball via HA's native backup integration |
| `snapshot` | `restore` | Existing `ha_backup_restore` behavior — HA restarts, pre-restore safety tarball created automatically |
| `edits` | `list` | List auto-backup files filterable by `domain` and/or `entity_id` |
| `edits` | `view` | Return one auto-backup's YAML content + parsed `config` |
| `edits` | `restore` | Re-apply one auto-backup (creates fresh safety snapshot first); no HA restart |
| `edits` | `delete` | Delete one auto-backup by name OR bulk-delete by filter (domain/entity_id/older_than_days) |

### Gating against accidental wrong-mode usage

This is the main design risk — without strong gates, the LLM could route
"restore my automation" through `(scope="snapshot", action="restore")`,
which would restart HA. Layered defenses:

1. **Type validation**: `scope` and `action` are `Literal`-typed so Pydantic
   rejects unknown values before the tool body runs.
2. **Scope+action matrix validation**: invalid combinations
   (`(snapshot, list)`, `(snapshot, view)`, `(snapshot, delete)`,
   `(edits, create)`) raise `VALIDATION_INVALID_PARAMETER` with the valid
   combinations listed in `suggestions`.
3. **Required-param checks per cell**:
   - `(snapshot, restore)` requires `backup_id`; if `backup_name` or
     `entity_id` is passed instead, structured error explains the param
     belongs to the other scope.
   - `(edits, restore/view/delete-single)` requires `backup_name` (which
     follows the `<domain>.<entity_id>.<timestamp>.yaml` shape — clearly
     not a tarball ID).
4. **Annotation differentiation**:
   - `(snapshot, restore)` keeps the existing `destructiveHint: True` AND
     surfaces the "LAST RESORT — HA will restart" warning in the response.
   - `(edits, restore)` is also destructive but explicitly safer; response
     includes `restart_required: false` and the path of the safety backup
     created.
5. **Docstring**: the tool's docstring leads with a routing table so the
   LLM picks the right cell. Per-scope sections clearly state what each
   does and how they differ.

### UI mapping

| Tool call | UI endpoint | UI element |
|---|---|---|
| `(edits, list)` | `GET /api/settings/backups?...` | Backups tab list table with filters |
| `(edits, view)` | `GET /api/settings/backups/<name>` | "View" button → modal showing YAML |
| (no tool — UI-only diff) | `GET /api/settings/backups/<name>/diff` | "Diff" button → modal with unified diff vs current state |
| `(edits, restore)` | `POST /api/settings/backups/<name>/restore` | "Restore" button (confirmation modal) |
| `(edits, delete)` | `DELETE /api/settings/backups/<name>` and `DELETE /api/settings/backups?...` | Per-row "Delete" + bulk "Delete matching filters" |

**List item shape:**
```json
{
  "name": "automation.kitchen_lights.20260521_153000.yaml",
  "domain": "automation",
  "entity_id": "kitchen_lights",
  "captured": "2026-05-21T15:30:00+00:00",
  "tool": "ha_config_set_automation",
  "size": 412
}
```

**Diff:** `difflib.unified_diff` against the current entity's config (fetched via the same handler.fetch as backup), yielding text diff.

## Settings UI changes

`settings_ui.py` gains:
- 5 new routes (list / view / diff / restore / delete + bulk delete)
- A "Backups" tab in the existing `/settings` HTML page — list table with filter inputs (domain, entity, since), each row with View / Diff / Restore / Delete buttons, plus a "Bulk delete matching" action
- A small JS section at the bottom of the existing inline script tag

## Tool surface (28 wrapped)

`ha_config_set_automation`, `ha_config_remove_automation`,
`ha_config_set_script`, `ha_config_remove_script`,
`ha_config_set_scene`, `ha_config_remove_scene`,
`ha_config_set_helper` (one DomainHandler per helper_type, dispatched via `domain_fn`),
`ha_config_set_dashboard`, `ha_config_delete_dashboard`,
`ha_config_set_dashboard_resource`, `ha_config_delete_dashboard_resource`,
`ha_config_set_label`, `ha_config_remove_label`,
`ha_config_set_category`, `ha_config_remove_category`,
`ha_config_set_group`, `ha_config_remove_group`,
`ha_config_set_calendar_event`, `ha_config_remove_calendar_event`,
`ha_set_zone`, `ha_remove_zone`,
`ha_set_area_or_floor`, `ha_remove_area_or_floor`,
`ha_set_todo_item`, `ha_remove_todo_item`,
`ha_set_entity`,
`ha_set_integration_enabled`, `ha_delete_helpers_integrations`.

**Explicitly NOT wrapped:** `ha_call_service`, `ha_call_event`, `ha_restart`, `ha_reload_core`, `ha_check_config`, `ha_eval_template`, `ha_delete_file`, `ha_remove_entity`, `ha_remove_device`, `ha_update_device`, `ha_install_mcp_tools`, `ha_hacs_*`, blueprint/import ops.

## Error handling

- Backup write errors: WARNING log, original write proceeds.
- Restore errors: structured `RESTORE_FAILED` error response with suggestions.
- Schema-version mismatch on restore: structured `BACKUP_INCOMPATIBLE` error.
- Filesystem init errors (backup dir uncreatable at startup): single startup WARNING, manager runs with snapshots silently skipped until restart.
- Concurrent writes to same entity: per-entity `asyncio.Lock` serializes; no duplicate snapshots within one window.

## Testing strategy

**Unit (`tests/src/unit/test_backup_manager.py`):**
- Throttle math (boundary at `auto_backup_throttle_minutes=0` always snapshots; >0 enforces window)
- Retention rotation (`auto_backup_retain_per_entity` enforced; oldest deleted)
- Filename safety (path separators, special chars, unicode)
- Diff output format
- Schema version validation on restore
- Best-effort error handling (mocked fetch raises → wrapped tool still runs)

**E2E (`tests/src/e2e/workflows/auto_backup/`):**
One test file per backed-up domain (`test_automation.py`, `test_script.py`, ..., one per `DomainHandler`). Each test:
1. Toggle on
2. Create/exist entity
3. Edit via the wrapped tool → backup file appears
4. Edit again → another backup file appears (with throttle off) OR no new file (with throttle on, within window)
5. Restore first backup via `ha_manage_auto_backup(action="restore", ...)` — entity returns to original state
6. Verify a safety backup was created
7. Delete via `ha_manage_auto_backup(action="delete", ...)` — file removed
8. Toggle off → next edit produces no backup

## Files

**New:**
- `src/ha_mcp/backup_manager.py`
- `src/ha_mcp/tools/auto_backup.py` (decorator)
- `tests/src/unit/test_backup_manager.py`
- `tests/src/e2e/workflows/auto_backup/` (one test file per domain)

**Modified:**
- `src/ha_mcp/config.py` (4 settings)
- `src/ha_mcp/tools/backup.py` — merge `ha_backup_create`+`ha_backup_restore` into a single `ha_manage_backup` polymorphic tool; add the `edits` scope handlers
- `src/ha_mcp/settings_ui.py` (5 routes + Backups tab)
- `homeassistant-addon/config.yaml` + `homeassistant-addon-dev/config.yaml` (4 options + schema)
- `homeassistant-addon-dev/translations/en.yaml` (4 translations)
- 28 `tools_*.py` modules (one `@with_auto_backup(...)` line each above existing `@mcp.tool(...)`)
