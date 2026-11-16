"""Todo tools with the component's todo_item bridge, and the backups the
tools take of each item a write changes.

The tools run on the server's own client against a real ``hass``: Core's Local
To-do list, or a test platform's list for a backend Local To-do is not (one
without reordering or descriptions, one that adds two rows, one whose
backend fails). ``component_core_client`` serves the bridge;
``core_client`` is an install without the component, whose writes take Core's
todo services over WebSocket.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError as CoreError
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.component_api import get_component_caps
from ha_mcp.tools.tools_todo import TodoTools

from ._backup_core import backup_settings as backup_settings
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_todo_core import (
    FULL_FEATURES,
    LIST,
    PLAIN_FEATURES,
    PlainList,
    add_item,
    listed,
    move_to_top,
    row_named,
    watch_services,
)
from ._backup_todo_core import plain_list as plain_list
from ._backup_todo_core import tasks as tasks
from ._command_faults import received, unanswered
from ._receipt_faults import on_receipt
from ._tool_errors import error_body, error_of

BRIDGE = "ha_mcp_tools/todo_item"
MOVE = "todo/item/move"

_GONE = (
    "'Buy milk' is gone from the list, but it was not completed when "
    "the list was read, so no backup of it was taken"
)


async def _shopping(hass: HomeAssistant) -> dict[str, str]:
    """``Buy milk``, then ``Pay`` and ``Bread``, both completed, on Local
    To-do's list; the uid Core gave each, by summary."""
    return {
        "Buy milk": await add_item(hass, "Buy milk"),
        "Pay": await add_item(hass, "Pay", completed=True),
        "Bread": await add_item(hass, "Bread", completed=True),
    }


def _answers_lost(client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """From now on the client's WebSocket messages get no answer."""
    monkeypatch.setattr(
        client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
    )


def _sends_lost(client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """From now on the client cannot send its WebSocket messages."""
    monkeypatch.setattr(
        client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantCommandNotSent("connection lost")),
    )


def _resolving(msg: dict[str, Any]) -> bool:
    return msg.get("action") == "resolve"


def _actions(seen: dict[str, list[Any]]) -> list[str]:
    return [msg["action"] for msg in seen[BRIDGE]]


class TestBridge:
    """With the component, writes go by the uid it resolved with Core's own
    lookup; only a move alone goes through Core's todo/item/move."""

    async def test_a_created_item_is_the_row_core_stored(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tasks: MockConfigEntry,
    ) -> None:
        with received(hass, BRIDGE, "call_service") as seen:
            got = await TodoTools(component_core_client).ha_set_todo_item(
                LIST, summary="  Eggs "
            )
        assert [msg["data"] for msg in seen[BRIDGE]] == [{"item": "  Eggs "}]
        assert seen["call_service"] == []
        assert got["todo_item"] == row_named(
            await listed(component_core_client), "Eggs"
        )

    async def test_an_update_goes_by_the_resolved_uid(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tasks: MockConfigEntry,
    ) -> None:
        uids = await _shopping(hass)
        with received(hass, BRIDGE, "call_service") as seen:
            got = await TodoTools(component_core_client).ha_set_todo_item(
                LIST, item="Pay", rename="Paid"
            )
        update = seen[BRIDGE][-1]
        assert (update["action"], update["uid"]) == ("update", uids["Pay"])
        assert seen["call_service"] == []
        assert (got["todo_item"]["uid"], got["todo_item"]["summary"]) == (
            uids["Pay"],
            "Paid",
        )

    async def test_an_update_naming_no_field_gets_cores_own_refusal(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tasks: MockConfigEntry,
    ) -> None:
        await _shopping(hass)
        with received(hass, BRIDGE) as seen, pytest.raises(ToolError) as excinfo:
            await TodoTools(component_core_client).ha_set_todo_item(LIST, item="Pay")
        assert _actions(seen) == ["resolve", "update"]
        assert error_of(excinfo)["code"] == "VALIDATION_INVALID_PARAMETER"

    async def test_a_move_alone_not_read_back_is_a_warning(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tasks: MockConfigEntry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _shopping(hass)
        with on_receipt(
            hass, MOVE, lambda msg: _answers_lost(component_core_client, monkeypatch)
        ):
            got = await TodoTools(component_core_client).ha_set_todo_item(
                LIST, item="Bread", move_after="Buy milk"
            )
        assert got["todo_item"] is None
        assert len(got["warnings"]) == 1
        assert "could not be read back" in got["warnings"][0]

    async def test_an_update_core_would_make_to_a_namesake_is_refused(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tasks: MockConfigEntry,
    ) -> None:
        uids = await _shopping(hass)
        await move_to_top(hass, await add_item(hass, uids["Pay"]))
        with pytest.raises(ToolError) as excinfo:
            await TodoTools(component_core_client).ha_set_todo_item(
                LIST, item="Pay", rename="Paid"
            )
        assert error_of(excinfo)["code"] == "VALIDATION_INVALID_PARAMETER"
        rows = await listed(component_core_client)
        assert row_named(rows, "Pay")["uid"] == uids["Pay"]

    async def test_a_move_alone_takes_cores_move_with_the_resolved_uids(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tasks: MockConfigEntry,
    ) -> None:
        uids = await _shopping(hass)
        with received(hass, BRIDGE, MOVE) as seen:
            await TodoTools(component_core_client).ha_set_todo_item(
                LIST, item="Bread", move_after="Buy milk"
            )
        assert [(msg["uid"], msg["previous_uid"]) for msg in seen[MOVE]] == [
            (uids["Bread"], uids["Buy milk"])
        ]
        assert _actions(seen) == ["resolve"]
        rows = await listed(component_core_client)
        assert [row["summary"] for row in rows] == ["Buy milk", "Bread", "Pay"]

    async def test_an_unknown_item_is_not_found_and_nothing_is_written(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tasks: MockConfigEntry,
    ) -> None:
        await _shopping(hass)
        with received(hass, BRIDGE) as seen, pytest.raises(ToolError) as excinfo:
            await TodoTools(component_core_client).ha_set_todo_item(
                LIST, item="Nope", status="completed"
            )
        assert error_of(excinfo)["code"] == "RESOURCE_NOT_FOUND"
        assert _actions(seen) == ["resolve"]

    async def test_a_string_naming_an_item_is_that_item_not_a_json_list(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tasks: MockConfigEntry,
    ) -> None:
        milk = await add_item(hass, "Milk")
        listed_milk = await add_item(hass, '["Milk"]')
        tools = TodoTools(component_core_client)
        first = await tools.ha_remove_todo_item(LIST, item='["Milk"]')
        second = await tools.ha_remove_todo_item(LIST, item='["Milk"]')
        assert [i["uid"] for i in first["removed"]] == [listed_milk]
        assert [i["uid"] for i in second["removed"]] == [milk]

    async def test_a_list_gone_unavailable_since_its_state_was_read_is_unavailable(
        self, component_core_client: HomeAssistantClient, plain_list: PlainList
    ) -> None:
        # Its state, which the tool reads first, still says available.
        plain_list.is_available = False
        with pytest.raises(ToolError) as excinfo:
            await TodoTools(component_core_client).ha_remove_todo_item(LIST, item="Pay")
        error = error_of(excinfo)
        assert (error["code"], error["message"]) == (
            "ENTITY_UNAVAILABLE",
            f"{LIST} is unavailable",
        )
        assert [row.summary for row in plain_list.rows] == ["Buy milk", "Pay"]

    async def test_a_write_without_an_answer_is_an_unknown_outcome(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tasks: MockConfigEntry,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await _shopping(hass)
        with (
            on_receipt(
                hass,
                BRIDGE,
                lambda msg: _answers_lost(component_core_client, monkeypatch),
                when=_resolving,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await TodoTools(component_core_client).ha_remove_todo_item(LIST, item="Pay")
        error = error_body(excinfo)
        assert error["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "did not return its outcome" in error["error"]["message"]
        assert error["write_outcome_unknown"] is True

    async def test_an_update_with_a_move_is_one_component_call_updating_first(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tasks: MockConfigEntry,
    ) -> None:
        # Below x, a row named after Pay's uid comes first; the component
        # updates before it moves, so the update still lands on Pay.
        uids = await _shopping(hass)
        x = await add_item(hass, uids["Pay"])
        with received(hass, BRIDGE, MOVE) as seen:
            got = await TodoTools(component_core_client).ha_set_todo_item(
                LIST, item="Pay", move_after=x, status="needs_action"
            )
        assert seen[MOVE] == []
        update = seen[BRIDGE][-1]
        assert (update["uid"], update["previous_uid"]) == (uids["Pay"], x)
        rows = await listed(component_core_client)
        assert [row["uid"] for row in rows] == [
            uids["Buy milk"],
            uids["Bread"],
            x,
            uids["Pay"],
        ]
        assert got["todo_item"]["status"] == "needs_action"
        assert rows[3]["status"] == "needs_action"

    async def test_a_move_to_the_top_with_an_update_sends_no_previous_uid(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tasks: MockConfigEntry,
    ) -> None:
        await _shopping(hass)
        with received(hass, BRIDGE) as seen:
            await TodoTools(component_core_client).ha_set_todo_item(
                LIST, item="Bread", move_after="", rename="Rye"
            )
        assert seen[BRIDGE][-1]["previous_uid"] is None
        rows = await listed(component_core_client)
        assert [row["summary"] for row in rows] == ["Rye", "Buy milk", "Pay"]

    async def test_a_move_on_a_list_without_reordering_is_refused_before_the_update(
        self, component_core_client: HomeAssistantClient, plain_list: PlainList
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await TodoTools(component_core_client).ha_set_todo_item(
                LIST, item="Pay", move_after="", status="needs_action"
            )
        error = error_of(excinfo)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert error["message"] == f"{LIST} does not support reordering items"
        assert plain_list.rows[1].status == TodoItemStatus.COMPLETED

    async def test_a_failed_move_after_the_update_says_the_update_was_applied(
        self,
        component_core_client: HomeAssistantClient,
        plain_list: PlainList,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        plain_list.features = FULL_FEATURES | TodoListEntityFeature.MOVE_TODO_ITEM
        monkeypatch.setattr(
            plain_list, "async_move_todo_item", AsyncMock(side_effect=CoreError("gone"))
        )
        with pytest.raises(ToolError) as excinfo:
            await TodoTools(component_core_client).ha_set_todo_item(
                LIST, item="Pay", move_after="", status="needs_action"
            )
        error = error_of(excinfo)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert error["message"] == "The update was applied, but the move failed: gone"
        assert plain_list.rows[1].status == TodoItemStatus.NEEDS_ACTION

    async def test_cores_refusal_comes_through_as_invalid_input(
        self, component_core_client: HomeAssistantClient, plain_list: PlainList
    ) -> None:
        plain_list.features = PLAIN_FEATURES
        with pytest.raises(ToolError) as excinfo:
            await TodoTools(component_core_client).ha_set_todo_item(
                LIST, item="Pay", description="x"
            )
        error = error_of(excinfo)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "description" in error["message"]

    async def test_a_new_item_not_told_apart_is_a_warning(
        self, component_core_client: HomeAssistantClient, plain_list: PlainList
    ) -> None:
        plain_list.created_as = lambda item: [
            item,
            TodoItem(uid="x", summary="other", status=TodoItemStatus.NEEDS_ACTION),
        ]
        got = await TodoTools(component_core_client).ha_set_todo_item(
            LIST, summary="Eggs"
        )
        assert got["todo_item"] is None
        assert "not confirmed" in got["message"]
        assert got["warnings"][0].startswith("Could not tell the new item apart")

    @pytest.mark.parametrize("move_after", [None, ""])
    async def test_a_write_not_sent_after_the_resolve_is_a_connection_failure(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tasks: MockConfigEntry,
        monkeypatch: pytest.MonkeyPatch,
        move_after: str | None,
    ) -> None:
        await _shopping(hass)
        with (
            received(hass, MOVE) as seen,
            on_receipt(
                hass,
                BRIDGE,
                lambda msg: _sends_lost(component_core_client, monkeypatch),
                when=_resolving,
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await TodoTools(component_core_client).ha_set_todo_item(
                LIST, item="Pay", status="needs_action", move_after=move_after
            )
        assert error_of(excinfo)["code"] == "CONNECTION_FAILED"
        # The move goes with the update, so nothing was moved either.
        assert seen[MOVE] == []

    # Core closes a socket whose token it refused; the client's close reply
    # then re-arms aiohttp's heartbeat timer on that closed server socket,
    # which nothing cancels.
    @pytest.mark.parametrize("expected_lingering_timers", [True])
    async def test_a_rejected_token_does_not_fall_back_to_cores_services(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        tasks: MockConfigEntry,
        sent: AsyncMock,
    ) -> None:
        # The capabilities were read while the token worked; Core refuses it
        # when the bridge needs a new connection, and Core's WebSocket
        # commands would connect again with it.
        assert await get_component_caps(component_core_client)
        component_core_client.token = "revoked"
        services = watch_services(hass)
        with pytest.raises(ToolError) as excinfo:
            await TodoTools(component_core_client).ha_set_todo_item(
                LIST, summary="Eggs"
            )
        body = error_body(excinfo)
        assert body["error"]["code"] == "AUTH_INVALID_TOKEN"
        assert "write_outcome_unknown" not in str(body)
        assert [call.args[0]["type"] for call in sent.await_args_list] == [BRIDGE]
        assert services() == []

    async def test_a_removal_still_on_the_list_read_back_is_a_warning(
        self,
        component_core_client: HomeAssistantClient,
        plain_list: PlainList,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def deleted_on_next_sync(uids: list[str]) -> None:
            """A backend that drops the rows only when the list syncs again."""

        monkeypatch.setattr(plain_list, "async_delete_todo_items", deleted_on_next_sync)
        got = await TodoTools(component_core_client).ha_remove_todo_item(
            LIST, item="Pay"
        )
        assert got["warnings"][0].startswith("The list read back after the removal")


def _backed_up(manager: bm.BackupManager) -> dict[str, str]:
    """The summary each todo item snapshot holds, by its backup key."""
    return {
        row["entity_id"]: manager.read_snapshot(row["name"])["config"]["summary"]
        for row in manager.list_snapshots(domain="todo_item")
    }


def _complete_buy_milk(plain_list: PlainList) -> None:
    plain_list.rows[0] = dataclasses.replace(
        plain_list.rows[0], status=TodoItemStatus.COMPLETED
    )


def _delete_buy_milk(plain_list: PlainList) -> None:
    plain_list.rows[:] = [row for row in plain_list.rows if row.uid != "1"]


def _at_remove_completed(
    hass: HomeAssistant, act: Callable[[], object]
) -> AbstractContextManager[None]:
    """Run ``act`` when Core receives the ``remove_completed_items`` call,
    before it runs it: after the tool read the list and took its backups."""
    return on_receipt(
        hass,
        "call_service",
        lambda msg: act(),
        when=lambda msg: msg.get("service") == "remove_completed_items",
    )


@pytest.mark.usefixtures("backup_settings")
class TestBackups:
    """Each item a write changes is backed up under its uid first; a backup
    that was not taken is named in warnings."""

    async def test_each_removed_item_is_backed_up_before_it_is_removed(
        self,
        core_client: HomeAssistantClient,
        plain_list: PlainList,
        snapshot_manager: bm.BackupManager,
    ) -> None:
        got = await TodoTools(core_client).ha_remove_todo_item(
            LIST, item=["Pay", "Buy milk"]
        )
        assert _backed_up(snapshot_manager) == {
            f"{LIST}::2": "Pay",
            f"{LIST}::1": "Buy milk",
        }
        assert "warnings" not in got
        assert plain_list.rows == []

    async def test_remove_completed_backs_up_the_completed_items_first(
        self,
        core_client: HomeAssistantClient,
        plain_list: PlainList,
        snapshot_manager: bm.BackupManager,
    ) -> None:
        got = await TodoTools(core_client).ha_remove_todo_item(
            LIST, remove_completed=True
        )
        assert _backed_up(snapshot_manager) == {f"{LIST}::2": "Pay"}
        assert "warnings" not in got
        assert [row.summary for row in plain_list.rows] == ["Buy milk"]

    async def test_remove_completed_names_an_item_completed_after_the_read(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        plain_list: PlainList,
    ) -> None:
        # Another client completes "Buy milk" after the backups were taken, so
        # Core's remove_completed_items removes it too.
        with _at_remove_completed(hass, lambda: _complete_buy_milk(plain_list)):
            got = await TodoTools(core_client).ha_remove_todo_item(
                LIST, remove_completed=True
            )
        assert plain_list.rows == []
        assert [i["uid"] for i in got["removed"]] == ["2"]
        assert got["warnings"] == [_GONE]

    async def test_remove_completed_names_an_open_item_deleted_meanwhile(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        plain_list: PlainList,
    ) -> None:
        with _at_remove_completed(hass, lambda: _delete_buy_milk(plain_list)):
            got = await TodoTools(core_client).ha_remove_todo_item(
                LIST, remove_completed=True
            )
        assert [i["uid"] for i in got["removed"]] == ["2"]
        assert got["warnings"] == [_GONE]

    async def test_remove_completed_names_nothing_gone_when_the_read_back_fails(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        plain_list: PlainList,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def delete_then_lose_answers() -> None:
            _delete_buy_milk(plain_list)
            _answers_lost(core_client, monkeypatch)

        with _at_remove_completed(hass, delete_then_lose_answers):
            got = await TodoTools(core_client).ha_remove_todo_item(
                LIST, remove_completed=True
            )
        assert got["removed"] is None
        assert len(got["warnings"]) == 1
        assert "could not be read back" in got["warnings"][0]

    async def test_an_item_deleted_meanwhile_is_not_named_with_auto_backup_off(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        plain_list: PlainList,
        backup_settings: config.Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(backup_settings, "enable_auto_backup", False)
        with _at_remove_completed(hass, lambda: _delete_buy_milk(plain_list)):
            got = await TodoTools(core_client).ha_remove_todo_item(
                LIST, remove_completed=True
            )
        assert [i["uid"] for i in got["removed"]] == ["2"]
        assert "warnings" not in got

    async def test_an_update_backs_up_the_resolved_item_first(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        plain_list: PlainList,
        snapshot_manager: bm.BackupManager,
    ) -> None:
        with received(hass, "call_service") as seen:
            await TodoTools(core_client).ha_set_todo_item(
                LIST, item="Pay", rename="Paid"
            )
        assert [msg["service"] for msg in seen["call_service"]] == ["update_item"]
        assert _backed_up(snapshot_manager) == {f"{LIST}::2": "Pay"}
        assert plain_list.rows[1].summary == "Paid"

    async def test_a_bridge_update_backs_up_the_resolved_item_first(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        plain_list: PlainList,
        snapshot_manager: bm.BackupManager,
    ) -> None:
        with received(hass, BRIDGE) as seen:
            await TodoTools(component_core_client).ha_set_todo_item(
                LIST, item="Pay", rename="Paid"
            )
        assert _actions(seen) == ["resolve", "update"]
        assert _backed_up(snapshot_manager) == {f"{LIST}::2": "Pay"}
        assert plain_list.rows[1].summary == "Paid"

    async def test_an_update_names_a_backup_not_taken_and_still_writes(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
        plain_list: PlainList,
        snapshot_manager: bm.BackupManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Only the backup reads the list over todo/item/list; the bridge
        # resolves and writes.
        async with unanswered(hass, "todo/item/list", monkeypatch):
            got = await TodoTools(component_core_client).ha_set_todo_item(
                LIST, item="Pay", rename="Paid"
            )
        assert len(got["warnings"]) == 1
        assert got["warnings"][0].startswith(
            f"No pre-write backup of todo_item:{LIST}::2 was taken"
        )
        assert _backed_up(snapshot_manager) == {}
        assert plain_list.rows[1].summary == "Paid"

    async def test_a_backup_that_cannot_start_is_a_warning_and_the_write_goes_ahead(
        self,
        core_client: HomeAssistantClient,
        plain_list: PlainList,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            bm, "get_backup_manager", Mock(side_effect=OSError("disk full"))
        )
        got = await TodoTools(core_client).ha_remove_todo_item(LIST, item="Pay")
        assert got["warnings"] == ["No pre-write backup was taken: OSError"]
        assert [row.summary for row in plain_list.rows] == ["Buy milk"]

    async def test_no_backup_is_taken_with_auto_backup_off(
        self,
        core_client: HomeAssistantClient,
        plain_list: PlainList,
        snapshot_manager: bm.BackupManager,
        backup_settings: config.Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(backup_settings, "enable_auto_backup", False)
        await TodoTools(core_client).ha_remove_todo_item(LIST, item="Pay")
        assert _backed_up(snapshot_manager) == {}
        assert [row.summary for row in plain_list.rows] == ["Buy milk"]
