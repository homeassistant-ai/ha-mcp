"""ha_mcp_tools/todo_item, and the server client that sends it.

The component runs on a real ``hass`` with Core's todo integration. The list
is a ``TodoListEntity`` of a test platform that keeps its rows in memory, so
Core's services, schema, feature checks and item lookup act on it; the
service calls Core receives are read from its ``call_service`` events.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.components.todo import DOMAIN as TODO_DOMAIN
from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.components.todo import const as todo_const
from homeassistant.components.todo import entity as todo_entity
from homeassistant.components.websocket_api.const import (
    ERR_HOME_ASSISTANT_ERROR,
    ERR_UNAUTHORIZED,
    ERR_UNKNOWN_ERROR,
)
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockUser,
    async_capture_events,
    setup_test_component_platform,
)
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools import todo_items
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ha_mcp.client.rest_client import HomeAssistantError as ClientError
from ha_mcp.tools import component_todo
from ha_mcp.tools.component_api import ComponentCaps

from ._component_core import ws_call, ws_result

LIST = "todo.tasks"

_NO_MOVE = (
    TodoListEntityFeature.CREATE_TODO_ITEM
    | TodoListEntityFeature.DELETE_TODO_ITEM
    | TodoListEntityFeature.UPDATE_TODO_ITEM
    | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
    | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
)


def _item(uid: str | None, summary: str, **fields: Any) -> TodoItem:
    return TodoItem(
        uid=uid, summary=summary, status=TodoItemStatus.NEEDS_ACTION, **fields
    )


class _TaskList(TodoListEntity):
    """A todo integration's list, its rows kept in memory as ``rows``."""

    _attr_should_poll = False

    def __init__(self, rows: list[TodoItem]) -> None:
        self.entity_id = LIST
        self.rows = rows
        self._attr_todo_items = rows
        self._attr_supported_features = _NO_MOVE | TodoListEntityFeature.MOVE_TODO_ITEM
        # What the backend raises on a write, and whether it changed a row first.
        self.failure: Exception | None = None
        self.changed_before_failing = False
        self.move_failure: Exception | None = None
        # (uid, previous_uid, summaries as they were when the move came)
        self.moves: list[tuple[str, str | None, list[str | None]]] = []

    def set_available(self, available: bool) -> None:
        self._attr_available = available
        self.async_write_ha_state()

    def set_features(self, features: int | None) -> None:
        self._attr_supported_features = features
        self.async_write_ha_state()

    def _backend(self) -> None:
        if self.changed_before_failing:
            self.rows.append(_item("listener", "added by a listener"))
        if self.failure is not None:
            raise self.failure

    async def async_create_todo_item(self, item: TodoItem) -> None:
        self._backend()
        self.rows.append(dataclasses.replace(item, uid=f"n{len(self.rows)}"))
        self.async_write_ha_state()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        self._backend()
        at = next(i for i, row in enumerate(self.rows) if row.uid == item.uid)
        self.rows[at] = item
        self.async_write_ha_state()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        self._backend()
        self.rows[:] = [row for row in self.rows if row.uid not in uids]
        self.async_write_ha_state()

    async def async_move_todo_item(
        self, uid: str, previous_uid: str | None = None
    ) -> None:
        self.moves.append((uid, previous_uid, [row.summary for row in self.rows]))
        if self.move_failure is not None:
            raise self.move_failure
        row = next(r for r in self.rows if r.uid == uid)
        self.rows.remove(row)
        at = 0
        if previous_uid is not None:
            at = [r.uid for r in self.rows].index(previous_uid) + 1
        self.rows.insert(at, row)
        self.async_write_ha_state()


@pytest.fixture
async def tasks(hass: HomeAssistant) -> _TaskList:
    """Core's todo integration with ``LIST`` loaded from a test platform."""
    entity = _TaskList(
        [
            _item("1", "Milk"),
            _item("2", "Pay", due=datetime(2026, 11, 1, 10, 0, tzinfo=UTC)),
            _item("3", "Milk"),
        ]
    )
    setup_test_component_platform(hass, TODO_DOMAIN, [entity])
    assert await async_setup_component(
        hass, TODO_DOMAIN, {TODO_DOMAIN: {"platform": "test"}}
    )
    await hass.async_block_till_done()
    return entity


@pytest.fixture
async def calls(hass: HomeAssistant) -> list[Event[dict[str, Any]]]:
    """Core's ``call_service`` events: fired once a call passed the schema."""
    return async_capture_events(hass, EVENT_CALL_SERVICE)


def _sent(calls: list[Event[dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    """The todo services Core was called with, and the data each got."""
    return [
        (call.data["service"], call.data["service_data"])
        for call in calls
        if call.data["domain"] == TODO_DOMAIN
    ]


async def _reply(
    ws: MockHAClientWebSocket, action: str, entity_id: str = LIST, **fields: Any
) -> dict[str, Any]:
    """Core's WebSocket reply to one ``ha_mcp_tools/todo_item`` action."""
    return await ws_call(
        ws, todo_items.WS_TODO_ITEM, entity_id=entity_id, action=action, **fields
    )


async def _run(ws: MockHAClientWebSocket, action: str, **fields: Any) -> dict[str, Any]:
    """The component's answer to one action that Core did not fail."""
    reply = await _reply(ws, action, **fields)
    assert reply["success"], reply
    result: dict[str, Any] = reply["result"]
    return result


@pytest.mark.usefixtures("tasks")
class TestComponent:
    async def test_resolve_takes_cores_first_match(
        self,
        component_ws: MockHAClientWebSocket,
        calls: list[Event[dict[str, Any]]],
    ) -> None:
        got = await _run(component_ws, "resolve", refs=["Milk", "2", "Nope"])
        assert [row and row["uid"] for row in got["items"]] == ["1", "2", None]
        assert _sent(calls) == []

    async def test_add_returns_the_row_core_stored(
        self,
        component_ws: MockHAClientWebSocket,
        calls: list[Event[dict[str, Any]]],
        hass_admin_user: MockUser,
    ) -> None:
        got = await _run(component_ws, "add", data={"item": "  Eggs "})
        stored = await ws_result(component_ws, "todo/item/list", entity_id=LIST)
        assert got["item"] == stored["items"][-1]
        assert got["item"]["summary"] == "Eggs"
        assert _sent(calls) == [("add_item", {"item": "  Eggs ", "entity_id": LIST})]
        assert [call.context.user_id for call in calls] == [hass_admin_user.id]

    async def test_update_goes_to_the_row_with_that_uid(
        self,
        component_ws: MockHAClientWebSocket,
        calls: list[Event[dict[str, Any]]],
        hass_admin_user: MockUser,
    ) -> None:
        got = await _run(component_ws, "update", uid="3", data={"status": "completed"})
        assert got["item"]["uid"] == "3"
        assert got["item"]["status"] == "completed"
        assert _sent(calls) == [
            ("update_item", {"status": "completed", "item": "3", "entity_id": LIST})
        ]
        assert [call.context.user_id for call in calls] == [hass_admin_user.id]

    async def test_uid_another_item_is_named_after_is_refused(
        self,
        component_ws: MockHAClientWebSocket,
        tasks: _TaskList,
        calls: list[Event[dict[str, Any]]],
    ) -> None:
        tasks.rows.insert(0, _item("x", "3"))
        got = await _run(component_ws, "update", uid="3", data={"status": "completed"})
        assert got["error"]["code"] == "namesake"
        assert _sent(calls) == []

    async def test_unknown_uid_is_not_found(
        self, component_ws: MockHAClientWebSocket
    ) -> None:
        got = await _run(
            component_ws, "update", uid="Milk", data={"status": "completed"}
        )
        assert got["error"]["code"] == "item_not_found"

    async def test_remove_reports_the_rows_it_removed(
        self, component_ws: MockHAClientWebSocket
    ) -> None:
        got = await _run(component_ws, "remove", refs=["Milk"])
        assert [row["uid"] for row in got["removed"]] == ["1"]

    async def test_unavailable_list_is_refused_before_the_call(
        self,
        component_ws: MockHAClientWebSocket,
        tasks: _TaskList,
        calls: list[Event[dict[str, Any]]],
    ) -> None:
        # Core's services skip an unavailable list and still answer success.
        tasks.set_available(False)
        got = await _run(component_ws, "remove", refs=["Milk"])
        assert got["error"]["code"] == "list_unavailable"
        assert _sent(calls) == []
        assert [row.uid for row in tasks.rows] == ["1", "2", "3"]

    async def test_list_not_loaded(self, component_ws: MockHAClientWebSocket) -> None:
        got = await _run(component_ws, "resolve", entity_id="todo.gone", refs=["Milk"])
        assert got["error"]["code"] == "not_loaded"

    @pytest.mark.parametrize(
        ("action", "fields"),
        [
            pytest.param(
                "update",
                {"uid": "2", "data": {"status": "bad status"}},
                id="schema-invalid",
            ),
            pytest.param(
                "remove", {"refs": ["Milk", "Nope"]}, id="service-validation-error"
            ),
        ],
    )
    async def test_core_refusal_that_changed_nothing(
        self,
        component_ws: MockHAClientWebSocket,
        tasks: _TaskList,
        action: str,
        fields: dict[str, Any],
    ) -> None:
        before = list(tasks.rows)
        got = await _run(component_ws, action, **fields)
        assert got["error"]["code"] == "invalid"
        assert got["error"]["message"]
        assert tasks.rows == before

    @pytest.mark.parametrize(
        ("error", "code"),
        [
            (HomeAssistantError("timeout"), ERR_HOME_ASSISTANT_ERROR),
            (ValueError("bad json"), ERR_UNKNOWN_ERROR),
        ],
    )
    async def test_integration_error_is_raised_though_the_rows_are_unchanged(
        self,
        component_ws: MockHAClientWebSocket,
        tasks: _TaskList,
        error: Exception,
        code: str,
    ) -> None:
        # The integration may have reached its backend before it failed.
        tasks.failure = error
        reply = await _reply(
            component_ws, "update", uid="2", data={"status": "completed"}
        )
        assert reply["success"] is False
        assert reply["error"]["code"] == code

    async def test_a_row_without_uid_does_not_break_the_reply(
        self, component_ws: MockHAClientWebSocket, tasks: _TaskList
    ) -> None:
        tasks.rows.append(_item(None, "No uid"))
        got = await _run(component_ws, "remove", refs=["Milk"])
        assert [row["uid"] for row in got["removed"]] == ["1"]

    async def test_add_and_update_read_back_past_a_row_without_uid(
        self, component_ws: MockHAClientWebSocket, tasks: _TaskList
    ) -> None:
        tasks.rows.insert(0, _item(None, "No uid"))
        added = await _run(component_ws, "add", data={"item": "Eggs"})
        assert added["item"]["summary"] == "Eggs"
        updated = await _run(
            component_ws, "update", uid="2", data={"status": "completed"}
        )
        assert updated["item"]["uid"] == "2"

    async def test_data_shaped_like_a_refusal_goes_to_core(
        self, component_ws: MockHAClientWebSocket, tasks: _TaskList
    ) -> None:
        # Core's schema, not the component, names the key it refuses.
        got = await _run(component_ws, "add", data={"item": "Eggs", "success": False})
        assert got["error"]["code"] == "invalid"
        assert "success" in got["error"]["message"]
        assert len(tasks.rows) == 3

    async def test_error_after_the_list_changed_is_raised(
        self, component_ws: MockHAClientWebSocket, tasks: _TaskList
    ) -> None:
        tasks.failure = ServiceValidationError("listener failed")
        tasks.changed_before_failing = True
        reply = await _reply(
            component_ws, "update", uid="2", data={"status": "completed"}
        )
        assert reply["success"] is False
        assert reply["error"]["code"] == ERR_HOME_ASSISTANT_ERROR
        assert reply["error"]["message"] == "listener failed"

    @pytest.mark.parametrize(
        ("due", "sent"),
        [
            ("2026-12-24", {"due_date": "2026-12-24"}),
            (
                "2026-12-24T10:00:00+01:00",
                {"due_datetime": "2026-12-24T10:00:00+01:00"},
            ),
            # A clear names the field of the due the item has.
            (None, {"due_datetime": None}),
        ],
    )
    async def test_due_takes_the_field_core_validates_it_as(
        self,
        component_ws: MockHAClientWebSocket,
        calls: list[Event[dict[str, Any]]],
        due: str | None,
        sent: dict[str, Any],
    ) -> None:
        got = await _run(component_ws, "update", uid="2", data={}, due=due)
        assert got["success"] is True
        assert _sent(calls) == [
            ("update_item", {"item": "2", "entity_id": LIST, **sent})
        ]

    async def test_clear_of_a_due_date_names_due_date(
        self,
        component_ws: MockHAClientWebSocket,
        tasks: _TaskList,
        calls: list[Event[dict[str, Any]]],
    ) -> None:
        tasks.rows.append(_item("4", "D", due=date(2026, 11, 1)))
        got = await _run(component_ws, "update", uid="4", data={}, due=None)
        assert got["success"] is True
        assert _sent(calls) == [
            ("update_item", {"item": "4", "due_date": None, "entity_id": LIST})
        ]

    async def test_clear_of_an_item_without_due_sends_no_field(
        self,
        component_ws: MockHAClientWebSocket,
        calls: list[Event[dict[str, Any]]],
    ) -> None:
        await _run(
            component_ws, "update", uid="1", data={"status": "completed"}, due=None
        )
        assert _sent(calls) == [
            ("update_item", {"status": "completed", "item": "1", "entity_id": LIST})
        ]

    async def test_missing_field_for_the_action_is_invalid(
        self, component_ws: MockHAClientWebSocket
    ) -> None:
        got = await _run(component_ws, "update", data={})
        assert got["error"]["code"] == "invalid"

    async def test_update_then_move_by_uid(
        self, component_ws: MockHAClientWebSocket, tasks: _TaskList
    ) -> None:
        # Below x, the row named "2" comes first; the update goes before the
        # move, on the order Core resolves "2" in, so it changes Pay.
        tasks.rows.append(_item("x", "2"))
        got = await _run(
            component_ws,
            "update",
            uid="2",
            data={"status": "completed"},
            previous_uid="x",
        )
        assert got["item"]["status"] == "completed"
        assert tasks.moves == [("2", "x", ["Milk", "Pay", "Milk", "2"])]
        assert [i.uid for i in tasks.rows] == ["1", "3", "x", "2"]
        assert tasks.rows[2].status == TodoItemStatus.NEEDS_ACTION

    async def test_move_to_the_top(
        self, component_ws: MockHAClientWebSocket, tasks: _TaskList
    ) -> None:
        # Core's update_item refuses an update that changes nothing.
        await _run(
            component_ws,
            "update",
            uid="3",
            data={"status": "completed"},
            previous_uid=None,
        )
        assert tasks.moves[0][:2] == ("3", None)
        assert [i.uid for i in tasks.rows] == ["3", "1", "2"]

    async def test_update_that_only_reorders(
        self,
        component_ws: MockHAClientWebSocket,
        tasks: _TaskList,
        calls: list[Event[dict[str, Any]]],
    ) -> None:
        """Core's update_item refuses an update with no field, so a move
        alone goes straight to the move."""
        got = await _run(component_ws, "update", uid="3", previous_uid=None)

        assert got["success"] is True
        assert got["item"]["uid"] == "3"
        assert [i.uid for i in tasks.rows] == ["3", "1", "2"]
        assert _sent(calls) == []

    async def test_list_without_reordering_refuses_before_the_update(
        self,
        component_ws: MockHAClientWebSocket,
        tasks: _TaskList,
        calls: list[Event[dict[str, Any]]],
    ) -> None:
        tasks.set_features(_NO_MOVE)
        got = await _run(
            component_ws,
            "update",
            uid="2",
            data={"status": "completed"},
            previous_uid=None,
        )
        assert got["error"]["code"] == "not_supported"
        assert _sent(calls) == []
        assert tasks.moves == []
        assert tasks.rows[1].status == TodoItemStatus.NEEDS_ACTION

    async def test_failed_move_after_the_update_says_it_was_applied(
        self, component_ws: MockHAClientWebSocket, tasks: _TaskList
    ) -> None:
        tasks.move_failure = HomeAssistantError("backend gone")
        got = await _run(
            component_ws,
            "update",
            uid="2",
            data={"status": "completed"},
            previous_uid="1",
        )
        assert got["error"] == {
            "code": "move_failed",
            "message": "The update was applied, but the move failed: backend gone",
        }
        assert tasks.rows[1].status == TodoItemStatus.COMPLETED

    async def test_unexpected_move_error_is_raised(
        self, component_ws: MockHAClientWebSocket, tasks: _TaskList
    ) -> None:
        tasks.move_failure = RuntimeError("bug")
        reply = await _reply(
            component_ws,
            "update",
            uid="2",
            data={"status": "completed"},
            previous_uid="1",
        )
        assert reply["success"] is False
        assert reply["error"]["code"] == ERR_UNKNOWN_ERROR

    async def test_list_without_reordering_still_takes_an_update(
        self, component_ws: MockHAClientWebSocket, tasks: _TaskList
    ) -> None:
        tasks.set_features(_NO_MOVE)
        got = await _run(component_ws, "update", uid="2", data={"status": "completed"})
        assert got["item"]["status"] == "completed"
        assert tasks.moves == []

    async def test_list_without_features_leaves_an_update_to_core(
        self,
        component_ws: MockHAClientWebSocket,
        tasks: _TaskList,
        calls: list[Event[dict[str, Any]]],
    ) -> None:
        # Without a move the component has nothing to check; Core refuses the
        # update itself, since the list does not support updating items.
        tasks.set_features(None)
        got = await _run(component_ws, "update", uid="2", data={"status": "completed"})
        assert got["error"]["code"] == "invalid"
        assert [service for service, _ in _sent(calls)] == ["update_item"]

    async def test_list_without_features_refuses_a_move(
        self,
        component_ws: MockHAClientWebSocket,
        tasks: _TaskList,
        calls: list[Event[dict[str, Any]]],
    ) -> None:
        tasks.set_features(None)
        got = await _run(
            component_ws,
            "update",
            uid="2",
            data={"status": "completed"},
            previous_uid=None,
        )
        assert got["error"]["code"] == "not_supported"
        assert _sent(calls) == []

    async def test_previous_uid_moves_only_on_update(
        self, component_ws: MockHAClientWebSocket, tasks: _TaskList
    ) -> None:
        await _run(component_ws, "remove", refs=["Pay"], previous_uid="1")
        assert tasks.moves == []

    async def test_core_without_its_todo_parts_is_unsupported(
        self, component_ws: MockHAClientWebSocket, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A Core without one of the todo parts the bridge reads.
        monkeypatch.delattr(todo_entity, "api_items_factory")
        got = await _run(component_ws, "resolve", refs=["Milk"])
        assert got["error"]["code"] == "unsupported"

    async def test_core_parts_refuse_only_validation_errors(
        self, component_ws: MockHAClientWebSocket, tasks: _TaskList
    ) -> None:
        """A ServiceValidationError that changed no row is a refusal, also
        when the integration raised it; its base HomeAssistantError is not
        (test_integration_error_is_raised_though_the_rows_are_unchanged)."""
        tasks.failure = ServiceValidationError("no due date")
        got = await _run(component_ws, "update", uid="2", data={"status": "completed"})
        assert got["error"] == {"code": "invalid", "message": "no due date"}

    async def test_core_without_the_move_feature_has_no_bridge(
        self, component_ws: MockHAClientWebSocket, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delattr(todo_const, "TodoListEntityFeature")
        got = await _run(component_ws, "resolve", refs=["Milk"])
        assert got["error"]["code"] == "unsupported"

    @pytest.mark.parametrize("previous_uid", [None, "1"])
    async def test_command_schema_takes_previous_uid(
        self, component_ws: MockHAClientWebSocket, previous_uid: str | None
    ) -> None:
        reply = await _reply(
            component_ws,
            "update",
            uid="2",
            data={"status": "completed"},
            previous_uid=previous_uid,
        )
        assert reply["success"], reply

    async def test_command_registered_and_advertised(
        self, component_ws: MockHAClientWebSocket
    ) -> None:
        info = await ws_result(component_ws, "ha_mcp_tools/info")
        assert todo_items.CAPABILITY in info["capabilities"]
        assert (await _run(component_ws, "resolve", refs=["Milk"]))["success"]

    async def test_command_is_admin_gated(
        self,
        component_ws: MockHAClientWebSocket,
        component_ws_read_only: MockHAClientWebSocket,
    ) -> None:
        refused = await _reply(component_ws_read_only, "resolve", refs=["Milk"])
        assert refused["success"] is False
        assert refused["error"]["code"] == ERR_UNAUTHORIZED
        assert (await _run(component_ws, "resolve", refs=["Milk"]))["success"]


# --- server client --------------------------------------------------------------


class _Client:
    def __init__(self, result: Any = None, raise_: Exception | None = None) -> None:
        self.sent: list[dict[str, Any]] = []
        self.result = result
        self.raise_ = raise_

    async def send_websocket_message(self, message: dict[str, Any]) -> Any:
        self.sent.append(message)
        if self.raise_ is not None:
            raise self.raise_
        return self.result


@pytest.fixture
def caps(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    served = ComponentCaps(1, "2.2.2", frozenset({component_todo.CAPABILITY}), {})
    monkeypatch.setattr(
        component_todo, "get_component_caps", AsyncMock(return_value=served)
    )
    invalidate = MagicMock()
    monkeypatch.setattr(component_todo, "invalidate_caps", invalidate)
    return invalidate


@pytest.mark.usefixtures("tasks")
class TestClient:
    async def test_reply_comes_back(
        self, component_core_client: HomeAssistantClient
    ) -> None:
        got = await component_todo.todo_item(
            component_core_client, LIST, "resolve", refs=["Milk", "x"]
        )
        assert got is not None
        assert got["success"] is True
        assert [row and row["uid"] for row in got["items"]] == ["1", None]

    async def test_refusal_comes_back(
        self, component_core_client: HomeAssistantClient, tasks: _TaskList
    ) -> None:
        tasks.rows.insert(0, _item("x", "3"))
        got = await component_todo.todo_item(
            component_core_client, LIST, "update", uid="3", data={"status": "completed"}
        )
        assert got is not None
        assert got["success"] is False
        assert got["error"]["code"] == "namesake"

    async def test_capability_absent_sends_nothing(
        self, core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The capability probe gets Core's own unknown_command; a command sent
        # after it would raise here.
        monkeypatch.setattr(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("must not be sent")),
        )
        assert await component_todo.todo_item(core_client, LIST, "resolve") is None

    async def test_not_sent_falls_back(
        self,
        component_core_client: HomeAssistantClient,
        tasks: _TaskList,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            component_core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantCommandNotSent("down")),
        )
        got = await component_todo.todo_item(
            component_core_client, LIST, "add", data={"item": "Eggs"}
        )
        assert got is None
        assert len(tasks.rows) == 3

    # Core closes a socket whose token it refused; the client's close reply
    # then re-arms aiohttp's heartbeat timer on that closed server socket
    # (web_ws.py _on_data_received -> _reset_heartbeat), which nothing cancels.
    @pytest.mark.parametrize("expected_lingering_timers", [True])
    async def test_rejected_token_does_not_fall_back(
        self, component_core_client: HomeAssistantClient, tasks: _TaskList
    ) -> None:
        # The capabilities were read while the token worked; Core then refuses
        # the token when the command needs a new connection.
        assert await component_todo.todo_item(
            component_core_client, LIST, "resolve", refs=["Milk"]
        )
        component_core_client.token = "revoked"
        with pytest.raises(HomeAssistantCommandNotSent) as exc_info:
            await component_todo.todo_item(
                component_core_client, LIST, "add", data={"item": "Eggs"}
            )
        assert isinstance(exc_info.value.__cause__, HomeAssistantAuthError)
        assert len(tasks.rows) == 3

    # The same closed server socket as test_rejected_token_does_not_fall_back.
    @pytest.mark.parametrize("expected_lingering_timers", [True])
    async def test_rejected_token_is_an_auth_error_not_an_unknown_outcome(
        self, component_core_client: HomeAssistantClient
    ) -> None:
        assert await component_todo.todo_item(
            component_core_client, LIST, "resolve", refs=["Milk"]
        )
        component_core_client.token = "revoked"
        with pytest.raises(ToolError) as exc_info:
            await component_todo.todo_call(
                component_core_client, LIST, "add", data={"item": "Eggs"}
            )
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "AUTH_INVALID_TOKEN"
        assert "write_outcome_unknown" not in json.dumps(body)

    async def test_rejection_after_a_sent_attempt_is_an_unknown_outcome(
        self, caps: MagicMock
    ) -> None:
        # Raised when an earlier attempt may have sent the command.
        dropped = HomeAssistantConnectionError("rejected")
        dropped.__cause__ = HomeAssistantAuthError("WebSocket authentication failed")
        with pytest.raises(ToolError) as exc_info:
            await component_todo.todo_call(_Client(raise_=dropped), LIST, "add")
        body = json.loads(str(exc_info.value))
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert body["write_outcome_unknown"] is True

    async def test_unknown_command_falls_back(
        self, core_client: HomeAssistantClient, caps: MagicMock
    ) -> None:
        # The capabilities say todo_item; Core, without the component, answers
        # unknown_command.
        assert await component_todo.todo_item(core_client, LIST, "add") is None
        caps.assert_called_once_with(core_client)

    async def test_core_without_todo_parts_falls_back(
        self,
        component_core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delattr(todo_entity, "api_items_factory")
        got = await component_todo.todo_item(
            component_core_client, LIST, "add", data={"item": "Eggs"}
        )
        assert got is None

    async def test_failed_command_is_an_error(
        self, component_core_client: HomeAssistantClient, tasks: _TaskList
    ) -> None:
        tasks.failure = HomeAssistantError("boom")
        with pytest.raises(ClientError, match="boom"):
            await component_todo.todo_item(
                component_core_client, LIST, "add", data={"item": "Eggs"}
            )
