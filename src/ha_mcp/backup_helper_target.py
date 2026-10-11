"""The storage helper a helper tool writes, for its pre-write snapshot (#2632).

``ha_config_set_helper`` and ``ha_remove_helpers_integrations`` resolve a
storage helper through the entity registry: the entry of ``<type>.<id>`` holds
the helper's storage id as its ``unique_id``. A renamed entity can carry another
helper's id as its object id, so the snapshot asks the registry first; an id
with no registry entry is taken as a storage id, as the removal's direct-id
fallback does. Both tools take a bare tag id as the stored id (a tag's
entity_id follows its name), so a bare tag id skips the registry.
"""

from __future__ import annotations

from typing import Any

from .backup_entity_ids import _manager
from .client.rest_client import HomeAssistantError


async def fetch_helper(client: Any, entity_id: str, helper_type: str) -> Any:
    """Fetch a storage helper's full config from its ``<helper_type>/list``.

    Registered for the storage helper types only; flow helpers (config
    entries) have their own handler family (``_make_flow_helper_handler``).
    """
    bm = _manager()
    listed = await bm._ws_send(client, {"type": f"{helper_type}/list"})
    if helper_type == "person" and isinstance(listed, dict):
        listed = listed.get("storage")  # YAML-defined persons are not editable
    items = bm._require_list(listed, f"{helper_type}/list")
    prefixed = entity_id.startswith(f"{helper_type}.")
    eid = entity_id if prefixed else f"{helper_type}.{entity_id}"
    entry: Any = {"unique_id": entity_id}
    if helper_type != "tag" or prefixed:
        try:
            entry = await bm._ws_send(
                client, {"type": "config/entity_registry/get", "entity_id": eid}
            )
        except HomeAssistantError as err:
            # Only Core's not_found, or invalid_format for an id that cannot
            # name an entity, makes the id a storage id; transport/auth/5xx
            # errors propagate, so maybe_snapshot logs a WARNING rather than
            # silently skipping.
            if getattr(err, "code", None) not in ("not_found", "invalid_format"):
                raise
            entry = {"unique_id": eid.split(".", 1)[1]}
    unique_id = entry.get("unique_id") if isinstance(entry, dict) else None
    if unique_id:
        for item in items:
            if str(item.get("id")) == str(unique_id):
                return await bm._registry_snapshot(client, helper_type, item)
    return None


async def safety_target(
    client: Any, domain: str, entity_id: str, config: Any
) -> str | None:
    """The entity a restore's safety backup is taken under, None when the item
    the restore writes no longer exists.

    The restore writes a stored item by its id; the backup is keyed by the
    entity_id the item had, which a rename since can leave naming nothing, or
    another item. For a storage item the registry entry whose unique_id is the
    stored id follows it, and no entry means the item is gone: the restore
    recreates it or fails, and overwrites nothing that needs a backup.
    """
    bm = _manager()
    platform = domain.removeprefix("helper_")
    stored = config.get("id") if isinstance(config, dict) else None
    if not stored or platform not in bm._HELPER_LIST_TYPES:
        return entity_id
    try:
        rows = await bm._entity_registry_rows(client)
    except bm._CAPTURE_TRANSIENT_ERRORS as err:
        raise bm.MandatoryBackupError(
            f"the entity registry could not be read: {type(err).__name__}",
            safe_detail="the entity registry could not be read",
        ) from err
    return next(
        (
            row["entity_id"]
            for row in rows
            if row.get("platform") == platform and row.get("unique_id") == str(stored)
        ),
        None,
    )


def require_holds(config: Any, stored: Any, entity_id: str) -> None:
    """Block a restore whose safety backup would hold another item than the
    one it overwrites: an entity_id the registry cannot follow, renamed since
    the backup, can name another item."""
    held = config.get("id") if isinstance(config, dict) else None
    if held != stored:
        raise _manager().BackupRestoreError(
            f"Restore blocked: the safety backup would hold {held}, not {stored}, "
            f"which this restore overwrites: {entity_id} names another item since "
            "this backup. Nothing was changed.",
            reason="backup_capture_failed",
            suggestions=[f"Find the current entity_id of {stored} with ha_search"],
        )


def note_no_safety_backup(
    outcome: Any, domain: str, target: str | None, config: Any
) -> None:
    """Say why a restore goes ahead without a safety backup."""
    if target is None:  # safety_target: the stored item no longer exists
        stored = config.get("id") if isinstance(config, dict) else None
        outcome["warnings"] = [
            f"No safety backup was taken: {domain.removeprefix('helper_')} "
            f"{stored} no longer exists, "
            "so this restore overwrites none of its state."
        ]
        return
    outcome["warnings"] = [
        f"No safety backup was taken: nothing was found under {target}. If it "
        "was renamed since this backup, the restore overwrites its current state "
        "without one."
    ]
