"""Give a recreated flow helper's entities their saved IDs and names.

Split out of ``backup_manager`` (#2632), which imports this module; the manager's
own helpers are reached lazily so its tests can patch them in place.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any


def _manager() -> Any:
    from . import backup_manager

    return backup_manager


async def registry_row(
    client: Any, platform: str, unique_id: str
) -> dict[str, Any] | None:
    """The entity-registry row of ``platform``'s entity with ``unique_id``."""
    rows = await _manager()._entity_registry_rows(client)
    return next(
        (
            row
            for row in rows
            if row.get("platform") == platform and row.get("unique_id") == unique_id
        ),
        None,
    )


def _recreated_unique_id(saved: dict[str, Any], old_entry: str, new_entry: str) -> str:
    """Core derives a helper entity's unique_id from its config-entry ID."""
    return str(saved["unique_id"]).replace(old_entry, new_entry)


async def _restore_entity_ids(
    client: Any, entry_id: str, original_entry_id: str, saved_rows: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Give each recreated entity its saved ID/name, guarded by ownership.

    A failure part-way reports the entities already restored, so the outcome
    names every rename that did happen.
    """
    bm = _manager()
    mapping: list[dict[str, str]] = []
    try:
        for saved in saved_rows:
            await _restore_entity_id(
                client, entry_id, original_entry_id, saved, mapping
            )
    except bm.BackupRestoreError as err:
        err.outcome.setdefault("entity_id_mapping", mapping)
        raise
    except bm._CAPTURE_TRANSIENT_ERRORS as err:
        bm._log_flow_helper_failure("entity_rename", err)
        raise bm.BackupRestoreError(
            "A recreated entity could not be found or restored",
            reason="upstream_error",
            entity_id_mapping=mapping,
            verification_status="unavailable",
        ) from err
    return mapping


async def _restore_entity_id(
    client: Any,
    entry_id: str,
    original_entry_id: str,
    saved: dict[str, Any],
    mapping: list[dict[str, str]],
) -> None:
    """Rename one recreated entity to its saved ID/name and record it."""
    bm = _manager()
    unique_id = _recreated_unique_id(saved, original_entry_id, entry_id)
    row = await bm._created_entity(client, entry_id, unique_id)
    source, target = row["entity_id"], saved["entity_id"]
    locked_ids = {source, target}
    async with AsyncExitStack() as locks:
        # A rename changes the lock key; hold both IDs in a stable order.
        for entity_id in sorted(locked_ids):
            await locks.enter_async_context(
                bm.registry_update_lock("entity", entity_id)
            )
        row = await bm._created_entity(client, entry_id, unique_id)
        source = row["entity_id"]
        if source not in locked_ids:
            raise bm.BackupRestoreError(
                "Recreated entity ID changed while waiting to restore it",
                reason="entity_identity_mismatch",
                verification_status="mismatched",
            )
        if source.split(".")[0] != target.split(".")[0]:
            raise bm.BackupRestoreError(
                "Recreated entity has an unexpected domain",
                reason="entity_identity_mismatch",
                verification_status="mismatched",
            )
        await bm._check_entity_collision(client, target, owned_entry_id=entry_id)
        update: dict[str, Any] = {
            "type": "config/entity_registry/update",
            "entity_id": source,
        }
        if source != target:
            update["new_entity_id"] = target
        if row.get("name") != saved.get("name"):
            update["name"] = saved.get("name")
        if len(update) > 2:
            try:
                await bm._ws_send(client, update)
            except bm._CAPTURE_TRANSIENT_ERRORS as err:
                bm._log_flow_helper_failure("entity_rename", err)
                raise bm.BackupRestoreError(
                    "The recreated helper's entity rename outcome is unknown",
                    reason="entity_rename_outcome_unknown",
                    entity_id_mapping=[
                        *mapping,
                        {"created_entity_id": source, "target_entity_id": target},
                    ],
                    verification_status="unavailable",
                ) from err
        actual = await bm._created_entity(client, entry_id, unique_id)
        if actual["entity_id"] != target or actual.get("name") != saved.get("name"):
            raise bm.BackupRestoreError(
                "Recreated entity identity did not match",
                reason="entity_identity_mismatch",
                verification_status="mismatched",
            )
        mapping.append({"created_entity_id": source, "restored_entity_id": target})
