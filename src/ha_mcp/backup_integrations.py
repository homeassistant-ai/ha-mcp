"""Auto-backup handlers for integration config entries.

A snapshot holds the entry's Core row, its enabled state and, read through the
ha_mcp_tools component, its options. Options are kept only when the component
could scrub them and they hold no redaction placeholder; otherwise
``options_unavailable`` says why. ``entry.data`` (credentials) is never read,
and neither are subentries: ha_set_integration does not edit them, and Core
has no API to put a subentry's data back under a deleted entry.

A restore refuses a deleted entry rather than sending Core a command for an
entry it no longer has. Otherwise it enables the entry first when the snapshot
was enabled, so the options flow runs on a loaded entry, puts back options that
differ through the options flow and reads them back, then applies the enabled
state.
"""

from __future__ import annotations

from functools import partial
from typing import Any

from .backup_entity_ids import _manager


async def fetch_integration(client: Any, entity_id: str) -> Any:
    from .redaction import sentinel_option_keys
    from .tools import component_config_entries

    bm = _manager()
    read = await component_config_entries.fetch_config_entries(
        client, entry_id=entity_id
    )
    if read is None:
        items = bm._require_list(
            await bm._ws_send(client, {"type": "config_entries/get"}),
            "config_entries/get",
        )
        for item in items:
            if item.get("entry_id") == entity_id:
                return {**item, "options_unavailable": "component_unavailable"}
        return None
    if not read.rows:
        return None
    row = {key: value for key, value in read.rows[0].items() if key != "subentries"}
    options = row.pop("options", {})
    if read.secret_scrub_degraded:
        row["options_unavailable"] = "secret_scrub_degraded"
    elif sentinel_option_keys(options) or bm._contains_redacted_leaf(options):
        row["options_unavailable"] = "redacted_options"
    else:
        row["options"] = options
    return row


async def _read_options(client: Any, entry_id: str) -> Any:
    # Options the component can no longer read safely leave the restore
    # unverified rather than failing it as a mismatch.
    from .client.rest_client import HomeAssistantError

    row = await fetch_integration(client, entry_id)
    if row is not None and "options" not in row:
        raise HomeAssistantError(f"options unreadable: {row['options_unavailable']}")
    return row


async def _set_disabled_by(client: Any, entry_id: str, disabled_by: Any) -> Any:
    return await _manager()._ws_send(
        client,
        {
            "type": "config_entries/disable",
            "entry_id": entry_id,
            "disabled_by": disabled_by,
        },
    )


async def restore_integration(client: Any, entity_id: str, config: Any) -> Any:
    bm = _manager()
    current = await fetch_integration(client, entity_id)
    if current is None:
        raise bm.BackupRestoreError(
            f"Config entry {entity_id} ({config.get('domain')}) no longer exists. "
            "An integration snapshot holds only the entry's metadata, options and "
            "enabled state, so it cannot recreate a deleted entry. Nothing was "
            "changed; add the integration again to recover it.",
            reason="entry_deleted",
            suggestions=[
                "Add it again with ha_set_integration, or have the user add it in "
                "the HA UI when it needs their credentials."
            ],
        )
    options = config.get("options")
    if options and "options" not in current:
        raise bm.BackupRestoreError(
            f"The current options of config entry {entity_id} cannot be read "
            f"({current.get('options_unavailable')}), so the snapshot's options "
            "cannot be checked against them. Nothing was changed.",
            reason="options_unreadable",
        )
    disabled_by = None if config.get("disabled_by") is None else "user"
    enable_first = disabled_by is None and current.get("disabled_by") is not None
    if enable_first:
        result = await _set_disabled_by(client, entity_id, None)
    if options and options != current["options"]:
        from .tools.config_entry_flow import update_config_entry_options

        await bm._apply_and_verify(
            f"{config.get('domain')} integration",
            partial(
                update_config_entry_options,
                client,
                entity_id,
                options,
                expected_domain=config.get("domain"),
                keep_current_values=False,
                fixed_options=current["options"],
            ),
            partial(
                bm._verify_readback,
                partial(_read_options, client, entity_id),
                {"options": options},
                ("options",),
            ),
        )
    if not enable_first:
        result = await _set_disabled_by(client, entity_id, disabled_by)
    return result
