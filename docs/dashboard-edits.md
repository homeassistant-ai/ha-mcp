# Dashboard edits

`ha_config_set_dashboard` supports full configuration replacement, Python
transforms, and structured patches. Existing calls remain supported. All modes
retain the tool's backup, best-practice, and optional screenshot behavior.

## Structured patches

Read the dashboard with `ha_config_get_dashboard`, then pass its `config_hash`
and a list of operations:

```json
{
  "url_path": "my-dashboard",
  "config_hash": "<hash from the latest read>",
  "patch": [
    {"op": "test", "path": "/views/0/title", "value": "Home"},
    {"op": "replace", "path": "/views/0/title", "value": "Living room"},
    {"op": "add", "path": "/views/0/cards/-", "value": {"type": "tile", "entity": "light.living_room"}}
  ]
}
```

Supported operations are `add`, `remove`, `replace`, and `test`, with at most
100 operations per request. Paths use JSON Pointer: escape `~` as `~0` and `/`
as `~1`; use `-` to append to an array. Operations run in order, so deleting an
array entry shifts later indices. A failed operation rejects the entire patch
before saving. Values, including card templates and JavaScript strings, stay
literal. `move` and `copy` are not supported.

Use patches for known locations and Python transforms for loops or pattern-based
changes. Supply exactly one of `patch`, `python_transform`, or `config`. Patches
and Python transforms require a hash; full replacements retain their optional
hash check. A stale hash rejects the configuration change. Read again before
retrying. An unchanged patch does not save or emit a dashboard update.

Patch requests cannot include sidebar metadata (`title`, `icon`, `require_admin`,
`show_in_sidebar`); that combination is rejected before editing the dashboard.
Update metadata in a separate `ha_config_set_dashboard` call without `patch`,
`config`, or `python_transform`.

## Native Home Assistant support

When HA MCP Tools advertises the `dashboard_edit` capability, storage dashboard
edits use Home Assistant's own Lovelace API in-process. Structured patches combine
loading, hash comparison, editing, saving, and readback in one WebSocket command.
Python continues to run in HA-MCP's existing sandbox; its final comparison, save,
and readback use the same native command. Component-less and older-component
installations use the existing WebSocket path, including for structured patches.
No File & YAML services permission is needed for this capability.

The native command compares the loaded configuration and begins Core's save
without yielding between them. It preserves Core's cache invalidation and update
event. A later dashboard editor can still overwrite that change. Readback returns
the current configuration hash, which can reflect such a later edit.

A save interrupted after dispatch reports an unknown outcome and is never
retried automatically. Read the dashboard to determine whether the change took
effect. `write_committed` describes the overall tool call: `true` means at least
one write was acknowledged, `false` means no write was made, and `null` means
the outcome is unknown. `post_write_verified` indicates whether the dashboard
configuration was read back successfully; `config_hash` is null when it was not.
These fields are returned for successful full replacements on either backend.

A full replacement can update sidebar metadata or create a dashboard before
saving its config. If the config phase fails after that earlier write,
`write_committed` remains `true`, while `dashboard_created`/`metadata_updated`
identify what already succeeded. `config_write_committed` separately records
whether the config write succeeded (`true`), did not happen (`false`), or has an
unknown outcome (`null`). In this case `reason` describes the config failure;
`reason="write_outcome_unknown"` with `write_committed=true` and
`config_write_committed=null` means the earlier registry write succeeded but
the config outcome is unknown. Read the dashboard before retrying either part.

A successful API save is not a guarantee of durable disk storage: Core logs
some persistence errors.
YAML dashboards and conversion away from a strategy dashboard remain protected.
