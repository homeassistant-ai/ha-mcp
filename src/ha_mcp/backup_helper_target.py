"""The storage helper a helper tool writes, for its pre-write snapshot (#2632).

``ha_config_set_helper`` and ``ha_remove_helpers_integrations`` resolve their
target as ``stored_target`` does: a storage id first, else the entity_id of
the helper's registry entry, whose ``unique_id`` is the storage id. A renamed
entity can carry another helper's id as its object id, so the storage id wins;
nothing is guessed from the text. The snapshot resolves the same way, so it
holds the item the write changes.
"""

from __future__ import annotations

from typing import Any

from .backup_entity_ids import _manager


async def _stored_items(client: Any, helper_type: str) -> list[Any]:
    bm = _manager()
    listed = await bm._ws_send(client, {"type": f"{helper_type}/list"})
    if helper_type == "person" and isinstance(listed, dict):
        listed = listed.get("storage")  # YAML-defined persons are not editable
    items: list[Any] = bm._require_list(listed, f"{helper_type}/list")
    return items


def stored_target(
    helper_type: str, target: str, stored_ids: Any, rows: list[Any]
) -> tuple[str | None, str | None]:
    """The storage id and entity_id of the stored helper ``target`` names.

    A storage id first, as ``ZoneTools._resolve_zone`` resolves a zone; else
    the registry entry on the type's platform whose entity_id is ``target``
    (with or without ``<type>.``), whose unique_id is the storage id. Its
    entity_id is None when the item has no registry entry, and both are None
    when neither matches. A storage id from the registry need not be stored: a
    YAML helper has an entry but no stored item.
    """
    ids = {str(i) for i in stored_ids}
    own = [r for r in rows if r.get("platform") == helper_type]
    if target in ids:
        entity = next(
            (r.get("entity_id") for r in own if r.get("unique_id") == target), None
        )
        return target, entity
    entity_id = (
        target if target.startswith(f"{helper_type}.") else f"{helper_type}.{target}"
    )
    row = next((r for r in own if r.get("entity_id") == entity_id), None)
    if row is None or not row.get("unique_id"):
        return None, None
    return str(row["unique_id"]), entity_id


async def fetch_helper(client: Any, entity_id: str, helper_type: str) -> Any:
    """Fetch a storage helper's full config from its ``<helper_type>/list``.

    Registered for the storage helper types only; flow helpers (config
    entries) have their own handler family (``_make_flow_helper_handler``).
    A failed registry read propagates, so maybe_snapshot logs a WARNING rather
    than silently skipping.
    """
    bm = _manager()
    items = await _stored_items(client, helper_type)
    by_id = {str(item.get("id")): item for item in items}
    storage_id, _ = stored_target(
        helper_type, entity_id, by_id, await bm._entity_registry_rows(client)
    )
    item = by_id.get(storage_id) if storage_id else None
    return await bm._registry_snapshot(client, helper_type, item) if item else None


async def safety_target(
    client: Any, domain: str, entity_id: str, config: Any
) -> str | None:
    """The entity a restore's safety backup is taken under, None when the item
    the restore writes no longer exists.

    The restore writes a stored item by its id; the backup is keyed by the
    entity_id the item had, which a rename since can leave naming nothing, or
    another item. For a storage item the registry entry whose unique_id is the
    stored id follows it. An item can outlive its registry entry, so only its
    absence from the stored items means it is gone: the restore recreates it or
    fails, and overwrites nothing that needs a backup.
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
    target: str | None = next(
        (
            row["entity_id"]
            for row in rows
            if row.get("platform") == platform and row.get("unique_id") == str(stored)
        ),
        None,
    )
    if target is not None:
        return target
    try:
        items = await _stored_items(client, platform)
    except bm._CAPTURE_TRANSIENT_ERRORS as err:
        raise bm.MandatoryBackupError(
            f"the stored {platform} items could not be read: {type(err).__name__}",
            safe_detail=f"the stored {platform} items could not be read",
        ) from err
    if not any(str(item.get("id")) == str(stored) for item in items):
        return None
    # Without a registry entry the item is found by its storage id.
    return str(stored)


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
            suggestions=[
                f"Find the current entity_id of {stored} with ha_search",
                f"If {stored} has no entity, rename the entity that holds {entity_id}",
            ],
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
