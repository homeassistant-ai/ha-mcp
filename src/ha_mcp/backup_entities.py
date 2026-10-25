"""Entity backups: the registry settings ``ha_set_entity`` and ``ha_remove_entity`` change.

Both tools write the entity registry, so the snapshot is the entity's registry
entry, cut to the fields a restore writes back. The entry's ``id`` stays the
same when the entity is renamed, so the restore finds a renamed entity and
gives it its old entity_id back; the safety backup and the diff follow it the
same way. A removed entity has no entry to update: when its integration adds it
again while its config entry exists, or within 30 days, Core gives it back its
old entry ``id`` and the settings it had, and this backup can then be restored
over it. The snapshot leaves out the entry's options, which also hold
voice-assistant exposure, and the device name, which the device registry holds.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any

from .backup_entity_ids import _manager
from .client.rest_client import HomeAssistantCommandError

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
)
# Core's entity_registry/update takes only these values for hidden_by/disabled_by.
_USER_SETTABLE = (None, "user")
_NOT_RESTORED = (
    "Device name, voice-assistant exposure and entity options are not part of "
    "an entity backup and were left as they are."
)


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


async def _entry_by_id(client: Any, entry_id: str) -> dict[str, Any] | None:
    rows = await _manager()._entity_registry_rows(client)
    return next((row for row in rows if row.get("id") == entry_id), None)


async def current_entity_id(client: Any, entity_id: str, config: Any) -> str | None:
    """The entity_id the snapshot's entity has now, found by its entry ``id``
    since a rename; None when the entry is gone. A snapshot without an entry id
    keeps the entity_id it was taken under."""
    if not isinstance(config, dict) or not isinstance(config.get("id"), str):
        return entity_id
    entry = await _entry_by_id(client, config["id"])
    return entry["entity_id"] if entry else None


async def restore_entity(client: Any, entity_id: str, config: Any) -> Any:
    """Write the snapshot's registry fields back, under the entity ID it saved."""
    bm = _manager()
    if not isinstance(config, dict) or not isinstance(config.get("id"), str):
        raise bm.BackupRestoreError(
            f"The backup of {entity_id} holds its state from an earlier ha-mcp "
            "version, not its registry settings, so it cannot be restored",
            reason="invalid_snapshot",
        )
    current = await _entry_by_id(client, config["id"])
    if current is None:
        raise bm.BackupRestoreError(
            f"{entity_id} is no longer in the entity registry",
            reason="entry_deleted",
            suggestions=[
                (
                    "Reload the integration that provides it: Home Assistant "
                    "adds the entity again with the registry settings it had, "
                    "if its config entry still exists or it was removed less "
                    "than 30 days ago"
                ),
                (
                    "If this backup is from an earlier edit, restore it once "
                    "the entity is back"
                ),
            ],
        )
    source, target = current["entity_id"], config["entity_id"]
    async with AsyncExitStack() as locks:
        # A rename changes the lock key; hold both IDs in a stable order.
        for locked in sorted({source, target}):
            await locks.enter_async_context(bm.registry_update_lock("entity", locked))
        current = await fetch_entity(client, source)
        if current is None or current["id"] != config["id"]:
            raise bm.BackupRestoreError(
                f"{entity_id} changed while waiting to restore it; retry",
                reason="entity_identity_mismatch",
                verification_status="mismatched",
            )
        update: dict[str, Any] = {
            "type": "config/entity_registry/update",
            "entity_id": source,
            **{
                field: config.get(field)
                for field in ("name", "icon", "area_id", "device_class")
            },
            "aliases": config.get("aliases"),
            "labels": config.get("labels"),
            # Core updates only the scopes it is sent: clear the ones added since.
            "categories": {
                **dict.fromkeys(current.get("categories") or {}),
                **(config.get("categories") or {}),
            },
        }
        if source != target:
            update["new_entity_id"] = target
        warnings = [_NOT_RESTORED]
        for field in ("hidden_by", "disabled_by"):
            saved, now = config.get(field), current.get(field)
            if saved in _USER_SETTABLE and now in _USER_SETTABLE:
                update[field] = saved
            elif saved != now:
                blocker = saved if saved not in _USER_SETTABLE else now
                warnings.append(
                    f"{field} stays {now!r}: only Home Assistant itself sets {blocker!r}"
                )
        result = await bm._ws_send(client, update)
    return {**result, "warnings": warnings}
