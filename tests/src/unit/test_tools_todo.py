"""Todo tools on an install without the component: requests go to Core's todo
services and commands over WebSocket, and the reply is what Core holds.

The tools run on the server's own client against a real ``hass``: Core's Local
To-do list ``todo.tasks``, or a test platform's list (``plain_list``) for a
backend Local To-do is not: one without reordering, one that goes unavailable,
one that stores a new item as some other row. The component's bridge is
covered in ``test_tools_todo_bridge.py``.
"""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_todo import TodoTools

from ._backup_todo_core import (
    LIST,
    PLAIN_FEATURES,
    PlainList,
    add_item,
    listed,
    move_to_top,
    row_named,
    take_offline,
    write_item,
)
from ._backup_todo_core import plain_list as plain_list
from ._backup_todo_core import tasks as tasks
from ._backup_todo_core import todo_core as todo_core
from ._command_faults import dropped_on, pooled_socket, received
from ._receipt_faults import on_receipt
from ._tool_errors import error_body, error_of

SERVICE = "call_service"
MOVE = "todo/item/move"


@pytest.fixture
async def shopping(hass: HomeAssistant, tasks: MockConfigEntry) -> dict[str, str]:
    """``Buy milk``, then ``Pay`` and ``Bread``, both completed, on Local
    To-do's list; the uid Core gave each, by summary."""
    return {
        "Buy milk": await add_item(hass, "Buy milk"),
        "Pay": await add_item(hass, "Pay", completed=True),
        "Bread": await add_item(hass, "Bread", completed=True),
    }


@pytest.fixture
def tools(core_client: HomeAssistantClient) -> TodoTools:
    return TodoTools(core_client)


def services(seen: dict[str, list[Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Each todo service Core received, with the data the tool sent; Core adds
    the target to that data as it handles the call."""
    return [
        (
            msg["service"],
            {k: v for k, v in msg["service_data"].items() if k != "entity_id"},
        )
        for msg in seen[SERVICE]
    ]


def answers_lost(client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """From now on the client's WebSocket messages get no answer."""
    monkeypatch.setattr(
        client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
    )


def sends_lost(client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """From now on the client cannot send its WebSocket messages."""
    monkeypatch.setattr(
        client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantCommandNotSent("not sent")),
    )


def go_unavailable(plain_list: PlainList) -> None:
    """The list's backend goes away, and Core's state says so."""
    plain_list.is_available = False
    plain_list.async_write_ha_state()


class TestGetTodo:
    async def test_a_list_of_statuses_reaches_core_as_given(
        self, hass: HomeAssistant, tools: TodoTools, shopping: dict[str, str]
    ) -> None:
        with received(hass, SERVICE) as seen:
            got = await tools.ha_get_todo(LIST, status=["needs_action", "completed"])
        assert services(seen) == [
            ("get_items", {"status": ["needs_action", "completed"]})
        ]
        assert got["count"] == 3

    async def test_one_status_returns_only_the_items_with_it(
        self, tools: TodoTools, shopping: dict[str, str]
    ) -> None:
        got = await tools.ha_get_todo(LIST, status="completed")
        assert [i["summary"] for i in got["items"]] == ["Pay", "Bread"]

    async def test_no_status_returns_every_item(
        self, tools: TodoTools, shopping: dict[str, str]
    ) -> None:
        # Core answers get_items only when a response is asked for, and
        # refuses a null status.
        got = await tools.ha_get_todo(LIST)
        assert [i["summary"] for i in got["items"]] == ["Buy milk", "Pay", "Bread"]

    async def test_a_status_core_does_not_know_is_refused_with_cores_text(
        self, tools: TodoTools, shopping: dict[str, str]
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_get_todo(LIST, status="done")
        error = error_of(excinfo)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "status" in error["message"]

    async def test_an_unknown_list_is_not_found_and_core_is_not_asked(
        self, tools: TodoTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_get_todo("todo.gone")
        assert error_of(excinfo)["code"] == "ENTITY_NOT_FOUND"
        assert sent.await_count == 0

    async def test_an_unavailable_list_returns_the_items_core_last_held(
        self, hass: HomeAssistant, tools: TodoTools, plain_list: PlainList
    ) -> None:
        # todo.get_items matches no unavailable entity; todo/item/list answers.
        go_unavailable(plain_list)
        with received(hass, SERVICE) as seen:
            got = await tools.ha_get_todo(LIST)
        assert [i["summary"] for i in got["items"]] == ["Buy milk", "Pay"]
        assert "unavailable" in got["warnings"][0]
        assert seen[SERVICE] == []

    async def test_an_unavailable_list_refuses_a_status_filter(
        self, tools: TodoTools, plain_list: PlainList, sent: AsyncMock
    ) -> None:
        go_unavailable(plain_list)
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_get_todo(LIST, status="completed")
        error = error_of(excinfo)
        assert error["code"] == "ENTITY_UNAVAILABLE"
        assert "status" in error["message"]
        assert sent.await_count == 0

    async def test_a_list_whose_integration_is_not_loaded_is_refused(
        self,
        hass: HomeAssistant,
        tools: TodoTools,
        tasks: MockConfigEntry,
        entity_registry: er.EntityRegistry,
        sent: AsyncMock,
    ) -> None:
        # Core keeps only a placeholder state; todo/item/list would answer
        # "Entity not found".
        await take_offline(hass, entity_registry, tasks, "unavailable")
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_get_todo(LIST)
        error = error_of(excinfo)
        assert error["code"] == "ENTITY_UNAVAILABLE"
        assert "placeholder" in error["message"]
        assert sent.await_count == 0


class TestSetTodoItem:
    async def test_a_created_item_is_the_row_core_stored(
        self,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        tasks: MockConfigEntry,
    ) -> None:
        got = await tools.ha_set_todo_item(LIST, summary="  Eggs ")
        assert got["todo_item"] == row_named(await listed(core_client), "Eggs")
        assert got["message"] == f"Added 'Eggs' to {LIST}"

    async def test_blank_fields_on_create_are_left_out(
        self, hass: HomeAssistant, tools: TodoTools, tasks: MockConfigEntry
    ) -> None:
        with received(hass, SERVICE) as seen:
            got = await tools.ha_set_todo_item(
                LIST, summary="Eggs", description="", due_date="", due_datetime=""
            )
        assert services(seen) == [("add_item", {"item": "Eggs"})]
        assert got["todo_item"]["summary"] == "Eggs"

    async def test_a_status_on_create_is_left_to_core(
        self, hass: HomeAssistant, tools: TodoTools, tasks: MockConfigEntry
    ) -> None:
        with received(hass, SERVICE) as seen, pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, summary="Eggs", status="completed")
        assert services(seen) == [("add_item", {"item": "Eggs", "status": "completed"})]
        assert "status" in error_of(excinfo)["message"]

    async def test_an_empty_string_clears_a_field(
        self, hass: HomeAssistant, tools: TodoTools, shopping: dict[str, str]
    ) -> None:
        await write_item(
            hass, "update_item", "Pay", description="electric", due_date="2026-11-20"
        )
        got = await tools.ha_set_todo_item(
            LIST, item="Pay", description="", due_date=" "
        )
        assert got["todo_item"]["uid"] == shopping["Pay"]
        assert {"description", "due"}.isdisjoint(got["todo_item"])

    async def test_an_update_stores_a_value_as_given(
        self, tools: TodoTools, shopping: dict[str, str]
    ) -> None:
        got = await tools.ha_set_todo_item(LIST, item="Pay", description="  electric  ")
        assert got["todo_item"]["description"] == "  electric  "

    async def test_an_update_naming_no_field_gets_cores_refusal(
        self, hass: HomeAssistant, tools: TodoTools, shopping: dict[str, str]
    ) -> None:
        with received(hass, SERVICE) as seen, pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Pay")
        assert services(seen) == [("update_item", {"item": shopping["Pay"]})]
        assert "rename" in error_of(excinfo)["message"]

    async def test_a_field_the_list_lacks_is_refused_with_cores_text(
        self, tools: TodoTools, plain_list: PlainList
    ) -> None:
        plain_list.features = PLAIN_FEATURES
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Pay", description="x")
        error = error_of(excinfo)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "description" in error["message"]
        assert plain_list.rows[1].description is None

    async def test_a_renamed_item_is_read_back_by_its_uid(
        self, tools: TodoTools, shopping: dict[str, str]
    ) -> None:
        got = await tools.ha_set_todo_item(LIST, item="Pay", rename="Paid")
        row = got["todo_item"]
        assert (row["uid"], row["summary"], row["status"]) == (
            shopping["Pay"],
            "Paid",
            "completed",
        )

    async def test_a_move_alone_takes_cores_move_and_writes_no_update(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        shopping: dict[str, str],
    ) -> None:
        with received(hass, SERVICE, MOVE) as seen:
            got = await tools.ha_set_todo_item(
                LIST, item="Bread", move_after="Buy milk"
            )
        assert seen[SERVICE] == []
        assert [(msg["uid"], msg["previous_uid"]) for msg in seen[MOVE]] == [
            (shopping["Bread"], shopping["Buy milk"])
        ]
        rows = await listed(core_client)
        assert [row["summary"] for row in rows] == ["Buy milk", "Bread", "Pay"]
        assert got["todo_item"]["uid"] == shopping["Bread"]

    async def test_an_empty_move_after_moves_the_item_to_the_top(
        self,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        shopping: dict[str, str],
    ) -> None:
        await tools.ha_set_todo_item(LIST, item="Pay", move_after="")
        assert (await listed(core_client))[0]["summary"] == "Pay"

    async def test_a_move_on_a_list_without_reordering_is_refused_before_the_update(
        self, hass: HomeAssistant, tools: TodoTools, plain_list: PlainList
    ) -> None:
        with received(hass, SERVICE) as seen, pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Pay", rename="Paid", move_after="")
        error = error_of(excinfo)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "does not support To-do item reordering" in error["message"]
        assert seen[SERVICE] == []
        assert plain_list.rows[1].summary == "Pay"

    @pytest.mark.parametrize(
        "fields",
        [
            {"item": "Pay", "move_after": "Nope", "status": "needs_action"},
            {"item": "Nope", "move_after": ""},
        ],
        ids=["unknown_target", "unknown_item"],
    )
    async def test_a_move_naming_an_unknown_item_writes_nothing(
        self,
        hass: HomeAssistant,
        tools: TodoTools,
        shopping: dict[str, str],
        fields: dict[str, str],
    ) -> None:
        with received(hass, SERVICE, MOVE) as seen, pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, **fields)
        assert error_of(excinfo)["code"] == "RESOURCE_NOT_FOUND"
        assert seen == {SERVICE: [], MOVE: []}

    async def test_a_rename_with_a_move_writes_both(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        shopping: dict[str, str],
    ) -> None:
        with received(hass, SERVICE) as seen:
            got = await tools.ha_set_todo_item(
                LIST, item="Bread", rename="Rye", move_after=""
            )
        assert services(seen) == [
            ("update_item", {"item": shopping["Bread"], "rename": "Rye"})
        ]
        assert (await listed(core_client))[0]["summary"] == "Rye"
        assert got["todo_item"]["summary"] == "Rye"

    async def test_create_refuses_move_after(
        self, tools: TodoTools, tasks: MockConfigEntry, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, summary="Eggs", move_after="")
        assert error_of(excinfo)["code"] == "VALIDATION_INVALID_PARAMETER"
        assert sent.await_count == 0

    async def test_create_needs_a_summary(
        self, tools: TodoTools, tasks: MockConfigEntry, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST)
        assert error_of(excinfo)["code"] == "VALIDATION_MISSING_PARAMETER"
        assert sent.await_count == 0


class TestRemoveTodoItem:
    async def test_a_list_of_items_is_removed_in_one_call(
        self, hass: HomeAssistant, tools: TodoTools, shopping: dict[str, str]
    ) -> None:
        refs = ["Pay", shopping["Buy milk"]]
        with received(hass, SERVICE) as seen:
            got = await tools.ha_remove_todo_item(LIST, item=refs)
        assert services(seen) == [("remove_item", {"item": refs})]
        assert [i["summary"] for i in got["removed"]] == ["Buy milk", "Pay"]

    async def test_remove_completed_removes_and_names_the_completed_items(
        self,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        shopping: dict[str, str],
    ) -> None:
        got = await tools.ha_remove_todo_item(LIST, remove_completed=True)
        assert [i["summary"] for i in got["removed"]] == ["Pay", "Bread"]
        assert [row["summary"] for row in await listed(core_client)] == ["Buy milk"]

    @pytest.mark.parametrize(
        "target",
        [{}, {"item": "Pay", "remove_completed": True}],
        ids=["neither", "both"],
    )
    async def test_exactly_one_target_is_required(
        self, tools: TodoTools, sent: AsyncMock, target: dict[str, Any]
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_remove_todo_item(LIST, **target)
        assert error_of(excinfo)["code"] == "VALIDATION_INVALID_PARAMETER"
        assert sent.await_count == 0

    @pytest.mark.parametrize("remove_completed", [False, True])
    async def test_an_empty_item_list_is_refused(
        self, tools: TodoTools, sent: AsyncMock, remove_completed: bool
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_remove_todo_item(
                LIST, item=[], remove_completed=remove_completed
            )
        assert "empty list" in error_of(excinfo)["message"]
        assert sent.await_count == 0

    async def test_a_blank_entry_in_a_list_is_refused(
        self, tools: TodoTools, sent: AsyncMock
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_remove_todo_item(LIST, item=["Pay", " "])
        assert error_of(excinfo)["code"] == "VALIDATION_INVALID_PARAMETER"
        assert sent.await_count == 0

    async def test_an_unknown_item_is_refused_before_anything_is_removed(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        shopping: dict[str, str],
    ) -> None:
        with received(hass, SERVICE) as seen, pytest.raises(ToolError) as excinfo:
            await tools.ha_remove_todo_item(LIST, item=["Pay", "Nope"])
        error = error_of(excinfo)
        assert error["code"] == "RESOURCE_NOT_FOUND"
        assert "'Nope'" in error["message"]
        assert seen[SERVICE] == []
        assert len(await listed(core_client)) == 3

    async def test_cores_refusal_of_a_removal_comes_through(
        self, tools: TodoTools, plain_list: PlainList
    ) -> None:
        plain_list.features = TodoListEntityFeature.UPDATE_TODO_ITEM
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_remove_todo_item(LIST, item="Pay")
        error = error_of(excinfo)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "remove_item" in error["message"]
        assert len(plain_list.rows) == 2

    async def test_a_string_naming_an_item_is_that_item_not_a_json_list(
        self, hass: HomeAssistant, tools: TodoTools, tasks: MockConfigEntry
    ) -> None:
        milk = await add_item(hass, "Milk")
        named_like_a_list = await add_item(hass, '["Milk"]')
        with received(hass, SERVICE) as seen:
            first = await tools.ha_remove_todo_item(LIST, item='["Milk"]')
            # Once no item has that name, the string is the list it holds.
            second = await tools.ha_remove_todo_item(LIST, item='["Milk"]')
        assert [i["uid"] for i in first["removed"]] == [named_like_a_list]
        assert [i["uid"] for i in second["removed"]] == [milk]
        assert services(seen) == [
            ("remove_item", {"item": ['["Milk"]']}),
            ("remove_item", {"item": ["Milk"]}),
        ]

    async def test_a_stringified_list_names_its_items(
        self, tools: TodoTools, shopping: dict[str, str]
    ) -> None:
        got = await tools.ha_remove_todo_item(LIST, item='["Pay", "Bread"]')
        assert [i["uid"] for i in got["removed"]] == [
            shopping["Pay"],
            shopping["Bread"],
        ]

    async def test_an_item_named_like_an_empty_list_is_removed(
        self, hass: HomeAssistant, tools: TodoTools, tasks: MockConfigEntry
    ) -> None:
        uid = await add_item(hass, "[]")
        got = await tools.ha_remove_todo_item(LIST, item="[]")
        assert [i["uid"] for i in got["removed"]] == [uid]
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_remove_todo_item(LIST, item="[]")
        assert "as a list it names no item" in error_of(excinfo)["message"]

    async def test_an_unknown_name_that_is_no_list_is_not_found(
        self, hass: HomeAssistant, tools: TodoTools, shopping: dict[str, str]
    ) -> None:
        with received(hass, SERVICE) as seen, pytest.raises(ToolError) as excinfo:
            await tools.ha_remove_todo_item(LIST, item="[Work] Call Bob")
        assert error_of(excinfo)["code"] == "RESOURCE_NOT_FOUND"
        assert seen[SERVICE] == []

    async def test_removed_names_only_the_items_asked_for(
        self, hass: HomeAssistant, tools: TodoTools, plain_list: PlainList
    ) -> None:
        # Another client removes "Buy milk" while Core removes "Pay".
        with on_receipt(hass, SERVICE, lambda msg: plain_list.rows.pop(0)):
            got = await tools.ha_remove_todo_item(LIST, item="Pay")
        assert plain_list.rows == []
        assert [i["uid"] for i in got["removed"]] == ["2"]


class TestOutcomes:
    """Each write says what it did, also when Core skipped it or the read
    after it failed."""

    async def test_an_unavailable_list_refuses_a_write(
        self, tools: TodoTools, plain_list: PlainList, sent: AsyncMock
    ) -> None:
        # Core skips an unavailable list and still answers success.
        go_unavailable(plain_list)
        with pytest.raises(ToolError) as set_refusal:
            await tools.ha_set_todo_item(LIST, summary="Eggs")
        with pytest.raises(ToolError) as remove_refusal:
            await tools.ha_remove_todo_item(LIST, item="Pay")
        for refusal in (set_refusal, remove_refusal):
            assert error_of(refusal)["code"] == "ENTITY_UNAVAILABLE"
        assert sent.await_count == 0

    async def test_a_failed_read_back_keeps_the_applied_add(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        plain_list: PlainList,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with on_receipt(
            hass, SERVICE, lambda msg: answers_lost(core_client, monkeypatch)
        ):
            got = await tools.ha_set_todo_item(LIST, summary="Eggs")
        assert (got["success"], got["todo_item"]) == (True, None)
        assert "could not be read back" in got["warnings"][0]
        assert plain_list.rows[-1].summary == "Eggs"

    async def test_a_failed_read_back_keeps_the_applied_removal(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        plain_list: PlainList,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with on_receipt(
            hass, SERVICE, lambda msg: answers_lost(core_client, monkeypatch)
        ):
            got = await tools.ha_remove_todo_item(LIST, item="Pay")
        assert got["removed"] is None
        assert len(got["warnings"]) == 1
        assert "could not be read back" in got["warnings"][0]
        assert [row.summary for row in plain_list.rows] == ["Buy milk"]

    async def test_a_removal_still_on_the_list_read_back_is_flagged(
        self,
        tools: TodoTools,
        plain_list: PlainList,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def deleted_on_next_sync(uids: list[str]) -> None:
            """A backend that drops the rows only when the list syncs again."""

        monkeypatch.setattr(plain_list, "async_delete_todo_items", deleted_on_next_sync)
        got = await tools.ha_remove_todo_item(LIST, item="Pay")
        assert got["removed"] == []
        assert "still holds" in got["warnings"][0]

    async def test_an_item_gone_after_its_update_is_flagged(
        self,
        tools: TodoTools,
        plain_list: PlainList,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def filed_away(item: TodoItem) -> None:
            """A backend that files an updated item off the list."""
            plain_list.rows[:] = [row for row in plain_list.rows if row.uid != item.uid]

        monkeypatch.setattr(plain_list, "async_update_todo_item", filed_away)
        got = await tools.ha_set_todo_item(LIST, item="Pay", status="needs_action")
        assert got["todo_item"] is None
        assert "not on the list" in got["warnings"][0]

    async def test_an_update_refused_after_a_move_says_the_move_happened(
        self,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        shopping: dict[str, str],
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(
                LIST, item="Pay", due_date="someday", move_after=""
            )
        assert "the move was applied" in error_of(excinfo)["message"]
        assert (await listed(core_client))[0]["uid"] == shopping["Pay"]

    async def test_a_move_without_an_answer_is_an_unknown_outcome(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        shopping: dict[str, str],
    ) -> None:
        socket = await pooled_socket(core_client)
        with dropped_on(hass, MOVE, socket), pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Pay", move_after="")
        assert error_body(excinfo)["write_outcome_unknown"] is True

    async def test_a_failed_list_read_writes_nothing(
        self,
        hass: HomeAssistant,
        tools: TodoTools,
        todo_core: None,
    ) -> None:
        # A todo state with no list behind it: todo/item/list answers
        # "Entity not found".
        hass.states.async_set(LIST, "1")
        with received(hass, SERVICE) as seen:
            with pytest.raises(ToolError):
                await tools.ha_set_todo_item(LIST, summary="Eggs")
            with pytest.raises(ToolError):
                await tools.ha_remove_todo_item(LIST, item="Pay")
        assert seen[SERVICE] == []

    async def test_a_write_without_an_answer_is_an_unknown_outcome(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        tasks: MockConfigEntry,
    ) -> None:
        socket = await pooled_socket(core_client)
        with dropped_on(hass, SERVICE, socket), pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, summary="Eggs")
        assert error_body(excinfo)["write_outcome_unknown"] is True

    async def test_a_write_never_sent_is_not_an_unknown_outcome(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        tasks: MockConfigEntry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Core answers the read before the add; the add is never sent.
        with (
            on_receipt(
                hass, "todo/item/list", lambda msg: sends_lost(core_client, monkeypatch)
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await tools.ha_set_todo_item(LIST, summary="Eggs")
        assert "write_outcome_unknown" not in error_body(excinfo)

    async def test_a_read_without_an_answer_is_not_called_a_write(
        self,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        shopping: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        answers_lost(core_client, monkeypatch)
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_get_todo(LIST)
        assert "applied" not in error_of(excinfo)["message"]

    async def test_two_new_rows_leave_the_created_item_open(
        self, tools: TodoTools, plain_list: PlainList
    ) -> None:
        plain_list.created_as = lambda item: [item, dataclasses.replace(item, uid="x")]
        got = await tools.ha_set_todo_item(LIST, summary="Eggs")
        assert got["todo_item"] is None
        assert "tell the new item apart" in got["warnings"][0]

    async def test_another_rows_arrival_is_not_confirmed_as_the_add(
        self, tools: TodoTools, plain_list: PlainList
    ) -> None:
        # The backend skipped the add; another client added an item meanwhile.
        plain_list.created_as = lambda item: [
            TodoItem(uid="o", summary="Other", status=TodoItemStatus.NEEDS_ACTION)
        ]
        got = await tools.ha_set_todo_item(LIST, summary="Eggs")
        assert (got["success"], got["todo_item"]) == (True, None)
        assert "not confirmed" in got["message"]


class TestTargets:
    """Core's update_item resolves an item value to the first row whose uid
    or summary equals it; the tool writes to the row it resolved."""

    async def test_an_update_after_a_move_targets_the_moved_item(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        tasks: MockConfigEntry,
    ) -> None:
        first = await add_item(hass, "Same")
        second = await add_item(hass, "Same")
        with received(hass, SERVICE) as seen:
            await tools.ha_set_todo_item(
                LIST, item="Same", rename="Moved", move_after=second
            )
        assert services(seen) == [("update_item", {"item": first, "rename": "Moved"})]
        rows = await listed(core_client)
        assert [(row["uid"], row["summary"]) for row in rows] == [
            (second, "Same"),
            (first, "Moved"),
        ]

    async def test_an_update_of_an_item_missing_from_the_read_goes_by_name(
        self, hass: HomeAssistant, tools: TodoTools, shopping: dict[str, str]
    ) -> None:
        with received(hass, SERVICE) as seen, pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Nope", status="completed")
        assert services(seen) == [
            ("update_item", {"item": "Nope", "status": "completed"})
        ]
        assert "Unable to find to-do list item: Nope" in error_of(excinfo)["message"]

    async def test_an_item_named_like_the_uid_blocks_the_update(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        shopping: dict[str, str],
    ) -> None:
        await move_to_top(hass, await add_item(hass, shopping["Pay"]))
        with received(hass, SERVICE) as seen, pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Pay", status="needs_action")
        error = error_of(excinfo)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "nothing was changed" in error["message"]
        assert seen[SERVICE] == []
        rows = await listed(core_client)
        assert row_named(rows, "Pay")["status"] == "completed"

    async def test_a_move_below_an_item_named_like_the_uid_is_refused(
        self,
        hass: HomeAssistant,
        tools: TodoTools,
        shopping: dict[str, str],
    ) -> None:
        # Moved below it, Pay would come after the item named after its uid.
        namesake = await add_item(hass, shopping["Pay"])
        with received(hass, SERVICE, MOVE) as seen, pytest.raises(ToolError):
            await tools.ha_set_todo_item(
                LIST, item="Pay", move_after=namesake, status="needs_action"
            )
        assert seen == {SERVICE: [], MOVE: []}

    async def test_a_move_alone_ignores_an_item_named_like_the_uid(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        shopping: dict[str, str],
    ) -> None:
        # todo/item/move takes the uid itself; only update_item resolves names.
        namesake = await add_item(hass, shopping["Pay"])
        got = await tools.ha_set_todo_item(LIST, item="Pay", move_after=namesake)
        rows = await listed(core_client)
        assert [row["uid"] for row in rows][-2:] == [namesake, shopping["Pay"]]
        assert got["todo_item"]["uid"] == shopping["Pay"]

    async def test_an_update_of_an_item_above_its_uid_namesake_goes_through(
        self, hass: HomeAssistant, tools: TodoTools, shopping: dict[str, str]
    ) -> None:
        await add_item(hass, shopping["Pay"])
        with received(hass, SERVICE) as seen:
            await tools.ha_set_todo_item(LIST, item="Pay", status="needs_action")
        assert services(seen) == [
            ("update_item", {"item": shopping["Pay"], "status": "needs_action"})
        ]

    async def test_a_move_to_the_top_with_an_update_is_not_refused(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        shopping: dict[str, str],
    ) -> None:
        # At the top, Core resolves the uid to the item itself.
        await move_to_top(hass, await add_item(hass, shopping["Pay"]))
        await tools.ha_set_todo_item(LIST, item="Pay", rename="Paid", move_after="")
        top = (await listed(core_client))[0]
        assert (top["uid"], top["summary"]) == (shopping["Pay"], "Paid")

    async def test_an_update_not_sent_after_a_move_says_the_move_happened(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: TodoTools,
        shopping: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        with (
            on_receipt(hass, MOVE, lambda msg: sends_lost(core_client, monkeypatch)),
            pytest.raises(ToolError) as excinfo,
        ):
            await tools.ha_set_todo_item(LIST, item="Pay", rename="Paid", move_after="")
        payload = error_body(excinfo)
        assert payload["error"]["code"] == "CONNECTION_FAILED"
        assert "the move was applied" in payload["error"]["message"]
        # Never sent, so not an unknown outcome.
        assert "write_outcome_unknown" not in payload
