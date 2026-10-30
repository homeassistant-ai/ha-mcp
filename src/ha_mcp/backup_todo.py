"""Todo item backups: an item as Core's ``todo/item/list`` reports it.

The backup key is ``<todo entity>::<item>``, where ``item`` is the item's
uid (its summary when it has none); a backup from an earlier ha-mcp version
keys it by what the tool was given, which may be its summary. A key finds the
item with that uid, else the one Core's todo services act on for that value.
The snapshot is Core's item row (uid, summary, status, and description and due
where set) plus ``todo_entity_id``; it leaves out ``completed``, the time the
list sets when an item is completed, which follows the restored status. A
restore's safety backup and the diff follow the item by the snapshot's uid, so
a rename since the backup does not lose it.

An item still on the list under its uid gets its summary, status,
description and due back through ``todo.update_item``; a description or due
date the snapshot has none of is cleared. A removed item is added again with
``todo.add_item`` and then given its status. When the component serves
``todo_item``, both go through its bridge, which updates by uid and picks the
due field with Core's own date validator. A restore to a list Home Assistant
has not loaded, or to an unavailable one, is refused before anything is
written. The snapshot holds no position and Core's services cannot set a uid,
so a re-added item gets a new uid and goes where the list adds new items.
"""

from __future__ import annotations

import logging
from typing import Any

from .backup_entities import fetch_entity
from .backup_entity_ids import _manager
from .client.rest_client import HomeAssistantCommandError, HomeAssistantError
from .config import get_global_settings
from .tools.component_todo import todo_item

_LIST = "todo/item/list"

logger = logging.getLogger(__name__)


class TodoListNotLoaded(HomeAssistantError):
    """Home Assistant holds no loaded todo list under the entity id, so
    whether the item is on it is unknown."""

    def __init__(self, todo_entity: str) -> None:
        super().__init__(f"Home Assistant has not loaded the todo list {todo_entity}")


def find_item(items: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    """The first item whose uid or summary is ``ref``, the one Core's todo
    services act on for the same value."""
    return next((i for i in items if ref in (i.get("uid"), i.get("summary"))), None)


async def list_items(client: Any, todo_entity: str) -> list[dict[str, Any]] | None:
    """The list's items, or None when Core holds no such todo list."""
    bm = _manager()
    try:
        result = await bm._ws_send(client, {"type": _LIST, "entity_id": todo_entity})
    except HomeAssistantCommandError as err:
        if err.code == "not_found":
            return None
        raise
    items: list[dict[str, Any]] = bm._require_list(
        bm._require_dict(result, _LIST).get("items"), _LIST
    )
    return items


async def _find(
    client: Any, todo_entity: str, items: list[dict[str, Any]], ref: str
) -> dict[str, Any] | None:
    """The item with uid ``ref``, else the one Core resolves ``ref`` to."""
    exact = next((i for i in items if i.get("uid") == ref), None)
    if exact is not None:
        return exact
    reply = await todo_item(client, todo_entity, "resolve", refs=[ref])
    if reply is None:
        return find_item(items, ref)
    if not reply.get("success"):
        raise TodoListNotLoaded(todo_entity)
    found: dict[str, Any] | None = reply["items"][0]
    return found


async def fetch_todo_item(client: Any, entity_id: str) -> dict[str, Any] | None:
    todo_entity, _, ref = entity_id.partition("::")
    if not todo_entity or not ref:
        return None
    items = await list_items(client, todo_entity)
    if items is None:
        if await _list_gone(client, todo_entity):
            return None
        raise TodoListNotLoaded(todo_entity)
    item = await _find(client, todo_entity, items, ref)
    if not item:
        return None
    return {"todo_entity_id": todo_entity} | {
        k: v for k, v in item.items() if k != "completed"
    }


async def back_up_items(
    client: Any, todo_entity: str, items: list[dict[str, Any] | None], tool_name: str
) -> list[str]:
    """Back up the given items, each under its uid (its summary when it has
    none); returns why a backup that was due was not taken."""
    settings = get_global_settings()
    if not getattr(settings, "enable_auto_backup", False):
        return []
    refs = dict.fromkeys(
        ref
        for item in items
        if item and (ref := item.get("uid") or item.get("summary"))
    )
    skipped: list[str] = []
    bm = _manager()
    try:
        mgr = bm.get_backup_manager(client, settings)
        for ref in refs:
            await mgr.maybe_snapshot(
                "todo_item",
                f"{todo_entity}::{ref}",
                tool_name=tool_name,
                skip_reasons=skipped,
            )
    except bm._CAPTURE_TRANSIENT_ERRORS as err:
        logger.warning("Todo item backup failed: %s", err)
        skipped.append(f"No pre-write backup was taken: {type(err).__name__}")
    return skipped


def unbacked_removals(
    before: list[dict[str, Any]], after: list[dict[str, Any]] | None
) -> list[str]:
    """Why an item gone after ``remove_completed_items`` has no backup: Core
    picks the completed items when it runs the service, so one completed
    after the list was read and backed up is removed without a backup. An
    item another client deleted meanwhile is named the same way."""
    settings = get_global_settings()
    if after is None or not getattr(settings, "enable_auto_backup", False):
        return []
    kept = {i.get("uid") for i in after}
    return [
        f"'{i.get('summary')}' is gone from the list, but it was not completed "
        "when the list was read, so no backup of it was taken"
        for i in before
        if i.get("status") != "completed" and i.get("uid") not in kept
    ]


def current_key(entity_id: str, config: Any) -> str:
    """The key that finds the snapshot's item now: its uid where it has one."""
    uid = config.get("uid") if isinstance(config, dict) else None
    return f"{entity_id.partition('::')[0]}::{uid}" if uid else entity_id


async def safety_target(client: Any, entity_id: str, config: Any) -> str | None:
    """The key a restore's safety backup is taken under; None when the item
    is gone, since the restore then adds it again and overwrites nothing."""
    key = current_key(entity_id, config)
    bm = _manager()
    try:
        item = await fetch_todo_item(client, key)
    except TodoListNotLoaded as err:
        raise _unavailable(f"{err}; nothing was restored") from err
    except bm._CAPTURE_TRANSIENT_ERRORS as err:
        raise bm.MandatoryBackupError(
            f"the todo list could not be read: {type(err).__name__}",
            safe_detail="the todo list could not be read",
        ) from err
    return key if snapshot_item(config, item) else None


def snapshot_item(config: Any, item: dict[str, Any] | None) -> dict[str, Any] | None:
    """``item`` when it is the snapshot's item: once the item with the
    snapshot's uid is gone, a row named after that uid is another item."""
    uid = config.get("uid") if isinstance(config, dict) else None
    if item is None or (uid and item.get("uid") != uid):
        return None
    return item


def _due_field(due: str) -> str:
    # Core lists a due datetime as an ISO datetime with a 'T', a due date
    # without one.
    return "due_datetime" if "T" in due else "due_date"


def _description(snapshot: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """The description as the snapshot had it. One only the current item has
    is sent as None, which Core takes as a clear; one neither has is left
    out, since Core refuses a field the list does not support."""
    if snapshot.get("description") is not None:
        return {"description": snapshot["description"]}
    if current.get("description") is not None:
        return {"description": None}
    return {}


def _field_values(snapshot: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Description and due as the snapshot had them, for Core's services."""
    fields = _description(snapshot, current)
    if (due := snapshot.get("due")) is not None:
        fields[_due_field(due)] = due
    elif (current_due := current.get("due")) is not None:
        fields[_due_field(current_due)] = None
    return fields


def service_message(
    service: str, todo_entity: str, data: dict[str, Any]
) -> dict[str, Any]:
    """A WebSocket ``call_service`` of todo.<service> on one list."""
    return {
        "type": "call_service",
        "domain": "todo",
        "service": service,
        "target": {"entity_id": todo_entity},
        "service_data": data,
    }


async def _call(
    client: Any, todo_entity: str, service: str, data: dict[str, Any]
) -> Any:
    return await _manager()._ws_send(
        client, service_message(service, todo_entity, data)
    )


def _not_applied(message: str, **outcome: Any) -> HomeAssistantError:
    error: HomeAssistantError = _manager().BackupRestoreError(
        message, apply_status="not_applied", **outcome
    )
    return error


def _unavailable(message: str) -> HomeAssistantError:
    return _not_applied(message, reason="entity_unavailable")


async def _list_gone(client: Any, todo_entity: str) -> bool:
    """Whether Home Assistant no longer has the list. Core answers a deleted
    list as one it has not loaded. A deleted list has neither a state nor a
    registry entry; a disabled one keeps its entry. So does a list without a
    unique id that Home Assistant has not loaded, which is taken as deleted."""
    state = await _manager()._rest_get_or_none(client, f"states/{todo_entity}")
    return state is None and await fetch_entity(client, todo_entity) is None


async def _require_available(client: Any, todo_entity: str) -> None:
    # Core skips an unavailable list: a write changes nothing and succeeds.
    state = await _manager()._rest_get_or_none(client, f"states/{todo_entity}")
    if state is None and await fetch_entity(client, todo_entity) is None:
        raise _not_applied(
            f"Todo list {todo_entity} no longer exists", reason="entity_not_found"
        )
    if state is not None and state.get("state") == "unavailable":
        raise _unavailable(
            f"Todo list {todo_entity} is unavailable, so nothing would be restored"
        )


async def _completed_again(todo_entity: str, summary: Any, mark: Any) -> Any:
    """Mark a re-added item completed. The add is applied by then, so any
    failure says so, since restoring again would add the item twice."""
    try:
        return await mark()
    except Exception as err:
        raise HomeAssistantError(
            f"Re-added {summary!r} to {todo_entity}, but marking it completed "
            f"failed or got no answer ({err}); check its status before restoring "
            "again, which would add it a second time"
        ) from err


async def _restore_by_bridge(
    client: Any, todo_entity: str, config: Any, current: dict[str, Any] | None
) -> Any:
    """The restore through the component, an update going by uid; None when
    the component does not serve it."""
    summary = config.get("summary")
    status = config.get("status")
    if current is not None:
        action = "update"
        data = {"rename": summary, "status": status} | _description(config, current)
        fields = {"uid": current["uid"], "data": data}
    else:
        action = "add"
        fields = {"data": {"item": summary} | _description(config, {})}
    reply = await todo_item(
        client, todo_entity, action, due=config.get("due"), **fields
    )
    if reply is None:
        return None
    if not reply.get("success"):
        message = (reply.get("error") or {}).get("message", "Unknown error")
        raise _not_applied(f"{message}; nothing was restored")
    if current is not None or status != "completed":
        return reply

    async def mark() -> Any:
        if reply["item"] is None:
            raise HomeAssistantError("the re-added item could not be told apart")
        marked = await todo_item(
            client,
            todo_entity,
            "update",
            uid=reply["item"]["uid"],
            data={"status": status},
        )
        if not marked or not marked.get("success"):
            error = (marked or {}).get("error") or {}
            raise HomeAssistantError(
                error.get("message", "the component did not run it")
            )
        return marked

    return await _completed_again(todo_entity, summary, mark)


async def restore_todo_item(client: Any, entity_id: str, config: Any) -> Any:
    # The list comes from the backup key, which the safety backup also uses.
    todo_entity = entity_id.partition("::")[0]
    uid = config.get("uid")
    summary = config.get("summary")
    status = config.get("status")
    await _require_available(client, todo_entity)
    items = await list_items(client, todo_entity)
    if items is None:
        raise _unavailable(f"{TodoListNotLoaded(todo_entity)}; nothing was restored")
    current = (
        next((i for i in items if i.get("uid") == uid), None)
        if uid
        else find_item(items, summary)
    )
    if current is None or current.get("uid"):
        result = await _restore_by_bridge(client, todo_entity, config, current)
        if result is not None:
            return result
    if current is not None:
        if uid and find_item(items, uid) is not current:
            raise _not_applied(
                f"Another item on {todo_entity} is named {uid!r}, so Home "
                "Assistant would update that one; nothing was restored"
            )
        return await _call(
            client,
            todo_entity,
            "update_item",
            {"item": uid or summary, "rename": summary, "status": status}
            | _field_values(config, current),
        )

    result = await _call(
        client, todo_entity, "add_item", {"item": summary} | _field_values(config, {})
    )
    if status != "completed":
        return result
    known = {i.get("uid") for i in items}

    async def mark() -> Any:
        after = await list_items(client, todo_entity) or []
        added = [i for i in after if i.get("uid") not in known]
        if (
            len(added) != 1
            or not added[0].get("uid")
            or added[0].get("summary") != summary
            or find_item(after, added[0]["uid"]) is not added[0]
        ):
            raise HomeAssistantError("the re-added item could not be told apart")
        return await _call(
            client,
            todo_entity,
            "update_item",
            {"item": added[0]["uid"], "status": status},
        )

    return await _completed_again(todo_entity, summary, mark)
