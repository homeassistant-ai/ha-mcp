"""Todo tools: requests reach Core's todo services unchanged and Core's answer
comes back, through a fake WebSocket that holds one list."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_todo import TodoTools

LIST = "todo.tasks"


class _FakeClient:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = [
            {"uid": "1", "summary": "Buy milk", "status": "needs_action"},
            {"uid": "2", "summary": "Pay", "status": "completed"},
            {"uid": "3", "summary": "Bread", "status": "completed"},
        ]
        self.calls: list[dict[str, Any]] = []
        self.refuse: dict[str, tuple[str, str]] = {}
        self.state = "1"
        self.attributes: dict[str, Any] = {}
        self.writes = 0
        # Reads fail once a write has gone through (read-back after a write).
        self.read_back_fails = False
        self.drop: str | None = None
        self.noop = False
        self.add_twice = False
        self.raise_on: tuple[str, Exception] | None = None

    async def get_entity_state(self, entity_id: str) -> dict[str, Any]:
        if entity_id != LIST:
            raise HomeAssistantAPIError("Not found", status_code=404)
        return {
            "entity_id": entity_id,
            "state": self.state,
            "attributes": self.attributes,
        }

    async def send_websocket_message(self, message: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(message)
        kind = message["type"]
        if self.drop is not None and self.drop in (kind, message.get("service")):
            raise HomeAssistantConnectionError("connection lost")
        if self.raise_on and self.raise_on[0] == message.get("service"):
            raise self.raise_on[1]
        if kind == "todo/item/list":
            if self.read_back_fails and self.writes:
                raise HomeAssistantConnectionError("connection lost")
            return self._list(message)
        if kind == "todo/item/move":
            return self._move(message)
        assert message["target"] == {"entity_id": LIST}
        if message["service"] != "get_items":
            self.writes += 1
        if self.noop:
            return {"success": True, "result": {"context": {}}}
        return self._service(message["service"], message["service_data"])

    def _list(self, message: dict[str, Any]) -> dict[str, Any]:
        if message["entity_id"] != LIST:
            return {
                "success": False,
                "error": "Entity not found",
                "error_code": "not_found",
            }
        return {"success": True, "result": {"items": [dict(i) for i in self.items]}}

    def _move(self, message: dict[str, Any]) -> dict[str, Any]:
        if "move" in self.refuse:
            code, error = self.refuse["move"]
            return {"success": False, "error": error, "error_code": code}
        uids = [i["uid"] for i in self.items]
        row = self.items.pop(uids.index(message["uid"]))
        at = 0
        if "previous_uid" in message:
            at = [i["uid"] for i in self.items].index(message["previous_uid"]) + 1
        self.items.insert(at, row)
        return {"success": True, "result": None}

    def _service(self, service: str, data: dict[str, Any]) -> dict[str, Any]:
        if service in self.refuse:
            code, error = self.refuse[service]
            return {
                "success": False,
                "error": f"Command failed: {error}",
                "error_code": code,
            }
        if service == "get_items":
            statuses = data.get("status")
            statuses = [statuses] if isinstance(statuses, str) else statuses
            items = [i for i in self.items if not statuses or i["status"] in statuses]
            return {"success": True, "result": {"response": {LIST: {"items": items}}}}
        if service == "add_item":
            # Core's add_item schema strips the summary.
            self.items.append(
                {"uid": "9", "summary": data["item"].strip(), "status": "needs_action"}
            )
            if self.add_twice:
                self.items.append(
                    {"uid": "10", "summary": data["item"], "status": "needs_action"}
                )
        elif service == "update_item":
            row = next(
                i for i in self.items if data["item"] in (i["uid"], i["summary"])
            )
            if data.get("rename"):
                row["summary"] = data["rename"]
        elif service == "remove_item":
            refs = [data["item"]] if isinstance(data["item"], str) else data["item"]
            self.items = [
                i for i in self.items if not {i["uid"], i["summary"]} & set(refs)
            ]
        elif service == "remove_completed_items":
            self.items = [i for i in self.items if i["status"] != "completed"]
        return {"success": True, "result": {"context": {}}}

    def services(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (c["service"], c["service_data"])
            for c in self.calls
            if c["type"] == "call_service"
        ]


@pytest.fixture(autouse=True)
def _no_component() -> Iterator[None]:
    """Without the component every write takes Core's WebSocket commands."""
    with patch(
        "ha_mcp.tools.component_todo.get_component_caps", AsyncMock(return_value=None)
    ):
        yield


@pytest.fixture
def client() -> _FakeClient:
    return _FakeClient()


@pytest.fixture
def tools(client: _FakeClient) -> TodoTools:
    return TodoTools(client)


def _error(excinfo: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(excinfo.value))


class TestGetTodo:
    async def test_several_statuses_go_to_core_as_given(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        got = await tools.ha_get_todo(LIST, status=["needs_action", "completed"])
        assert client.services() == [
            ("get_items", {"status": ["needs_action", "completed"]})
        ]
        assert got["count"] == 3

    async def test_one_status_is_filtered_by_core(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        got = await tools.ha_get_todo(LIST, status="completed")
        assert [i["summary"] for i in got["items"]] == ["Pay", "Bread"]

    async def test_core_rejection_of_a_status_comes_through(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.refuse["get_items"] = ("invalid_format", "value must be one of ...")
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_get_todo(LIST, status="done")
        error = _error(excinfo)["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "value must be one of" in error["message"]

    async def test_unknown_list_is_not_found_before_core_is_asked(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_get_todo("todo.gone")
        assert _error(excinfo)["error"]["code"] == "ENTITY_NOT_FOUND"
        assert client.calls == []

    async def test_unavailable_list_returns_the_items_core_last_held(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        # todo.get_items matches no unavailable entity; todo/item/list answers.
        # A loaded entity keeps its attributes; only Core's placeholder has
        # `restored`.
        client.state = "unavailable"
        client.attributes = {"friendly_name": "Tasks", "supported_features": 15}
        got = await tools.ha_get_todo(LIST)
        assert got["count"] == 3
        assert "unavailable" in got["warnings"][0]
        assert client.services() == []

    async def test_unavailable_list_refuses_a_status_filter(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        # Core filters in todo.get_items, which matches no unavailable list.
        client.state = "unavailable"
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_get_todo(LIST, status="completed")
        error = _error(excinfo)["error"]
        assert error["code"] == "ENTITY_UNAVAILABLE"
        assert "status" in error["message"]
        assert client.calls == []

    async def test_placeholder_list_is_refused_before_core_is_asked(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        # Core's placeholder state for a registered list with no loaded entity;
        # todo/item/list would answer "Entity not found".
        client.state = "unavailable"
        client.attributes = {"restored": True}
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_get_todo(LIST)
        error = _error(excinfo)["error"]
        assert error["code"] == "ENTITY_UNAVAILABLE"
        assert "placeholder" in error["message"]
        assert client.calls == []


class TestSetTodoItem:
    async def test_create_returns_the_new_row(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        got = await tools.ha_set_todo_item(LIST, summary="Eggs", description="")
        assert client.services() == [("add_item", {"item": "Eggs"})]
        assert got["todo_item"] == {
            "uid": "9",
            "summary": "Eggs",
            "status": "needs_action",
        }

    async def test_status_on_create_is_left_to_core(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.refuse["add_item"] = (
            "invalid_format",
            "extra keys not allowed @ data['status']",
        )
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, summary="Eggs", status="completed")
        assert client.services() == [
            ("add_item", {"item": "Eggs", "status": "completed"})
        ]
        assert "extra keys not allowed" in _error(excinfo)["error"]["message"]

    async def test_empty_string_clears_a_field(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        await tools.ha_set_todo_item(LIST, item="Pay", description="", due_date=" ")
        assert client.services() == [
            ("update_item", {"item": "2", "description": None, "due_date": None})
        ]

    async def test_update_sends_a_value_as_given(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        # Core stores a description as given, on create and on update alike.
        await tools.ha_set_todo_item(LIST, item="Pay", description="  electric  ")
        assert client.services() == [
            ("update_item", {"item": "2", "description": "  electric  "})
        ]

    async def test_update_without_changes_is_judged_by_core(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.refuse["update_item"] = (
            "invalid_format",
            "must contain at least one of rename, status",
        )
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Pay")
        assert client.services() == [("update_item", {"item": "2"})]
        assert "at least one of" in _error(excinfo)["error"]["message"]

    async def test_field_the_list_lacks_reports_core_text(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.refuse["update_item"] = (
            "service_validation_error",
            "This entity does not support setting field: description",
        )
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Pay", description="x")
        error = _error(excinfo)["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "does not support setting field" in error["message"]

    async def test_renamed_item_is_read_back_by_uid(self, tools: TodoTools) -> None:
        got = await tools.ha_set_todo_item(LIST, item="Pay", rename="Paid")
        assert got["todo_item"] == {
            "uid": "2",
            "summary": "Paid",
            "status": "completed",
        }

    async def test_move_alone_sends_no_update(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        got = await tools.ha_set_todo_item(LIST, item="Bread", move_after="Buy milk")
        assert client.services() == []
        assert [c for c in client.calls if c["type"] == "todo/item/move"] == [
            {
                "type": "todo/item/move",
                "entity_id": LIST,
                "uid": "3",
                "previous_uid": "1",
            }
        ]
        assert [i["summary"] for i in client.items] == ["Buy milk", "Bread", "Pay"]
        assert got["todo_item"]["uid"] == "3"

    async def test_empty_move_after_moves_to_top(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        await tools.ha_set_todo_item(LIST, item="Pay", move_after="")
        assert client.items[0]["summary"] == "Pay"

    async def test_list_without_reordering_reports_core_refusal(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.refuse["move"] = (
            "not_supported",
            "To-do list does not support To-do item reordering",
        )
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Pay", move_after="")
        error = _error(excinfo)["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "does not support To-do item reordering" in error["message"]

    async def test_move_to_an_unknown_item_is_refused(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Pay", move_after="Nope")
        assert _error(excinfo)["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert not [c for c in client.calls if c["type"] == "todo/item/move"]


class TestRemoveTodoItem:
    async def test_list_of_items_in_one_call(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        got = await tools.ha_remove_todo_item(LIST, item=["Pay", "1"])
        assert client.services() == [("remove_item", {"item": ["Pay", "1"]})]
        assert [i["summary"] for i in got["removed"]] == ["Buy milk", "Pay"]

    async def test_remove_completed(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        got = await tools.ha_remove_todo_item(LIST, remove_completed=True)
        assert client.services() == [("remove_completed_items", {})]
        assert [i["summary"] for i in got["removed"]] == ["Pay", "Bread"]

    @pytest.mark.parametrize("kwargs", [{}, {"item": "Pay", "remove_completed": True}])
    async def test_needs_exactly_one_target(
        self, tools: TodoTools, client: _FakeClient, kwargs: dict[str, Any]
    ) -> None:
        with pytest.raises(ToolError):
            await tools.ha_remove_todo_item(LIST, **kwargs)
        assert client.calls == []

    async def test_blank_entry_in_a_list_is_refused(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        with pytest.raises(ToolError):
            await tools.ha_remove_todo_item(LIST, item=["Pay", " "])
        assert client.calls == []

    async def test_unknown_item_is_refused_before_anything_is_removed(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_remove_todo_item(LIST, item=["Pay", "Nope"])
        error = _error(excinfo)["error"]
        assert error["code"] == "RESOURCE_NOT_FOUND"
        assert "'Nope'" in error["message"]
        assert client.services() == []

    async def test_core_refusal_comes_through(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.refuse["remove_item"] = (
            "service_validation_error",
            "Entity todo.tasks does not support removing items",
        )
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_remove_todo_item(LIST, item="Pay")
        error = _error(excinfo)["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "does not support removing items" in error["message"]


class TestOutcomes:
    """Each write says what it did, also when Core skipped it or the read
    after it failed."""

    async def test_unavailable_list_refuses_a_write(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        # Core skips an unavailable list and still answers success.
        client.state = "unavailable"
        with pytest.raises(ToolError) as set_refusal:
            await tools.ha_set_todo_item(LIST, summary="Eggs")
        with pytest.raises(ToolError) as remove_refusal:
            await tools.ha_remove_todo_item(LIST, item="Pay")
        assert client.calls == []
        for refusal in (set_refusal, remove_refusal):
            assert _error(refusal)["error"]["code"] == "ENTITY_UNAVAILABLE"

    async def test_failed_read_back_keeps_the_applied_add(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.read_back_fails = True
        got = await tools.ha_set_todo_item(LIST, summary="Eggs")
        assert got["success"] is True
        assert got["todo_item"] is None
        assert "could not be read back" in got["warnings"][0]

    async def test_failed_read_back_keeps_the_applied_removal(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.read_back_fails = True
        got = await tools.ha_remove_todo_item(LIST, item="Pay")
        assert got["removed"] is None
        assert len(got["warnings"]) == 1
        assert "could not be read back" in got["warnings"][0]

    async def test_unchanged_list_after_a_removal_is_flagged(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.noop = True
        got = await tools.ha_remove_todo_item(LIST, item="Pay")
        assert got["removed"] == []
        assert "still holds" in got["warnings"][0]

    async def test_item_missing_after_an_update_is_flagged(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.refuse = {}
        client.items[1]["uid"] = "2"
        client.noop = True
        client.read_back_fails = False
        original_list = client._list

        def list_without_pay(message: dict[str, Any]) -> dict[str, Any]:
            reply = original_list(message)
            if client.writes:
                reply["result"]["items"] = [
                    i for i in reply["result"]["items"] if i["uid"] != "2"
                ]
            return reply

        client._list = list_without_pay  # type: ignore[method-assign]
        got = await tools.ha_set_todo_item(LIST, item="Pay", status="needs_action")
        assert got["todo_item"] is None
        assert "not on the list" in got["warnings"][0]

    async def test_refused_move_writes_no_update(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.refuse["move"] = (
            "not_supported",
            "To-do list does not support To-do item reordering",
        )
        with pytest.raises(ToolError):
            await tools.ha_set_todo_item(LIST, item="Pay", rename="Paid", move_after="")
        assert client.services() == []

    async def test_unknown_move_target_writes_nothing(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        with pytest.raises(ToolError):
            await tools.ha_set_todo_item(
                LIST, item="Pay", status="needs_action", move_after="Nope"
            )
        assert client.services() == []

    async def test_update_refused_after_a_move_says_the_move_happened(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.refuse["update_item"] = ("service_validation_error", "nope")
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Pay", rename="Paid", move_after="")
        assert "the move was applied" in _error(excinfo)["error"]["message"]

    async def test_move_without_answer_is_an_unknown_outcome(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.drop = "todo/item/move"
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Pay", move_after="")
        assert _error(excinfo)["write_outcome_unknown"] is True

    async def test_list_reply_without_items_is_an_error(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        # Read as an empty list, it would report every item as removed.
        client._list = lambda _message: {"success": True, "result": {}}  # type: ignore[method-assign]
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_remove_todo_item(LIST, item="Pay")
        assert "without an item list" in _error(excinfo)["error"]["message"]
        assert client.services() == []

    async def test_string_naming_an_item_is_that_item(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.items += [
            {"uid": "4", "summary": "Milk", "status": "needs_action"},
            {"uid": "5", "summary": '["Milk"]', "status": "needs_action"},
        ]
        first = await tools.ha_remove_todo_item(LIST, item='["Milk"]')
        assert [i["uid"] for i in first["removed"]] == ["5"]
        # Once no item has that name, the string is the list it holds.
        second = await tools.ha_remove_todo_item(LIST, item='["Milk"]')
        assert [i["uid"] for i in second["removed"]] == ["4"]
        assert client.services() == [
            ("remove_item", {"item": ['["Milk"]']}),
            ("remove_item", {"item": ["Milk"]}),
        ]

    async def test_stringified_list_names_its_items(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        got = await tools.ha_remove_todo_item(LIST, item='["Pay", "Bread"]')
        assert [i["uid"] for i in got["removed"]] == ["2", "3"]

    async def test_item_named_like_an_empty_list_is_removed(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.items.append({"uid": "6", "summary": "[]", "status": "needs_action"})
        got = await tools.ha_remove_todo_item(LIST, item="[]")
        assert [i["uid"] for i in got["removed"]] == ["6"]
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_remove_todo_item(LIST, item="[]")
        assert "as a list it names no item" in _error(excinfo)["error"]["message"]

    async def test_unknown_name_that_is_no_list_is_not_found(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_remove_todo_item(LIST, item="[Work] Call Bob")
        assert _error(excinfo)["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert client.services() == []


class TestReviewGaps:
    async def test_get_asks_core_for_a_response_without_filter(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        await tools.ha_get_todo(LIST)
        (call,) = [c for c in client.calls if c["type"] == "call_service"]
        assert call["return_response"] is True
        assert call["service_data"] == {}

    async def test_rename_and_move_together_send_both(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        got = await tools.ha_set_todo_item(
            LIST, item="Bread", rename="Rye", move_after=""
        )
        assert client.services() == [("update_item", {"item": "3", "rename": "Rye"})]
        assert client.items[0]["summary"] == "Rye"
        assert got["todo_item"]["summary"] == "Rye"

    async def test_failed_list_read_writes_nothing(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client._list = lambda _m: {  # type: ignore[method-assign]
            "success": False,
            "error": "Unauthorized",
            "error_code": "unauthorized",
        }
        with pytest.raises(ToolError):
            await tools.ha_set_todo_item(LIST, summary="Eggs")
        with pytest.raises(ToolError):
            await tools.ha_remove_todo_item(LIST, item="Pay")
        assert client.services() == []

    async def test_write_without_answer_is_an_unknown_outcome(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.raise_on = ("add_item", HomeAssistantConnectionError("lost"))
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, summary="Eggs")
        assert _error(excinfo)["write_outcome_unknown"] is True

    async def test_write_never_sent_is_not_an_unknown_outcome(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.raise_on = ("add_item", HomeAssistantCommandNotSent("not sent"))
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, summary="Eggs")
        assert "write_outcome_unknown" not in _error(excinfo)

    async def test_dropped_read_is_not_called_a_write(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.raise_on = ("get_items", HomeAssistantConnectionError("lost"))
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_get_todo(LIST)
        assert "applied" not in _error(excinfo)["error"]["message"]

    async def test_two_new_rows_leave_the_created_item_open(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.add_twice = True
        got = await tools.ha_set_todo_item(LIST, summary="Eggs")
        assert got["todo_item"] is None
        assert "tell the new item apart" in got["warnings"][0]

    async def test_create_refuses_move_after(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, summary="Eggs", move_after="")
        assert _error(excinfo)["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert client.services() == []

    async def test_create_needs_a_summary(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST)
        assert _error(excinfo)["error"]["code"] == "VALIDATION_MISSING_PARAMETER"
        assert client.services() == []

    @pytest.mark.parametrize("field", ["description", "due_date", "due_datetime"])
    async def test_blank_field_on_create_is_left_out(
        self, tools: TodoTools, client: _FakeClient, field: str
    ) -> None:
        await tools.ha_set_todo_item(LIST, summary="Eggs", **{field: ""})
        assert client.services() == [("add_item", {"item": "Eggs"})]

    async def test_move_of_an_unknown_item_is_refused(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Nope", move_after="")
        assert _error(excinfo)["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert not [c for c in client.calls if c["type"] == "todo/item/move"]


class TestTargetsAndReplies:
    async def test_update_after_a_move_targets_the_moved_item(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        # Two items share a name; Core would resolve the name to the first.
        client.items = [
            {"uid": "a", "summary": "Same", "status": "needs_action"},
            {"uid": "b", "summary": "Same", "status": "needs_action"},
        ]
        await tools.ha_set_todo_item(LIST, item="Same", rename="Moved", move_after="b")
        assert client.services() == [("update_item", {"item": "a", "rename": "Moved"})]
        assert [(i["uid"], i["summary"]) for i in client.items] == [
            ("b", "Same"),
            ("a", "Moved"),
        ]

    async def test_unconfirmed_add_is_not_called_added(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.noop = True
        got = await tools.ha_set_todo_item(LIST, summary="Eggs")
        assert got["success"] is True
        assert got["todo_item"] is None
        assert "not confirmed" in got["message"]

    @pytest.mark.parametrize("remove_completed", [False, True])
    async def test_empty_item_list_is_refused(
        self, tools: TodoTools, client: _FakeClient, remove_completed: bool
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_remove_todo_item(
                LIST, item=[], remove_completed=remove_completed
            )
        assert "empty list" in _error(excinfo)["error"]["message"]
        assert client.calls == []

    async def test_removed_lists_only_what_was_asked_for(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        original = client._service

        def remove_and_more(service: str, data: dict[str, Any]) -> dict[str, Any]:
            reply = original(service, data)
            if service == "remove_item":
                # Another client removes "Bread" in the meantime.
                client.items = [i for i in client.items if i["uid"] != "3"]
            return reply

        client._service = remove_and_more  # type: ignore[method-assign]
        got = await tools.ha_remove_todo_item(LIST, item="Pay")
        assert [i["uid"] for i in got["removed"]] == ["2"]

    async def test_removed_completed_leaves_out_another_clients_removal(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        original = client._service

        def remove_and_more(service: str, data: dict[str, Any]) -> dict[str, Any]:
            reply = original(service, data)
            if service == "remove_completed_items":
                # Another client removes the open "Buy milk" in the meantime.
                client.items = [i for i in client.items if i["uid"] != "1"]
            return reply

        client._service = remove_and_more  # type: ignore[method-assign]
        got = await tools.ha_remove_todo_item(LIST, remove_completed=True)
        assert sorted(i["uid"] for i in got["removed"]) == ["2", "3"]

    async def test_update_of_an_item_missing_from_the_first_read_goes_by_name(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.refuse["update_item"] = (
            "service_validation_error",
            "Unable to find to-do list item: Nope",
        )
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Nope", status="completed")
        assert client.services()[0][1]["item"] == "Nope"
        assert "Unable to find" in _error(excinfo)["error"]["message"]

    async def test_item_named_like_the_uid_blocks_the_update(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        # Core would resolve the uid "2" to the earlier item named "2".
        client.items.insert(0, {"uid": "x", "summary": "2", "status": "needs_action"})
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Pay", status="needs_action")
        error = _error(excinfo)["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "nothing was changed" in error["message"]
        assert client.services() == []

    async def test_move_below_an_item_named_like_the_uid_is_refused(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        # "Pay" (uid "2") sits above an item named "2"; moved below it, Core
        # would resolve "2" to that item.
        client.items.append({"uid": "x", "summary": "2", "status": "needs_action"})
        with pytest.raises(ToolError):
            await tools.ha_set_todo_item(
                LIST, item="Pay", move_after="x", status="needs_action"
            )
        assert client.services() == []
        assert not [c for c in client.calls if c["type"] == "todo/item/move"]

    async def test_move_alone_ignores_an_item_named_like_the_uid(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        # todo/item/move takes the uid itself; only update_item resolves names.
        client.items.append({"uid": "x", "summary": "2", "status": "needs_action"})
        got = await tools.ha_set_todo_item(LIST, item="Pay", move_after="x")
        assert [i["uid"] for i in client.items][-2:] == ["x", "2"]
        # The read-back reports the moved item, not the one named after its uid.
        assert got["todo_item"]["uid"] == "2"

    async def test_update_of_an_item_above_its_uid_namesake_goes_through(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        # Without a move, Core resolves "2" to "Pay", which comes first.
        client.items.append({"uid": "x", "summary": "2", "status": "needs_action"})
        await tools.ha_set_todo_item(LIST, item="Pay", status="needs_action")
        assert client.services() == [
            ("update_item", {"item": "2", "status": "needs_action"})
        ]

    async def test_update_not_sent_after_a_move_says_the_move_happened(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        client.raise_on = ("update_item", HomeAssistantCommandNotSent("not sent"))
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Pay", rename="Paid", move_after="")
        payload = _error(excinfo)
        assert payload["error"]["code"] == "CONNECTION_FAILED"
        assert "the move was applied" in payload["error"]["message"]
        # Never sent, so not an unknown outcome.
        assert "write_outcome_unknown" not in payload

    async def test_another_clients_new_item_is_not_returned_as_ours(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        original = client._service

        def add_other(service: str, data: dict[str, Any]) -> dict[str, Any]:
            if service != "add_item":
                return original(service, data)
            # Core skipped the add; another client added an item meanwhile.
            client.items.append(
                {"uid": "o", "summary": "Other", "status": "needs_action"}
            )
            return {"success": True, "result": {"context": {}}}

        client._service = add_other  # type: ignore[method-assign]
        got = await tools.ha_set_todo_item(LIST, summary="Eggs")
        assert got["todo_item"] is None
        assert "not confirmed" in got["message"]

    async def test_padded_summary_is_confirmed_as_core_stores_it(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        got = await tools.ha_set_todo_item(LIST, summary="  Eggs ")
        assert got["todo_item"]["uid"] == "9"
        assert got["message"] == f"Added 'Eggs' to {LIST}"

    async def test_move_to_the_top_with_an_update_is_not_refused(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        # At the top, Core resolves the uid to the item itself.
        client.items.insert(0, {"uid": "x", "summary": "2", "status": "needs_action"})
        await tools.ha_set_todo_item(LIST, item="Pay", rename="Paid", move_after="")
        assert client.services() == [("update_item", {"item": "2", "rename": "Paid"})]
        assert client.items[0]["uid"] == "2"
