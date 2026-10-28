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

With the ha_mcp_tools component (``entity_restore.py`` there) the restore is
one in-process registry update, so a value Core refuses changes nothing; it can
write hidden_by and disabled_by values other than ``user``, and it reports what
Core still keeps of a removed entry. Without it, Core's ``config/entity_registry``
commands write the fields one update at a time and take only ``user``. An
enable goes through Core's update command on both paths, so Core's refusal for
a disabled device and its ``require_restart`` answer apply.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from contextvars import ContextVar
from typing import Any

from ._vendor.fastmcp.exceptions import ToolError
from .backup_entity_ids import _manager, entity_id_locks
from .client.rest_client import HomeAssistantCommandError
from .errors import ErrorCode
from .tools.component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
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
_RESTORED_FIELDS = ("name", "icon", "area_id", "device_class", "aliases", "labels")
_NOT_RESTORED = "The device name is not part of an entity backup and was left as it is."
_ENTRY_COMMAND = "ha_mcp_tools/entity_entry"
_RESTORE_COMMAND = "ha_mcp_tools/entity_restore"
# A component reply can be None; only this means the component ran nothing.
_NOT_SERVED = object()

logger = logging.getLogger(__name__)


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
    backups held registry settings has none, and one without a non-empty
    string entity_id names no entity to restore; both are refused before
    anything, a safety backup included, is written."""
    entry_id = config.get("id") if isinstance(config, dict) else None
    if not isinstance(entry_id, str):
        raise _manager().InvalidBackupSnapshotError(
            f"The backup of {entity_id} holds its state from an earlier ha-mcp "
            "version, not its registry settings, so it cannot be restored"
        )
    if not isinstance(config.get("entity_id"), str) or not config["entity_id"]:
        raise _manager().InvalidBackupSnapshotError(
            f"The backup of {entity_id} names no entity_id, so it cannot be restored"
        )
    return entry_id


async def _component_send(client: Any, capability: str, message: dict[str, Any]) -> Any:
    """A component command's reply; ``_NOT_SERVED`` when the component does
    not serve it, so the caller uses Core's commands."""
    if not component_supports(await get_component_caps(client), capability):
        return _NOT_SERVED
    try:
        return await _manager()._ws_send(client, message)
    except HomeAssistantCommandError as err:
        if not is_unknown_command(err):
            raise
        logger.warning(
            "%s unknown to the component; using Core's commands", message["type"]
        )
        invalidate_caps(client)
        return _NOT_SERVED


async def _lookup(client: Any, entry_id: str) -> dict[str, Any]:
    """``{"entry": row or None}``. For a gone entry the component's reply adds
    ``"deleted"``: what Core still keeps of it, or None when it keeps nothing.
    Without the component the key is absent: nobody knows."""
    found = await _component_send(
        client, "entity_entry", {"type": _ENTRY_COMMAND, "entry_id": entry_id}
    )
    if found is not _NOT_SERVED:
        lookup: dict[str, Any] = _manager()._require_dict(found, _ENTRY_COMMAND)
        return lookup
    rows = await _manager()._entity_registry_rows(client)
    return {"entry": next((row for row in rows if row.get("id") == entry_id), None)}


async def current_entity_id(client: Any, entity_id: str, config: Any) -> str | None:
    """The entity_id the snapshot's entity has now, found by its entry ``id``
    since a rename; None when the entry is gone."""
    entry = (await _lookup(client, _entry_id(entity_id, config)))["entry"]
    return entry["entity_id"] if entry else None


def _entry_deleted(entity_id: str, found: dict[str, Any]) -> Exception:
    """The refusal for a gone entry, from what ``_lookup`` knows of it."""
    again = "If this backup is from an earlier edit, restore it once the entity is back"
    if "deleted" not in found:
        suggestions = [
            "Restart Home Assistant: if what provides the entity is still "
            "there, it adds the entity again with the registry settings it "
            "had while its config entry exists, and for 30 days after that "
            "entry, or an entity without one, was removed. A reload does not "
            "bring back a stored helper's entity",
            again,
        ]
    elif (deleted := found["deleted"]) is None:
        suggestions = [
            "Home Assistant no longer keeps this entity's registry settings: "
            "an entity added again gets a new registry entry, which this "
            "backup cannot be restored over. Set the entity with ha_set_entity "
            "instead"
        ]
    else:
        kept = (
            f"until {deleted['kept_until']}"
            if deleted.get("kept_until")
            else "while its config entry exists"
        )
        suggestions = [
            f"Home Assistant keeps the registry settings of {deleted['entity_id']} "
            f"{kept} and gives them back when the entity is added again. If "
            "what provides the entity is still there, restart Home Assistant; "
            "a reload does not bring back a stored helper's entity",
            again,
        ]
    error: Exception = _manager().BackupRestoreError(
        f"{entity_id} is no longer in the entity registry",
        reason="entry_deleted",
        suggestions=suggestions,
    )
    return error


# The entity IDs whose locks ``restore_locks`` holds for the running restore.
_HELD: ContextVar[frozenset[str] | None] = ContextVar("held_entity_locks", default=None)


@asynccontextmanager
async def restore_locks(client: Any, config: Any) -> AsyncIterator[None]:
    """Hold the entity locks from before a restore's safety backup through its
    write, as a flow helper's restore holds its config-entry guard. When the
    lookup finds no entry or fails with a transient error, nothing is locked,
    and the restore refuses before it writes."""
    entry_id = config.get("id") if isinstance(config, dict) else None
    saved = config.get("entity_id") if isinstance(config, dict) else None
    try:
        found = await _lookup(client, entry_id) if isinstance(entry_id, str) else {}
    except _manager()._CAPTURE_TRANSIENT_ERRORS:
        found = {}  # the restore looks the entry up again and reports a failure
    entry = found.get("entry")
    ids: frozenset[str] = frozenset()
    if entry is not None and isinstance(saved, str):
        ids = frozenset((entry["entity_id"], saved))
    async with entity_id_locks(*ids):
        token = _HELD.set(ids)
        try:
            yield
        finally:
            _HELD.reset(token)


def _entity_locks(
    entity_id: str, source: str, target: str
) -> AbstractAsyncContextManager[None]:
    """The restore's entity locks, or none when ``restore_locks`` holds them.
    An entry it could not lock, or one renamed to a third ID since, is not
    under them, and its safety backup was taken without them."""
    held = _HELD.get()
    if held is None:
        return entity_id_locks(source, target)
    if not held:
        raise _manager().BackupRestoreError(
            f"{entity_id} could not be locked before its safety backup was taken; retry",
            reason="entity_identity_mismatch",
            verification_status="mismatched",
        )
    if source not in held:
        raise _changed_while_waiting(entity_id)
    return nullcontext()


def _changed_while_waiting(entity_id: str) -> Exception:
    """The refusal for an entry that moved while the restore waited for it."""
    error: Exception = _manager().BackupRestoreError(
        f"{entity_id} changed while waiting to restore it; retry",
        reason="entity_identity_mismatch",
        verification_status="mismatched",
    )
    return error


async def restore_entity(client: Any, entity_id: str, config: Any) -> Any:
    """Write the snapshot's registry fields back, under the entity ID it saved."""
    bm = _manager()
    entry_id = _entry_id(entity_id, config)
    found = await _lookup(client, entry_id)
    current = found["entry"]
    if current is None:
        raise _entry_deleted(entity_id, found)
    source, target = current["entity_id"], config["entity_id"]
    async with _entity_locks(entity_id, source, target):
        current = await fetch_entity(client, source)
        if current is None or current["id"] != entry_id:
            raise _changed_while_waiting(entity_id)
        if source != target:
            await bm._check_entity_collision(client, target)
        await _check_references(client, config)
        result = await _restore_in_process(
            client, entity_id, entry_id, source, current, config
        )
        if result is None:
            result = await _write_back(client, source, target, current, config)
        return result


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
    update: dict[str, Any] = {field: config.get(field) for field in _RESTORED_FIELDS}
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
        if config.get(field) != current.get(field):
            result = await _write_hider(
                client, target, field, config, current, result, warnings
            )
    now_options = current.get("options") or {}
    for domain, saved in _changed_options(current, config).items():
        now = now_options.get(domain)
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


async def _restore_in_process(
    client: Any,
    entity_id: str,
    entry_id: str,
    source: str,
    current: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """The component's write; None when the component does not serve it.

    disabled_by None is not sent: the enable goes through Core's update
    command afterwards, for its device check and its restart answer. An error
    once the write was sent reaches the restore as it is, which reports the
    outcome as unknown, and is never followed by a write through Core's
    commands.
    """
    settings: dict[str, Any] = {field: config.get(field) for field in _RESTORED_FIELDS}
    settings.update(
        hidden_by=config.get("hidden_by"), categories=config.get("categories") or {}
    )
    if config.get("disabled_by") is not None:
        settings["disabled_by"] = config["disabled_by"]
    message: dict[str, Any] = {
        "type": _RESTORE_COMMAND,
        "entry_id": entry_id,
        "entity_id": source,
        "settings": settings,
        "options": _changed_options(current, config),
    }
    target = config["entity_id"]
    if source != target:
        message["new_entity_id"] = target
    reply = await _component_send(client, "entity_restore", message)
    if reply is _NOT_SERVED:
        return None
    reply = _manager()._require_dict(reply, _RESTORE_COMMAND)
    if reply.get("success") is not True:
        raise _refusal(entity_id, reply.get("error") or {})
    result: dict[str, Any] = {"entity_entry": reply.get("entity_entry")}
    warnings = [_NOT_RESTORED, *(reply.get("warnings") or [])]
    if config.get("disabled_by") is None and current.get("disabled_by") is not None:
        result = await _write_hider(
            client, target, "disabled_by", config, current, result, warnings
        )
    return {**result, "warnings": warnings}


def _refusal(entity_id: str, error: dict[str, Any]) -> Exception:
    """Why the component did not restore. Its three codes mean it wrote
    nothing; a code this server does not know leaves the outcome unknown."""
    code, message = error.get("code"), error.get("message") or "no reason given"
    if code == "entry_deleted":
        return _entry_deleted(entity_id, {"deleted": error.get("deleted")})
    refusal: Exception
    if code == "entity_identity_mismatch":
        refusal = _changed_while_waiting(entity_id)
    elif code == "invalid":
        refusal = _manager().BackupRestoreError(
            f"Home Assistant refused the restore of {entity_id}: {message}. "
            "Nothing was restored.",
            reason="rejected_by_home_assistant",
        )
    else:
        refusal = _manager().BackupRestoreError(
            f"The restore of {entity_id} failed with {code!r}: {message}",
            apply_status="unknown",
            reason="upstream_error",
        )
    return refusal


def _changed_options(current: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """The saved options of each domain that differs now; None removes one."""
    saved = config.get("options") or {}
    now = current.get("options") or {}
    return {
        domain: saved.get(domain)
        for domain in sorted({*saved, *now})
        if saved.get(domain) != now.get(domain)
    }


async def _write_hider(
    client: Any,
    target: str,
    field: str,
    config: dict[str, Any],
    current: dict[str, Any],
    result: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    """Send one hidden_by or disabled_by value; keep Core's refusal as a warning."""
    saved, now = config.get(field), current.get(field)
    try:
        return {**result, **await _update(client, target, {field: saved})}
    except HomeAssistantCommandError as err:
        if err.code not in ("invalid_format", "invalid_info"):
            raise
        warnings.append(
            f"{field} stays {now!r}: Home Assistant refused {saved!r}: {err}"
        )
        return result


async def _update(client: Any, entity_id: str, fields: dict[str, Any]) -> Any:
    return await _manager()._ws_send(
        client,
        {"type": "config/entity_registry/update", "entity_id": entity_id, **fields},
    )
