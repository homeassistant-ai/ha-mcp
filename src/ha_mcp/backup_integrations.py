"""Auto-backup handlers for integration config entries.

An integration snapshot holds only the entry's metadata and its enabled state, so
a restore re-applies that state to an entry that still exists and refuses a
deleted one rather than sending Core a command for an entry it no longer has.
It also refuses ha-mcp's own ``ha_mcp_tools`` entries, which the generic
integration tools leave to the HA UI and the developer tools.
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
                "the HA UI when it needs their credentials."
            ],
        )
    from .tools.config_entry_flow import COMPONENT_DOMAIN

    # Same refusal as ha_set_integration's own-entry guard: re-applying the
    # enabled state would reload or stop the server answering the call.
    domain = config.get("domain")
    if isinstance(domain, str) and domain.strip().lower() == COMPONENT_DOMAIN:
        raise bm.BackupRestoreError(
            f"Config entry {entity_id} belongs to ha-mcp's own '{COMPONENT_DOMAIN}' "
            "integration, whose entries hold the server's own settings and "
            "credentials. Nothing was changed.",
            reason="unsupported_domain",
            suggestions=[
                "Manage it in the HA UI (Settings > Devices & services > "
                "HA-MCP Custom Component), or with developer mode on, use "
                "ha_dev_manage_settings or ha_dev_manage_server."
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
