"""Entity backups: the registry settings ``ha_set_entity`` and ``ha_remove_entity`` change.

Both tools write the entity registry, so the snapshot is the entity's registry
entry, cut to the fields a restore writes back, its options (which hold
voice-assistant exposure) included. The entry's ``id`` stays the same when the
entity is renamed, so the restore finds a renamed entity and gives it its old
entity_id back; the safety backup and the diff follow it the same way. A
removed entity has no entry to update: Core keeps its settings while its config
entry exists, and for 30 days after that entry, or an entity without one, was
removed, and gives them back when the integration adds the entity again; this
backup can then be restored over it.
The snapshot leaves out the device name, which the device registry holds.
"""

from __future__ import annotations

import json
from typing import Any

from ._vendor.fastmcp.exceptions import ToolError
from .backup_entity_ids import _manager, entity_id_locks
from .client.rest_client import HomeAssistantCommandError
from .errors import ErrorCode
from .tools.config_helpers.registry import validate_registry_ids

_SNAPSHOT_FIELDS = (
    "id",
    "entity_id",
    "name",
    "icon",
    "area_id",
    "device_class",
    "aliases",
    "labels",
    "categories",
    "hidden_by",
    "disabled_by",
    "options",
)
_NOT_RESTORED = "The device name is not part of an entity backup and was left as it is."


async def fetch_entity(client: Any, entity_id: str) -> dict[str, Any] | None:
    """The registry fields of ``entity_id``; None when it has no registry entry."""
    try:
        entry = await _manager()._ws_send(
            client, {"type": "config/entity_registry/get", "entity_id": entity_id}
        )
    except HomeAssistantCommandError as err:
        if err.code != "not_found":
            raise
        return None
    entry = _manager()._require_dict(entry, "config/entity_registry/get")
    return {field: entry.get(field) for field in _SNAPSHOT_FIELDS}


def _entry_id(entity_id: str, config: Any) -> str:
    """The snapshot's registry entry ``id``. A backup from before entity
    backups held registry settings has none; it is refused before anything,
    a safety backup included, is written."""
    entry_id = config.get("id") if isinstance(config, dict) else None
    if isinstance(entry_id, str):
        return entry_id
    raise _manager().InvalidBackupSnapshotError(
        f"The backup of {entity_id} holds its state from an earlier ha-mcp "
        "version, not its registry settings, so it cannot be restored"
    )


async def _entry_by_id(client: Any, entry_id: str) -> dict[str, Any] | None:
    rows = await _manager()._entity_registry_rows(client)
    return next((row for row in rows if row.get("id") == entry_id), None)


async def current_entity_id(client: Any, entity_id: str, config: Any) -> str | None:
    """The entity_id the snapshot's entity has now, found by its entry ``id``
    since a rename; None when the entry is gone."""
    entry = await _entry_by_id(client, _entry_id(entity_id, config))
    return entry["entity_id"] if entry else None


async def restore_entity(client: Any, entity_id: str, config: Any) -> Any:
    """Write the snapshot's registry fields back, under the entity ID it saved."""
    bm = _manager()
    entry_id = _entry_id(entity_id, config)
    current = await _entry_by_id(client, entry_id)
    if current is None:
        raise bm.BackupRestoreError(
            f"{entity_id} is no longer in the entity registry",
            reason="entry_deleted",
            suggestions=[
                (
                    "Restart Home Assistant: if what provides the entity is "
                    "still there, it adds the entity again with the registry "
                    "settings it had while its config entry exists, and for "
                    "30 days after that entry, or an entity without one, was "
                    "removed. A reload does not bring back a stored helper's "
                    "entity"
                ),
                (
                    "If this backup is from an earlier edit, restore it once "
                    "the entity is back"
                ),
            ],
        )
    source, target = current["entity_id"], config["entity_id"]
    async with entity_id_locks(source, target):
        current = await fetch_entity(client, source)
        if current is None or current["id"] != entry_id:
            raise bm.BackupRestoreError(
                f"{entity_id} changed while waiting to restore it; retry",
                reason="entity_identity_mismatch",
                verification_status="mismatched",
            )
        if source != target:
            await bm._check_entity_collision(client, target)
        await _check_references(client, config)
        return await _write_back(client, source, target, current, config)


async def _check_references(client: Any, config: dict[str, Any]) -> None:
    """Refuse a backup naming an area, label or category deleted since: Core
    would store the area or category ID as sent and drop the label silently.
    The check is the one ha_set_entity runs."""
    try:
        await validate_registry_ids(
            client,
            config.get("area_id"),
            config.get("labels"),
            config.get("categories"),
            fail_closed=True,
        )
    except ToolError as err:
        error = json.loads(str(err)).get("error") or {}
        if error.get("code") != ErrorCode.VALIDATION_INVALID_PARAMETER:
            raise
        raise _manager().BackupRestoreError(
            f"{error.get('message')} Nothing was restored.",
            reason="missing_reference",
            suggestions=[
                "Create the area, label or category again, then restore; or "
                "set the entity with ha_set_entity instead"
            ],
        ) from err


async def _write_back(
    client: Any,
    source: str,
    target: str,
    current: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    update: dict[str, Any] = {
        field: config.get(field)
        for field in ("name", "icon", "area_id", "device_class", "aliases", "labels")
    }
    # Core updates only the scopes it is sent: clear the ones added since.
    update["categories"] = {
        **dict.fromkeys(current.get("categories") or {}),
        **(config.get("categories") or {}),
    }
    if source != target:
        update["new_entity_id"] = target
    result = await _update(client, source, update)
    warnings = [_NOT_RESTORED]
    # Each on its own and only when changed. In Core 2026.10 an update
    # carrying disabled_by None asks for a restart when the entity has no
    # config entry or one that cannot unload; Core refuses values only Home
    # Assistant itself sets, and enabling an entity whose device is disabled.
    for field in ("hidden_by", "disabled_by"):
        saved, now = config.get(field), current.get(field)
        if saved == now:
            continue
        try:
            result = {**result, **await _update(client, target, {field: saved})}
        except HomeAssistantCommandError as err:
            if err.code not in ("invalid_format", "invalid_info"):
                raise
            warnings.append(
                f"{field} stays {now!r}: Home Assistant refused {saved!r}: {err}"
            )
    saved_options = config.get("options") or {}
    now_options = current.get("options") or {}
    for domain in sorted({*saved_options, *now_options}):
        saved, now = saved_options.get(domain), now_options.get(domain)
        if saved == now:
            continue
        expose = (saved or {}).get("should_expose")
        if isinstance(expose, bool) and expose != (now or {}).get("should_expose"):
            # Ahead of the options write: Core tells the assistant only when
            # this command is the one that changes the value.
            await _manager()._ws_send(
                client,
                {
                    "type": "homeassistant/expose_entity",
                    "assistants": [domain],
                    "entity_ids": [target],
                    "should_expose": expose,
                },
            )
        options = {"options_domain": domain, "options": saved}
        result = {**result, **await _update(client, target, options)}
    return {**result, "warnings": warnings}


async def _update(client: Any, entity_id: str, fields: dict[str, Any]) -> Any:
    return await _manager()._ws_send(
        client,
        {"type": "config/entity_registry/update", "entity_id": entity_id, **fields},
    )
