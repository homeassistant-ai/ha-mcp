"""Todo item writes through Core's todo services, resolved and read back in process.

Core's todo services act on the first row whose uid or summary equals the
value given (``_find_by_uid_or_summary``) and answer without the row they
wrote. This bridge resolves with that same function, calls the services
(their schema, feature checks and context apply) and reads the stored rows
from the list entity, so with the bridge the server needs no copy of Core's
item resolution or of its schema's coercions. It refuses an unavailable list
itself: Core's services would skip it and answer success. An update can also
move the item: the move follows the update and goes by uid, as Core's
``todo/item/move`` does, so the update's item value resolves on the order the
list had, which the bridge checked.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from .helper_collections import _INVALID_ERRORS

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

WS_TODO_ITEM = "ha_mcp_tools/todo_item"
# Advertised by websocket_api's info; the server's todo tools and todo
# backups use it and fall back to Core's todo services over WebSocket.
CAPABILITY = "todo_item"
_SERVICES = {"add": "add_item", "update": "update_item", "remove": "remove_item"}
_NEEDS = {"resolve": "refs", "remove": "refs", "update": "uid", "add": "data"}


def _failure(code: str, message: str) -> dict[str, Any]:
    return {"success": False, "error": {"code": code, "message": message}}


class _Refused(Exception):
    """A write refused before Core's service was called."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.failure = _failure(code, message)


def _core() -> SimpleNamespace | None:
    """Core's todo parts this bridge uses, or None on a Core without them."""
    try:
        from homeassistant.components.todo import const, entity, services
        from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
        from homeassistant.helpers import config_validation as cv
    except ImportError:
        return None
    core = SimpleNamespace(
        component_key=getattr(const, "DATA_COMPONENT", None),
        domain=getattr(const, "DOMAIN", None),
        list_entity=getattr(entity, "TodoListEntity", None),
        can_move=getattr(
            getattr(const, "TodoListEntityFeature", None), "MOVE_TODO_ITEM", None
        ),
        items_factory=getattr(entity, "api_items_factory", None),
        find=getattr(services, "_find_by_uid_or_summary", None),
        # What Core's todo services raise when they refuse a call: their
        # schema and their own checks. An integration may raise these too,
        # so _write also compares the rows. Any other error may come after
        # the integration reached its backend, so its outcome is unknown.
        refused=(
            *(e for e in _INVALID_ERRORS if e is not ValueError),
            ServiceValidationError,
        ),
        error=HomeAssistantError,
        date=cv.date,
    )
    return None if None in vars(core).values() else core


def _row(core: SimpleNamespace, item: Any) -> dict[str, Any]:
    # The row shape Core's todo/item/list answers with.
    return dataclasses.asdict(item, dict_factory=core.items_factory)


def _due(core: SimpleNamespace, value: str | None, current: Any) -> dict[str, Any]:
    """A snapshot's ``due`` as the service field Core's schema takes: a value
    Core's own date validator accepts is a date. A clear names the field of
    the due date the item has, since Core refuses a field the list lacks."""
    if value is None:
        if current is None or current.due is None:
            return {}
        key = "due_datetime" if isinstance(current.due, datetime) else "due_date"
        return {key: None}
    try:
        core.date(value)
    except _INVALID_ERRORS:
        return {"due_datetime": value}
    return {"due_date": value}


def _service_data(
    core: SimpleNamespace, msg: dict[str, Any], before: list[Any]
) -> tuple[dict[str, Any], Any]:
    """The service data for a write and the row an update targets. An update
    goes by uid; Core changes the first row whose uid or summary equals it, so
    the update is refused when an earlier row is named after the uid."""
    data = dict(msg.get("data") or {})
    current = None
    if msg["action"] == "update":
        uid = msg["uid"]
        current = next((i for i in before if i.uid == uid), None)
        if current is None:
            raise _Refused("item_not_found", f"No item with uid {uid!r}")
        if core.find(uid, before) is not current:
            raise _Refused(
                "namesake",
                f"Another item is named {uid!r}, so Home Assistant would change "
                "that one; nothing was changed",
            )
        data["item"] = uid
    elif msg["action"] == "remove":
        data["item"] = msg["refs"]
    if "due" in msg:
        data |= _due(core, msg["due"], current)
    return data, current


def _outcome(
    core: SimpleNamespace,
    msg: dict[str, Any],
    before: list[Any],
    current: Any,
    after: list[dict[str, Any]],
) -> dict[str, Any]:
    """The added or updated row as the list entity holds it afterwards, or
    the rows a removal took off, as they were before it. A row may lack a
    uid (``api_items_factory`` drops None fields)."""
    if msg["action"] == "add":
        known = {i.uid for i in before}
        added = [row for row in after if row.get("uid") not in known]
        return {"success": True, "item": added[0] if len(added) == 1 else None}
    if msg["action"] == "update":
        stored = next((row for row in after if row.get("uid") == current.uid), None)
        return {"success": True, "item": stored}
    # Another client's removal of a row the call did not name is not this call's.
    asked = {found.uid for ref in msg["refs"] if (found := core.find(ref, before))}
    gone = asked - {row.get("uid") for row in after}
    removed = [_row(core, i) for i in before if i.uid in gone]
    return {"success": True, "removed": removed}


async def _write(
    hass: HomeAssistant, core: SimpleNamespace, entity: Any, msg: dict[str, Any]
) -> dict[str, Any]:
    if not entity.available:
        # Core's services skip an unavailable list and still answer success.
        return _failure("list_unavailable", f"{entity.entity_id} is unavailable")
    before = list(entity.todo_items or ())
    try:
        data, current = _service_data(core, msg, before)
    except _Refused as refused:
        return refused.failure
    moves = msg["action"] == "update" and "previous_uid" in msg
    if moves and not (entity.supported_features or 0) & core.can_move:
        # What Core's todo/item/move checks, asked before the update is written.
        return _failure(
            "not_supported", f"{entity.entity_id} does not support reordering items"
        )
    rows_before = [_row(core, i) for i in before]
    try:
        await hass.services.async_call(
            core.domain,
            _SERVICES[msg["action"]],
            data,
            target={"entity_id": entity.entity_id},
            blocking=True,
            context=msg.get("ha_mcp_context"),
        )
    except core.refused as err:
        if [_row(core, i) for i in entity.todo_items or ()] != rows_before:
            raise  # the list changed, so this is no refusal
        return _failure("invalid", str(err))
    if moves:
        # After the update, which Core resolved on the order as it was; the
        # move goes by uid, as Core's todo/item/move does.
        try:
            await entity.async_move_todo_item(
                uid=msg["uid"], previous_uid=msg["previous_uid"]
            )
        except core.error as err:
            return _failure(
                "move_failed", f"The update was applied, but the move failed: {err}"
            )
    after = [_row(core, i) for i in entity.todo_items or ()]
    return _outcome(core, msg, before, current, after)


async def async_todo_item(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    core = _core()
    if core is None:
        return _failure("unsupported", "This Home Assistant has no todo bridge")
    action = msg["action"]
    if _NEEDS[action] not in msg:
        return _failure("invalid", f"{_NEEDS[action]} is required for {action}")
    component = hass.data.get(core.component_key)
    entity_id = msg["entity_id"]
    entity = component.get_entity(entity_id) if component is not None else None
    if not isinstance(entity, core.list_entity):
        return _failure(
            "not_loaded", f"Home Assistant has no loaded todo list {entity_id}"
        )
    if action != "resolve":
        return await _write(hass, core, entity, msg)
    items = list(entity.todo_items or ())
    found = [core.find(ref, items) for ref in msg["refs"]]
    return {"success": True, "items": [_row(core, i) if i else None for i in found]}


def command_specs(vol: Any) -> list[tuple[dict[Any, Any], Any, Any]]:
    """The (schema, handler, async prep) row websocket_api registers."""

    async def prep(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
        return {"result": await async_todo_item(hass, msg)}

    def do_todo_item(
        hass: HomeAssistant, msg: dict[str, Any], *, result: dict[str, Any]
    ) -> dict[str, Any]:
        return result

    return [
        (
            {
                vol.Required("type"): WS_TODO_ITEM,
                vol.Required("entity_id"): str,
                vol.Required("action"): vol.In(tuple(_NEEDS)),
                vol.Optional("refs"): [str],
                vol.Optional("uid"): str,
                vol.Optional("data"): dict,
                vol.Optional("due"): vol.Any(str, None),
                # update only: move the item below this uid, or to the top for None
                vol.Optional("previous_uid"): vol.Any(str, None),
            },
            do_todo_item,
            prep,
        )
    ]
