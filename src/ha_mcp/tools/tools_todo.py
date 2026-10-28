"""
Todo/Shopping List management tools for Home Assistant.

This module provides tools for managing Home Assistant todo lists including:
- Listing all todo list entities
- Getting items from a todo list
- Creating, updating and moving todo items
- Removing items from a todo list

Item writes go through Core's todo services over WebSocket, and a move
through Core's ``todo/item/move`` command, so Core validates them and its own
error text reaches the caller; the REST service endpoint answers a schema
rejection with a bare 400.
"""

import json
import logging
from typing import Annotated, Any, NoReturn

from pydantic import BeforeValidator, Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..backup_todo import find_item, service_message
from ..client.rest_client import (
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .auto_backup import with_auto_backup
from .coercion import JSON_STRING_COERCION
from .helpers import (
    clearable_value,
    exception_to_structured_error,
    get_entity_state_or_none,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .tool_hints import read_only_hints, write_hints

logger = logging.getLogger(__name__)

# Core's codes for input it refuses; ``not_supported`` is todo/item/move's
# refusal for a list without reordering.
_CORE_INPUT_ERRORS = ("invalid_format", "service_validation_error", "not_supported")


def _items_if_json_list(value: Any) -> Any:
    """A JSON array of strings, as some MCP clients send a list, becomes the
    list; any other string is an item name, which may start with '['."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        if isinstance(parsed, list) and all(isinstance(i, str) for i in parsed):
            return parsed
    return value


def _raise_core_failure(
    command: str, result: dict[str, Any], context: dict[str, Any], applied: str = ""
) -> NoReturn:
    error = str(result.get("error", "Unknown error")).removeprefix("Command failed: ")
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER
            if result.get("error_code") in _CORE_INPUT_ERRORS
            else ErrorCode.SERVICE_CALL_FAILED,
            f"{command} failed: {error}{applied}",
            context=context,
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

    async def _list_unavailable(self, entity_id: str) -> bool:
        """Whether the list is unavailable; an unknown list is not found."""
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
        return bool(state.get("state") == "unavailable")

    async def _require_available(self, entity_id: str) -> None:
        """Core's services skip an unavailable list: a service call changes
        nothing yet answers success. Refuse a write up front."""
        if await self._list_unavailable(entity_id):
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
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
        """Send one write; a write that got no answer is reported as unknown."""
        try:
            result = await self._client.send_websocket_message(message)
        except HomeAssistantCommandNotSent:
            raise
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
            if await self._list_unavailable(entity_id):
                # todo.get_items matches no unavailable entity; the list
                # command still answers with the items Core last held.
                held = await self._list_items(entity_id)
                return {
                    "success": True,
                    "entity_id": entity_id,
                    "status_filter": status,
                    "count": len(held),
                    "items": held,
                    "message": f"Found {len(held)} item(s) in {entity_id}",
                    "warnings": [
                        f"{entity_id} is unavailable: these are the items Home "
                        "Assistant last held"
                        + (
                            ", unfiltered, since Home Assistant applies the "
                            "status filter only to an available list"
                            if status
                            else ""
                        )
                    ],
                }
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
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    @tool(
        name="ha_set_todo_item",
        tags={"Todo Lists"},
        annotations=write_hints(
            "Set Todo Item", destructive=True, idempotent=False, open_world=False
        ),
    )
    @with_auto_backup(
        domain="todo_item",
        id_fn=lambda kw: f"{kw.get('entity_id', '')}::{kw.get('item') or ''}",
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
        warning when it cannot be read back.

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
            # A None description or due date clears it.
            given |= {
                name: clearable_value(value, name)
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
        if not summary:
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
        known = {i.get("uid") for i in await self._list_items(entity_id)}
        await self._todo_service("add_item", entity_id, {"item": summary, **fields})
        after, warnings = await self._read_back(entity_id)
        added = [i for i in after or [] if i.get("uid") not in known]
        todo_item = added[0] if len(added) == 1 else None
        if after is not None and todo_item is None:
            warnings.append(
                "Could not tell the new item apart from other changes to the list; "
                "read it with ha_get_todo()"
            )
        response: dict[str, Any] = {
            "success": True,
            "entity_id": entity_id,
            "item": summary,
            "todo_item": todo_item,
            "message": f"Added '{summary}' to {entity_id}",
        }
        if warnings:
            response["warnings"] = warnings
        return response

    async def _update_item(
        self,
        entity_id: str,
        item: str,
        updates: dict[str, Any],
        move_after: str | None,
    ) -> dict[str, Any]:
        items = await self._list_items(entity_id)
        target = find_item(items, item)
        applied = ""
        if move_after is not None:
            # Moved first, so a list without reordering refuses before the
            # update is written.
            await self._move_item(entity_id, item, target, items, move_after)
            applied = "; the move was applied"
        if updates or move_after is None:
            # Core decides whether the update names anything to change.
            await self._todo_service(
                "update_item", entity_id, {"item": item, **updates}, applied
            )
        after, warnings = await self._read_back(entity_id)
        todo_item = (
            find_item(after, (target or {}).get("uid") or item) if after else None
        )
        if after is not None and todo_item is None:
            warnings.append(
                f"'{item}' was not on the list read back after the write; "
                "read it with ha_get_todo()"
            )
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

    async def _move_item(
        self,
        entity_id: str,
        item: str,
        target: dict[str, Any] | None,
        items: list[dict[str, Any]],
        move_after: str,
    ) -> None:
        """todo/item/move takes uids; resolve the given names the way Core's
        services resolve ``item``."""
        message = {
            "type": "todo/item/move",
            "entity_id": entity_id,
            "uid": self._uid_of(target, item, entity_id),
        }
        if move_after:
            message["previous_uid"] = self._uid_of(
                find_item(items, move_after), move_after, entity_id
            )
        await self._write(
            "todo/item/move", message, {"entity_id": entity_id, "item": item}
        )

    @tool(
        name="ha_remove_todo_item",
        tags={"Todo Lists"},
        annotations=write_hints(
            "Remove Todo Item", destructive=True, idempotent=True, open_world=False
        ),
    )
    @with_auto_backup(
        domain="todo_item",
        id_fn=lambda kw: (
            f"{kw['entity_id']}::{kw['item']}"
            if kw.get("entity_id") and isinstance(kw.get("item"), str) and kw["item"]
            else ""
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
            BeforeValidator(_items_if_json_list),
            Field(
                description="Item to remove, or a list of items - each the item UID "
                "(from ha_get_todo) or the exact item summary/name",
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
        status="completed". Only an item named as a plain string gets an
        automatic backup; a list and remove_completed do not.

        EXAMPLES:
        - ha_remove_todo_item("todo.shopping_list", "Buy milk")
        - ha_remove_todo_item("todo.shopping_list", ["Buy milk", "Bread"])
        - ha_remove_todo_item("todo.shopping_list", remove_completed=True)
        """
        _require_todo_entity(entity_id)
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
            before = await self._list_items(entity_id)
            if remove_completed:
                await self._todo_service("remove_completed_items", entity_id, {})
            else:
                await self._todo_service("remove_item", entity_id, {"item": item})
            after, warnings = await self._read_back(entity_id)
            kept = {i.get("uid") for i in after or []}
            removed = (
                [i for i in before if i.get("uid") not in kept]
                if after is not None
                else None
            )
            if removed == [] and not remove_completed:
                # Core refuses to remove an item it does not hold, so an
                # unchanged list is a stale read.
                warnings.append(
                    "The list read back after the removal still holds the items; "
                    "check with ha_get_todo()"
                )
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


def register_todo_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant todo list management tools."""
    register_tool_methods(mcp, TodoTools(client))
