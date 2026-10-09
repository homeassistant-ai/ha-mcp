"""Auto-backup handlers for integration config entries.

An integration snapshot holds only the entry's metadata and its enabled state, so
a restore re-applies that state to an entry that still exists and refuses a
deleted one rather than sending Core a command for an entry it no longer has.
"""

from __future__ import annotations

from typing import Any

from .backup_entity_ids import _manager


async def fetch_integration(client: Any, entity_id: str) -> Any:
    bm = _manager()
    items = bm._require_list(
        await bm._ws_send(client, {"type": "config_entries/get"}),
        "config_entries/get",
    )
    for item in items:
        if item.get("entry_id") == entity_id:
            return item
    return None


async def restore_integration(client: Any, entity_id: str, config: Any) -> Any:
    bm = _manager()
    if await fetch_integration(client, entity_id) is None:
        raise bm.BackupRestoreError(
            f"Config entry {entity_id} ({config.get('domain')}) no longer exists. "
            "An integration snapshot holds only the entry's metadata and enabled "
            "state, so it cannot recreate a deleted entry. Nothing was changed; add "
            "the integration again to recover it.",
            reason="entry_deleted",
            suggestions=[
                "Add it again with ha_set_integration, or have the user add it in "
                "the HA UI when it needs their credentials (otp)."
            ],
        )
    disabled = config.get("disabled_by") is not None
    return await bm._ws_send(
        client,
        {
            "type": "config_entries/disable",
            "entry_id": entity_id,
            "disabled_by": "user" if disabled else None,
        },
    )
