"""Todo item backups: an item as Core's ``todo/item/list`` reports it.

The backup key is ``<todo entity>::<item>``, where ``item`` is what the tool
was given: the item's uid or its summary, which Core's todo services both
accept. The snapshot is Core's item row (uid, summary, status, and
description and due where set) plus ``todo_entity_id``; it leaves out
``completed``, the time the list sets when an item is completed, which
follows the restored status. A restore's safety backup and the diff follow the item
by the snapshot's uid, so a rename since the backup does not lose it.

An item still on the list under its uid gets its summary, status,
description and due back through ``todo.update_item``; a description or due
date the snapshot has none of is cleared. A removed item is added again with
``todo.add_item`` and then given its status. The snapshot holds no position
and Core's services cannot set a uid, so a re-added item gets a new uid and
goes where the list adds new items.
"""

from __future__ import annotations

from typing import Any

from .backup_entity_ids import _manager
from .client.rest_client import HomeAssistantCommandError, HomeAssistantError

_LIST = "todo/item/list"


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


async def fetch_todo_item(client: Any, entity_id: str) -> dict[str, Any] | None:
    todo_entity, _, ref = entity_id.partition("::")
    if not todo_entity or not ref:
        return None
    item = find_item(await list_items(client, todo_entity) or [], ref)
    if not item:
        return None
    return {"todo_entity_id": todo_entity} | {
        k: v for k, v in item.items() if k != "completed"
    }


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
    except bm._CAPTURE_TRANSIENT_ERRORS as err:
        raise bm.MandatoryBackupError(
            f"the todo list could not be read: {type(err).__name__}",
            safe_detail="the todo list could not be read",
        ) from err
    return key if item else None


def _due_field(due: str) -> str:
    # Core lists a due datetime as an ISO datetime with a 'T', a due date
    # without one.
    return "due_datetime" if "T" in due else "due_date"


def _field_values(snapshot: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """Description and due as the snapshot had them. A field only the current
    item has is sent as None, which Core takes as a clear; a field neither
    has is left out, since Core refuses one the list does not support."""
    fields: dict[str, Any] = {}
    if snapshot.get("description") is not None:
        fields["description"] = snapshot["description"]
    elif current.get("description") is not None:
        fields["description"] = None
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


async def _require_available(client: Any, todo_entity: str) -> None:
    # Core skips an unavailable list: a write changes nothing and succeeds.
    state = await _manager()._rest_get_or_none(client, f"states/{todo_entity}")
    if state is None:
        raise HomeAssistantError(f"Todo list {todo_entity} no longer exists")
    if state.get("state") == "unavailable":
        raise HomeAssistantError(
            f"Todo list {todo_entity} is unavailable, so nothing would be restored"
        )


async def restore_todo_item(client: Any, entity_id: str, config: Any) -> Any:
    # The list comes from the backup key, which the safety backup also uses.
    todo_entity = entity_id.partition("::")[0]
    uid = config.get("uid")
    summary = config.get("summary")
    status = config.get("status")
    await _require_available(client, todo_entity)
    items = await list_items(client, todo_entity)
    if items is None:
        raise HomeAssistantError(f"Todo list {todo_entity} no longer exists")
    current = (
        next((i for i in items if i.get("uid") == uid), None)
        if uid
        else find_item(items, summary)
    )
    if current is not None:
        if uid and find_item(items, uid) is not current:
            raise HomeAssistantError(
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
    try:
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
    except Exception as err:
        raise HomeAssistantError(
            f"Re-added {summary!r} to {todo_entity}, but marking it completed "
            f"failed or got no answer ({err}); check its status before restoring "
            "again, which would add it a second time"
        ) from err
