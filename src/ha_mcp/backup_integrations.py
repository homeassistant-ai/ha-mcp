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
state. Options it cannot put back (the snapshot has none, or empty ones an
options flow cannot produce) fail the restore after the enabled state is set.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

from .backup_entity_ids import _manager

if TYPE_CHECKING:
    from .backup_manager import BackupRestoreError


async def fetch_integration(client: Any, entity_id: str) -> Any:
    from .redaction import placeholder_option_keys
    from .tools import component_config_entries
    from .tools.component_api import component_supports, get_component_caps

    bm = _manager()
    read = await component_config_entries.fetch_config_entries(
        client, entry_id=entity_id
    )
    if read is None:
        # An installed component whose command failed is not a missing one.
        caps = await get_component_caps(client)
        why = (
            "component_read_failed"
            if component_supports(caps, "config_entries")
            else "component_unavailable"
        )
        items = bm._require_list(
            await bm._ws_send(client, {"type": "config_entries/get"}),
            "config_entries/get",
        )
        for item in items:
            if item.get("entry_id") == entity_id:
                return {**item, "options_unavailable": why}
        return None
    if not read.rows:
        return None
    row = {key: value for key, value in read.rows[0].items() if key != "subentries"}
    options = row.pop("options", None)
    if not isinstance(options, dict):
        row["options_unavailable"] = "options_missing"
    elif read.secret_scrub_degraded:
        row["options_unavailable"] = "secret_scrub_degraded"
    elif placeholder_option_keys(options):
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
    if "options" in config and not isinstance(options, dict):
        raise bm.BackupRestoreError(
            "The integration snapshot's options are not an object. Nothing was "
            "changed.",
            reason="invalid_snapshot",
        )
    if options is not None and "options" not in current:
        raise bm.BackupRestoreError(
            f"The current options of config entry {entity_id} cannot be read "
            f"({current.get('options_unavailable')}), so the snapshot's options "
            "cannot be checked against them. Nothing was changed.",
            reason="options_unreadable",
        )
    not_restored = _options_not_restored(config, current)
    disabled_by = None if config.get("disabled_by") is None else "user"
    enable_first = disabled_by is None and current.get("disabled_by") is not None
    if enable_first:
        result = await _set_disabled_by(client, entity_id, None)
    if (
        not_restored is None
        and options is not None
        and not bm._snapshot_configs_match(options, current["options"])
    ):
        try:
            await _restore_options(client, entity_id, config, current["options"])
        except bm.BackupRestoreError as err:
            if not enable_first:
                raise
            raise _enabled_then_failed(entity_id, err) from err
    if not enable_first:
        result = await _set_disabled_by(client, entity_id, disabled_by)
    if not_restored is not None:
        verification_status, why = not_restored
        raise bm.BackupRestoreError(
            f"The enabled state of config entry {entity_id} was restored, but "
            f"its options were not: {why}.",
            apply_status="applied",
            verification_status=verification_status,
            reason="options_not_restored",
        )
    return result


def _enabled_then_failed(entity_id: str, err: BackupRestoreError) -> BackupRestoreError:
    """``err`` from the options restore of an entry the restore enabled first."""
    if err.outcome["apply_status"] != "not_applied":
        detail = f"then its options restore failed: {err}"
    elif err.outcome.get("reason"):
        detail = f"but its options were not restored: {err}"
    else:
        detail = "but its options were not restored: the options flow refused them"
    failed: BackupRestoreError = _manager().BackupRestoreError(
        f"Config entry {entity_id} was enabled, {detail}",
        # The enable applied; the options restore keeps its own status.
        **{
            **err.outcome,
            "apply_status": "applied",
            "options_apply_status": err.outcome["apply_status"],
        },
    )
    return failed


def _options_not_restored(
    config: Any, current: dict[str, Any]
) -> tuple[str, str] | None:
    """``(verification_status, why)`` for options a restore cannot put back."""
    if "options_unavailable" in config or "options" not in config:
        why = config.get("options_unavailable", "taken before options were captured")
        return ("unavailable", f"the snapshot holds none ({why})")
    if config.get("options") == {} and current["options"]:
        return (
            "mismatched",
            "the snapshot's are empty, which an options flow cannot produce",
        )
    return None


async def _restore_options(
    client: Any, entity_id: str, config: Any, current_options: dict[str, Any]
) -> None:
    from .tools.config_entry_flow import update_config_entry_options

    bm = _manager()
    options = config["options"]
    await bm._apply_and_verify(
        f"{config.get('domain')} integration",
        partial(
            update_config_entry_options,
            client,
            entity_id,
            options,
            expected_domain=config.get("domain"),
            keep_current_values=False,
            fixed_options=current_options,
        ),
        partial(
            bm._verify_readback,
            partial(_read_options, client, entity_id),
            {"options": options},
            ("options",),
        ),
    )
