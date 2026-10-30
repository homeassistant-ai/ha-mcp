"""
Todo/Shopping List management tools for Home Assistant.

This module provides tools for managing Home Assistant todo lists including:
- Listing all todo list entities
- Getting items from a todo list
- Creating, updating and moving todo items
- Removing items from a todo list

Adding, updating and removing named items go through the component's
``ha_mcp_tools/todo_item`` bridge when the component serves it (see
``component_todo``), and through Core's todo services over WebSocket
otherwise; the REST service endpoint answers a schema rejection with a bare
400. A move together with an update goes through the bridge too; a move alone
(``todo/item/move``) and ``remove_completed`` use Core's WebSocket commands
on both paths. Either way Core validates the write and its own error text
reaches the caller.
"""

import json
import logging
from typing import Annotated, Any, NoReturn

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backup_todo import (
    back_up_items,
    find_item,
    service_message,
    unbacked_removals,
)
from ..client.rest_client import (
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .coercion import JSON_STRING_COERCION
from .component_todo import todo_call, todo_write
from .helpers import (
    clearable_value,
    exception_to_structured_error,
    get_entity_state_or_none,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
    ws_failure_code,
)
from .tool_hints import read_only_hints, write_hints

logger = logging.getLogger(__name__)


def _not_read_back(item: str) -> str:
    return (
        f"'{item}' was not on the list read back after the write; "
        "read it with ha_get_todo()"
    )


_NEW_ITEM_UNCLEAR = (
    "Could not tell the new item apart from other changes to the list; "
    "read it with ha_get_todo()"
)


def _json_names(value: str) -> list[str] | None:
    """The list a JSON array of strings holds, as some MCP clients send a
    list; None for any other string."""
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    if isinstance(parsed, list) and all(isinstance(i, str) for i in parsed):
        return parsed
    return None


def _raise_core_failure(
    command: str, result: dict[str, Any], context: dict[str, Any], applied: str = ""
) -> NoReturn:
    error = str(result.get("error", "Unknown error")).removeprefix("Command failed: ")
    raise_tool_error(
        create_error_response(
            ws_failure_code(result),
            f"{command} failed: {error}{applied}",
            context=context,
        )
    )


def _removed(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]] | None,
    refs: list[str] | None,
) -> list[dict[str, Any]] | None:
    """The asked-for items gone after a removal (``refs`` None: the completed
    ones); another client's removal of an item the call did not ask for is
    not this call's. None when the list was not read back."""
    if after is None:
        return None
    kept = {i.get("uid") for i in after}
    gone = [i for i in before if i.get("uid") not in kept]
    if refs is None:
        return [i for i in gone if i.get("status") == "completed"]
    return [i for i in gone if i.get("uid") in refs or i.get("summary") in refs]


def _refuse_namesake(entity_id: str, ref: str, item: str) -> NoReturn:
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"Another item on {entity_id} is named {ref!r}, so Home "
            "Assistant would change that one; nothing was changed",
            context={"entity_id": entity_id, "item": item},
            suggestions=["Rename the item named after the uid first"],
        )
    )


def _require_todo_entity(entity_id: str) -> None:
    if not entity_id.startswith("todo."):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid entity_id: {entity_id}. Must start with 'todo.'",
                context={"entity_id": entity_id},
                suggestions=["Use ha_get_todo() to find valid todo list entity IDs"],
            )
        )


class TodoTools:
    """Todo/Shopping List management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _list_state(self, entity_id: str) -> dict[str, Any]:
        """The list's state; an unknown list is not found."""
        state = await get_entity_state_or_none(self._client, entity_id)
        if state is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.ENTITY_NOT_FOUND,
                    f"Home Assistant has no todo list {entity_id}",
                    context={"entity_id": entity_id},
                    suggestions=["Use ha_get_todo() to find valid todo lists"],
                )
            )
        return state

    async def _require_available(self, entity_id: str) -> None:
        """Core's services skip an unavailable list: a service call changes
        nothing yet answers success. Refuse a write up front."""
        if (await self._list_state(entity_id)).get("state") == "unavailable":
            raise_tool_error(
                create_error_response(
                    ErrorCode.ENTITY_UNAVAILABLE,
                    f"Todo list {entity_id} is unavailable; Home Assistant skips "
                    "an unavailable list, so nothing would be changed",
                    context={"entity_id": entity_id},
                    suggestions=["Check the integration that provides the list"],
                )
            )

    async def _write(
        self,
        label: str,
        message: dict[str, Any],
        context: dict[str, Any],
        applied: str = "",
    ) -> None:
        """Send one write; a write that got no answer is reported as unknown,
        and one never sent after an applied write says what was applied."""
        try:
            result = await self._client.send_websocket_message(message)
        except HomeAssistantCommandNotSent as e:
            if not applied:
                raise
            raise_tool_error(
                create_error_response(
                    ErrorCode.CONNECTION_FAILED,
                    f"{label} was not sent: {e}{applied}",
                    context=context,
                )
            )
        except HomeAssistantConnectionError as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"{label} got no answer, so whether it was applied is "
                    f"unknown: {e}{applied}",
                    context={**context, "write_outcome_unknown": True},
                    suggestions=["Check with ha_get_todo() before retrying"],
                )
            )
        if not result.get("success"):
            _raise_core_failure(label, result, context, applied)

    async def _todo_service(
        self, service: str, entity_id: str, data: dict[str, Any], applied: str = ""
    ) -> None:
        await self._write(
            f"todo.{service}",
            service_message(service, entity_id, data),
            {"entity_id": entity_id, **data},
            applied,
        )

    async def _list_items(self, entity_id: str) -> list[dict[str, Any]]:
        result = await self._client.send_websocket_message(
            {"type": "todo/item/list", "entity_id": entity_id}
        )
        if not result.get("success"):
            _raise_core_failure("todo/item/list", result, {"entity_id": entity_id})
        items = (result.get("result") or {}).get("items")
        if not isinstance(items, list):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"todo/item/list answered without an item list: {result}",
                    context={"entity_id": entity_id},
                )
            )
        return items

    async def _read_unavailable(
        self,
        entity_id: str,
        state: dict[str, Any],
        status: str | list[str] | None,
    ) -> dict[str, Any]:
        if (state.get("attributes") or {}).get("restored"):
            # Core's placeholder for a registered list with no loaded
            # entity; todo/item/list answers it with "Entity not found".
            raise_tool_error(
                create_error_response(
                    ErrorCode.ENTITY_UNAVAILABLE,
                    f"Todo list {entity_id} is unavailable and Home "
                    "Assistant holds only a placeholder for it (for "
                    "example while its integration is not set up), so "
                    "it has no items to read",
                    context={"entity_id": entity_id},
                    suggestions=["Check the integration that provides the list"],
                )
            )
        # todo.get_items, which applies the status filter, matches no
        # unavailable entity; todo/item/list still answers with the
        # items Core last held, unfiltered.
        if status:
            raise_tool_error(
                create_error_response(
                    ErrorCode.ENTITY_UNAVAILABLE,
                    f"Todo list {entity_id} is unavailable, and Home Assistant "
                    "filters by status only on an available list",
                    context={"entity_id": entity_id, "status": status},
                    suggestions=[
                        "Omit status to read the items Home Assistant last held"
                    ],
                )
            )
        held = await self._list_items(entity_id)
        return {
            "success": True,
            "entity_id": entity_id,
            "status_filter": None,
            "count": len(held),
            "items": held,
            "message": f"Found {len(held)} item(s) in {entity_id}",
            "warnings": [
                f"{entity_id} is unavailable: these are the items Home "
                "Assistant last held"
            ],
        }

    async def _read_back(
        self, entity_id: str
    ) -> tuple[list[dict[str, Any]] | None, list[str]]:
        """The list after a write that Core applied. A failed read must not
        report that write as failed, or a retry repeats it."""
        try:
            return await self._list_items(entity_id), []
        except (ToolError, HomeAssistantConnectionError) as e:
            return None, [
                f"The write was applied, but the list could not be read back: {e}"
            ]

    async def _resolve(
        self, entity_id: str, refs: list[str]
    ) -> tuple[list[dict[str, Any] | None], list[dict[str, Any]] | None]:
        """The row each ref names, the one Core's todo services act on, and
        the list as read, or None when the component resolved them."""
        reply = await todo_call(self._client, entity_id, "resolve", refs=refs)
        if reply is not None:
            return reply["items"], None
        items = await self._list_items(entity_id)
        return [find_item(items, ref) for ref in refs], items

    @tool(
        name="ha_get_todo",
        tags={"Todo Lists"},
        annotations=read_only_hints("Get Todo", open_world=False),
    )
    @log_tool_usage
    async def ha_get_todo(
        self,
        entity_id: Annotated[
            str | None,
            Field(
                description="Todo list entity ID (e.g., 'todo.shopping_list').",
                default=None,
            ),
        ] = None,
        status: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description="Only items with this status, or with any of a list of "
                "statuses: 'needs_action' (open) or 'completed' (done). "
                "Only applies when entity_id is provided.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get todo lists or items - list all todo lists or get items from a specific list.

        Without an entity_id: lists every entity in the 'todo' domain (shopping
        lists and other todo-type integrations) with entity_id, friendly_name
        and state (number of incomplete items or current status).

        With an entity_id: returns that list's items as Home Assistant reports
        them — uid, summary, status, and description, due and completed where
        set.

        EXAMPLES:
        - List all todo lists: ha_get_todo()
        - Get incomplete items: ha_get_todo("todo.shopping_list", status="needs_action")
        """
        try:
            if entity_id is None:
                states = await self._client.get_states()

                todo_lists = []
                for state in states:
                    eid = state.get("entity_id", "")
                    if eid.startswith("todo."):
                        todo_lists.append(
                            {
                                "entity_id": eid,
                                "friendly_name": state.get("attributes", {}).get(
                                    "friendly_name", eid
                                ),
                                "state": state.get("state"),
                                "icon": state.get("attributes", {}).get("icon"),
                                "supported_features": state.get("attributes", {}).get(
                                    "supported_features"
                                ),
                            }
                        )

                return {
                    "success": True,
                    "count": len(todo_lists),
                    "todo_lists": todo_lists,
                    "message": f"Found {len(todo_lists)} todo list(s)",
                }

            _require_todo_entity(entity_id)
            state = await self._list_state(entity_id)
            if state.get("state") == "unavailable":
                return await self._read_unavailable(entity_id, state, status)
            message = service_message(
                "get_items", entity_id, {"status": status} if status else {}
            )
            result = await self._client.send_websocket_message(
                {**message, "return_response": True}
            )
            if not result.get("success"):
                _raise_core_failure("todo.get_items", result, {"entity_id": entity_id})
            response = (result.get("result") or {}).get("response") or {}
            items = (response.get(entity_id) or {}).get("items")
            if not isinstance(items, list):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"todo.get_items answered without items for {entity_id}",
                        context={"entity_id": entity_id, "response": response},
                    )
                )
            return {
                "success": True,
                "entity_id": entity_id,
                "status_filter": status,
                "count": len(items),
                "items": items,
                "message": f"Found {len(items)} item(s) in {entity_id}",
            }

        except ToolError:
            raise
        except Exception as e:
            context: dict[str, Any] = {}
            if entity_id:
                context["entity_id"] = entity_id
            suggestions = (
                [
                    "Check Home Assistant connection",
                    "Verify entity_id is correct",
                    "Use ha_get_todo() to find valid todo lists",
                ]
                if entity_id
                else [
                    "Check Home Assistant connection",
                    "Verify todo integration is enabled",
                ]
            )
            exception_to_structured_error(
                e, context=context or None, suggestions=suggestions
            )
            raise  # unreachable: exception_to_structured_error always raises

    @tool(
        name="ha_set_todo_item",
        tags={"Todo Lists"},
        annotations=write_hints(
            "Set Todo Item", destructive=True, idempotent=False, open_world=False
        ),
    )
    @log_tool_usage
    async def ha_set_todo_item(
        self,
        entity_id: Annotated[
            str,
            Field(description="Todo list entity ID (e.g., 'todo.shopping_list')"),
        ],
        summary: Annotated[
            str | None,
            Field(
                description="Item text/name. "
                "Ignored in update mode — use 'rename' to change the item name.",
                default=None,
            ),
        ] = None,
        item: Annotated[
            str | None,
            Field(
                description="Existing item to update - can be the item UID or the exact item summary/name.",
                default=None,
            ),
        ] = None,
        status: Annotated[
            str | None,
            Field(
                description="Item status: 'completed' to mark done, 'needs_action' to "
                "mark incomplete. Update mode only: to add a done item, create it, "
                "then update it.",
                default=None,
            ),
        ] = None,
        description: Annotated[
            str | None,
            Field(
                description="Detailed description for the item. "
                "An empty string clears it on update.",
                default=None,
            ),
        ] = None,
        due_date: Annotated[
            str | None,
            Field(
                description="Due date in YYYY-MM-DD format (e.g., '2024-12-25'). "
                "An empty string clears the due date on update.",
                default=None,
            ),
        ] = None,
        due_datetime: Annotated[
            str | None,
            Field(
                description="Due datetime in ISO format (e.g., '2024-12-25T14:00:00'). "
                "An empty string clears the due date on update.",
                default=None,
            ),
        ] = None,
        rename: Annotated[
            str | None,
            Field(
                description="New name/summary for an existing item.",
                default=None,
            ),
        ] = None,
        move_after: Annotated[
            str | None,
            Field(
                description="Move the item directly below this item (UID or exact "
                "summary). An empty string moves it to the top of the list.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Create, update or reorder a todo item in Home Assistant.

        Without `item` (create mode): creates a new item from summary.
        With `item` (update mode): changes the fields given, and moves the item
        when `move_after` is given, alone or together with other changes.
        Home Assistant refuses a description or due date the list does not
        support, and says which. The reply carries `todo_item`, the item as
        Home Assistant stores it afterwards, uid included, or null with a
        warning when it cannot be read back. With auto-backup on, an updated
        item is backed up first; `warnings` names a backup that was not taken
        because the item could not be read or the backup could not start.

        EXAMPLES:
        - Add item: ha_set_todo_item("todo.tasks", summary="Pay bills", due_date="2024-12-31")
        - Complete item: ha_set_todo_item("todo.shopping_list", item="Buy milk", status="completed")
        - Rename item: ha_set_todo_item("todo.tasks", item="Old task", rename="New task name")
        - Move to top: ha_set_todo_item("todo.tasks", item="Pay bills", move_after="")
        """
        _require_todo_entity(entity_id)

        # ``item`` is the implicit create/update discriminator: ``None`` routes
        # to create, anything else to update, so a blank ``item`` must not
        # reach Core as an update of an empty name.
        if item is not None:
            validate_identifier_not_empty(
                item,
                "item",
                suggestions=[
                    "Use ha_get_todo() to list items and obtain a valid UID or summary",
                    "Omit the item parameter entirely to create a new todo item",
                ],
                context={"entity_id": entity_id, "action": "update"},
            )

        try:
            await self._require_available(entity_id)
            given: dict[str, str | None] = {
                k: v
                for k, v in (("rename", rename), ("status", status))
                if v is not None
            }
            if item is None:
                fields = {
                    "description": description or None,
                    "due_date": due_date or None,
                    "due_datetime": due_datetime or None,
                }
                given |= {k: v for k, v in fields.items() if v is not None}
                return await self._create_item(entity_id, summary, given, move_after)
            # A blank description or due date is sent as None, which clears
            # it; any other value goes to Core as given, as on create.
            given |= {
                name: clearable_value(value, name) and value
                for name, value in (
                    ("description", description),
                    ("due_date", due_date),
                    ("due_datetime", due_datetime),
                )
                if value is not None
            }
            return await self._update_item(entity_id, item, given, move_after)
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"entity_id": entity_id, "item": item or summary},
                suggestions=["Verify the entity_id exists using ha_get_todo()"],
            )
            raise  # unreachable: exception_to_structured_error always raises

    async def _create_item(
        self,
        entity_id: str,
        summary: str | None,
        fields: dict[str, Any],
        move_after: str | None,
    ) -> dict[str, Any]:
        if summary is None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "summary is required when creating a new item (no item parameter provided)",
                    context={"entity_id": entity_id},
                    suggestions=["Provide a summary for the new todo item"],
                )
            )
        if move_after is not None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "move_after needs an existing item (provide 'item')",
                    context={"entity_id": entity_id},
                    suggestions=["Create the item first, then move it with item=<uid>"],
                )
            )
        data = {"item": summary, **fields}
        reply = await todo_call(self._client, entity_id, "add", data=data)
        if reply is not None:
            todo_item, warnings = reply["item"], []
            if todo_item is None:
                warnings.append(_NEW_ITEM_UNCLEAR)
        else:
            todo_item, warnings = await self._create_item_ws(entity_id, summary, data)
        response: dict[str, Any] = {
            "success": True,
            "entity_id": entity_id,
            "item": summary,
            "todo_item": todo_item,
            "message": f"Added '{todo_item['summary']}' to {entity_id}"
            if todo_item is not None
            else f"Sent '{summary}' to {entity_id}; the new item is not confirmed",
        }
        if warnings:
            response["warnings"] = warnings
        return response

    async def _create_item_ws(
        self, entity_id: str, summary: str, data: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """The item Core's add_item stored, told apart by the list read back."""
        known = {i.get("uid") for i in await self._list_items(entity_id)}
        await self._todo_service("add_item", entity_id, data)
        after, warnings = await self._read_back(entity_id)
        # Core strips the summary it stores; an item another client adds with
        # another summary is not taken for ours.
        stored = summary.strip()
        added = [
            i
            for i in after or []
            if i.get("uid") not in known and i.get("summary") == stored
        ]
        todo_item = added[0] if len(added) == 1 else None
        if after is not None and todo_item is None:
            warnings.append(_NEW_ITEM_UNCLEAR)
        return todo_item, warnings

    async def _update_item(
        self,
        entity_id: str,
        item: str,
        updates: dict[str, Any],
        move_after: str | None,
    ) -> dict[str, Any]:
        rows, items = await self._resolve(
            entity_id, [item, move_after] if move_after else [item]
        )
        backup_warnings = await back_up_items(
            self._client, entity_id, rows[:1], "ha_set_todo_item"
        )
        if items is None:
            todo_item, warnings = await self._update_by_uid(
                entity_id, item, rows, updates, move_after
            )
        else:
            todo_item, warnings = await self._update_item_ws(
                entity_id, item, rows, items, updates, move_after
            )
        warnings = backup_warnings + warnings
        response: dict[str, Any] = {
            "success": True,
            "entity_id": entity_id,
            "item": item,
            "todo_item": todo_item,
            "message": f"Updated '{item}' in {entity_id}",
        }
        if warnings:
            response["warnings"] = warnings
        return response

    async def _update_by_uid(
        self,
        entity_id: str,
        item: str,
        rows: list[dict[str, Any] | None],
        updates: dict[str, Any],
        move_after: str | None,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """Update the row the component resolved, by its uid; the component
        refuses the update when an item listed before it is named after that
        uid, and moves the item after it. A move alone takes Core's
        todo/item/move."""
        uid = self._uid_of(rows[0], item, entity_id)
        warnings: list[str] = []
        if updates or move_after is None:
            # Core decides whether the update names anything to change.
            fields: dict[str, Any] = {"uid": uid, "data": updates}
            if move_after is not None:
                fields["previous_uid"] = self._previous_uid(rows, move_after, entity_id)
            reply = await todo_write(self._client, entity_id, "update", **fields)
            todo_item = reply["item"]
        else:
            await self._move_item(entity_id, item, rows, move_after)
            after, warnings = await self._read_back(entity_id)
            if after is None:
                return None, warnings
            todo_item = next((i for i in after if i.get("uid") == uid), None)
        if todo_item is None:
            warnings.append(_not_read_back(item))
        return todo_item, warnings

    async def _update_item_ws(
        self,
        entity_id: str,
        item: str,
        rows: list[dict[str, Any] | None],
        items: list[dict[str, Any]],
        updates: dict[str, Any],
        move_after: str | None,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        target = rows[0]
        # The uid keeps the update and the read-back on the item resolved here;
        # after a move, a name shared with another item can resolve to that one.
        uid = (target or {}).get("uid")
        ref = uid or item
        update = bool(updates) or move_after is None
        # Core resolves an item value to the first row whose uid or summary
        # equals it. Without a move, that is decided by the list as read; a move
        # to the top puts the item first; a move below another item can put a
        # row named after the uid before it.
        if move_after is None:
            resolves_elsewhere = find_item(items, ref) is not target
        else:
            resolves_elsewhere = bool(move_after) and any(
                i is not target and i.get("summary") == uid for i in items
            )
        if update and uid and resolves_elsewhere:
            _refuse_namesake(entity_id, ref, item)
        # Moved first, so a list without reordering refuses before the update
        # is written.
        applied = await self._move_item(entity_id, item, rows, move_after)
        if update:
            # Core decides whether the update names anything to change.
            await self._todo_service(
                "update_item", entity_id, {"item": ref, **updates}, applied
            )
        after, warnings = await self._read_back(entity_id)
        # By uid alone: after a move, another item can be named after it.
        todo_item = (
            next((i for i in after or [] if i.get("uid") == uid), None)
            if uid
            else find_item(after or [], item)
        )
        if after is not None and todo_item is None:
            warnings.append(_not_read_back(item))
        return todo_item, warnings

    @staticmethod
    def _uid_of(found: dict[str, Any] | None, ref: str, entity_id: str) -> str:
        if not (found and found.get("uid")):
            raise_tool_error(
                create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    f"No item {ref!r} on {entity_id}",
                    context={"entity_id": entity_id, "item": ref},
                    suggestions=["Use ha_get_todo() to list the items"],
                )
            )
        return str(found["uid"])

    def _previous_uid(
        self, rows: list[dict[str, Any] | None], move_after: str, entity_id: str
    ) -> str | None:
        """The uid of the item ``move_after`` names; None for the top."""
        return self._uid_of(rows[1], move_after, entity_id) if move_after else None

    async def _move_item(
        self,
        entity_id: str,
        item: str,
        rows: list[dict[str, Any] | None],
        move_after: str | None,
    ) -> str:
        """Core's todo/item/move of ``rows[0]``, which takes uids; an empty
        ``move_after`` puts the item at the top, None moves nothing. Returns
        the note for a later failure."""
        if move_after is None:
            return ""
        message = {
            "type": "todo/item/move",
            "entity_id": entity_id,
            "uid": self._uid_of(rows[0], item, entity_id),
        }
        if previous_uid := self._previous_uid(rows, move_after, entity_id):
            message["previous_uid"] = previous_uid
        await self._write(
            "todo/item/move", message, {"entity_id": entity_id, "item": item}
        )
        return "; the move was applied"

    @tool(
        name="ha_remove_todo_item",
        tags={"Todo Lists"},
        annotations=write_hints(
            "Remove Todo Item", destructive=True, idempotent=False, open_world=False
        ),
    )
    @log_tool_usage
    async def ha_remove_todo_item(
        self,
        entity_id: Annotated[
            str,
            Field(description="Todo list entity ID (e.g., 'todo.shopping_list')"),
        ],
        item: Annotated[
            str | list[str] | None,
            Field(
                description="Item to remove, or a list of items - each the item UID "
                "(from ha_get_todo) or the exact item summary/name. A string that "
                "names no item but holds a JSON array of strings is that list.",
                default=None,
            ),
        ] = None,
        remove_completed: Annotated[
            bool,
            Field(
                description="Remove every completed item",
                default=False,
            ),
        ] = False,
    ) -> dict[str, Any]:
        """
        Remove items from a Home Assistant todo list.

        Give `item` or `remove_completed=True`, not both. Removal is permanent;
        to mark an item done instead, use ha_set_todo_item() with
        status="completed". `removed` lists the items the call asked for that
        are gone afterwards; with remove_completed=True that includes a
        completed item another client removed meanwhile. With auto-backup on,
        each item is backed up before it is removed; `warnings` names a backup
        not taken because the item could not be read or the backup could not
        start, and with remove_completed=True an item gone afterwards that was
        not completed when the list was read, so was not backed up.

        EXAMPLES:
        - ha_remove_todo_item("todo.shopping_list", "Buy milk")
        - ha_remove_todo_item("todo.shopping_list", ["Buy milk", "Bread"])
        - ha_remove_todo_item("todo.shopping_list", remove_completed=True)
        """
        _require_todo_entity(entity_id)
        if item == []:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "item is an empty list; name at least one item",
                    context={"entity_id": entity_id},
                )
            )
        if (item is None) == (not remove_completed):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Give either item or remove_completed=True",
                    context={"entity_id": entity_id},
                    suggestions=[
                        "Use ha_get_todo() to list items and obtain a UID or summary"
                    ],
                )
            )
        # Core accepts an empty item name and then reports it as not found.
        for ref in [item] if isinstance(item, str) else item or []:
            validate_identifier_not_empty(
                ref,
                "item",
                suggestions=[
                    "Use ha_get_todo() to list items and obtain a valid UID or summary",
                ],
                context={"entity_id": entity_id},
            )

        try:
            await self._require_available(entity_id)
            if remove_completed:
                removed, warnings = await self._remove_completed(entity_id)
            else:
                removed, warnings = await self._remove_items(entity_id, item or [])
            response: dict[str, Any] = {
                "success": True,
                "entity_id": entity_id,
                "removed": removed,
                "message": f"Removed {'the' if removed is None else len(removed)} "
                f"item(s) from {entity_id}",
            }
            if warnings:
                response["warnings"] = warnings
            return response

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"entity_id": entity_id, "item": item},
                suggestions=["Verify the items exist using ha_get_todo()"],
            )
            raise  # unreachable: exception_to_structured_error always raises

    async def _remove_completed(
        self, entity_id: str
    ) -> tuple[list[dict[str, Any]] | None, list[str]]:
        before = await self._list_items(entity_id)
        warnings = await back_up_items(
            self._client,
            entity_id,
            [i for i in before if i.get("status") == "completed"],
            "ha_remove_todo_item",
        )
        await self._todo_service("remove_completed_items", entity_id, {})
        after, read_warnings = await self._read_back(entity_id)
        warnings += unbacked_removals(before, after) + read_warnings
        return _removed(before, after, None), warnings

    async def _remove_items(
        self, entity_id: str, item: str | list[str]
    ) -> tuple[list[dict[str, Any]] | None, list[str]]:
        refs = [item] if isinstance(item, str) else item
        rows, before = await self._resolve(entity_id, refs)
        if isinstance(item, str) and rows[0] is None:
            # A string that names an item is a name; only one that names none
            # is read as the JSON list some MCP clients send.
            names = _json_names(item)
            if names == []:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"No item {item!r} on {entity_id}, and as a list it names "
                        "no item",
                        context={"entity_id": entity_id, "item": item},
                        suggestions=["Use ha_get_todo() to list the items"],
                    )
                )
            if names:
                refs = names
                rows, before = await self._resolve(entity_id, refs)
        for ref, row in zip(refs, rows, strict=True):
            self._uid_of(row, ref, entity_id)  # refuses an unknown item
        warnings = await back_up_items(
            self._client, entity_id, rows, "ha_remove_todo_item"
        )
        read_warnings: list[str] = []
        if before is None:
            reply = await todo_write(self._client, entity_id, "remove", refs=refs)
            removed = reply["removed"]
        else:
            await self._todo_service("remove_item", entity_id, {"item": refs})
            after, read_warnings = await self._read_back(entity_id)
            removed = _removed(before, after, refs)
        if removed == []:
            # Core refuses to remove an item it does not hold, so an
            # unchanged list is a stale read.
            read_warnings.append(
                "The list read back after the removal still holds the items; "
                "check with ha_get_todo()"
            )
        return removed, warnings + read_warnings


def register_todo_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant todo list management tools."""
    register_tool_methods(mcp, TodoTools(client))
