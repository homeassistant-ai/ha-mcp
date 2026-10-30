"""Todo tools with the component's todo_item bridge, and the backups the
tools take of each item a write changes."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_todo import unbacked_removals
from ha_mcp.client.rest_client import HomeAssistantError
from ha_mcp.tools.tools_todo import TodoTools

from .test_tools_todo import LIST, _error, _FakeClient, _no_component  # noqa: F401


@pytest.fixture
def client() -> _FakeClient:
    return _FakeClient()


@pytest.fixture
def tools(client: _FakeClient) -> TodoTools:
    return TodoTools(client)


class _Bridge:
    """The component's ``todo_item`` over the fake list: Core's first-match
    lookup for names, writes by uid, rows read back."""

    def __init__(self, client: _FakeClient) -> None:
        self.client = client
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.refusal: dict[str, str] | None = None
        # A reply per action that stands in for what the component answers.
        self.replies: dict[str, dict[str, Any] | None] = {}

    def _find(self, ref: str) -> dict[str, Any] | None:
        return next(
            (i for i in self.client.items if ref in (i["uid"], i["summary"])), None
        )

    async def __call__(
        self, _client: Any, entity_id: str, action: str, **fields: Any
    ) -> dict[str, Any] | None:
        assert entity_id == LIST
        self.calls.append((action, fields))
        if action in self.replies:
            return self.replies[action]
        if self.refusal and action != "resolve":
            return {"success": False, "error": self.refusal}
        if action == "resolve":
            return {"success": True, "items": [self._find(r) for r in fields["refs"]]}
        if action == "add":
            row = {"uid": "9", "summary": fields["data"]["item"].strip()}
            self.client.items.append(row | {"status": "needs_action"})
            return {"success": True, "item": dict(self.client.items[-1])}
        if action == "update":
            row = next(i for i in self.client.items if i["uid"] == fields["uid"])
            if self._find(fields["uid"]) is not row:
                return {
                    "success": False,
                    "error": {"code": "namesake", "message": "Another item is named"},
                }
            data = fields["data"]
            if "rename" in data:
                row["summary"] = data["rename"]
            if "status" in data:
                row["status"] = data["status"]
            if "previous_uid" in fields:
                # After the update, by uid, as the component moves it.
                previous = fields["previous_uid"]
                message = {"uid": row["uid"]} | (
                    {"previous_uid": previous} if previous else {}
                )
                self.client._move(message)
            return {"success": True, "item": dict(row)}
        asked = {row["uid"] for r in fields["refs"] if (row := self._find(r))}
        removed = [i for i in self.client.items if i["uid"] in asked]
        self.client.items = [i for i in self.client.items if i["uid"] not in asked]
        return {"success": True, "removed": removed}


@pytest.fixture
def bridge(client: _FakeClient) -> Iterator[_Bridge]:
    fake = _Bridge(client)
    with patch("ha_mcp.tools.component_todo.todo_item", fake):
        yield fake


class TestBridge:
    """With the component, writes go by the uid it resolved with Core's own
    lookup; only a move alone goes through Core's todo/item/move."""

    async def test_create_returns_the_row_the_component_read(
        self, tools: TodoTools, client: _FakeClient, bridge: _Bridge
    ) -> None:
        got = await tools.ha_set_todo_item(LIST, summary="  Eggs ")
        assert bridge.calls == [("add", {"data": {"item": "  Eggs "}})]
        assert got["todo_item"]["uid"] == "9"
        assert client.services() == []

    async def test_update_goes_by_the_resolved_uid(
        self, tools: TodoTools, client: _FakeClient, bridge: _Bridge
    ) -> None:
        got = await tools.ha_set_todo_item(LIST, item="Pay", rename="Paid")
        assert bridge.calls[-1] == ("update", {"uid": "2", "data": {"rename": "Paid"}})
        assert got["todo_item"]["summary"] == "Paid"
        assert client.services() == []

    async def test_update_naming_no_field_still_goes_to_the_component(
        self, tools: TodoTools, bridge: _Bridge
    ) -> None:
        # Core decides whether an update names anything to change.
        await tools.ha_set_todo_item(LIST, item="Pay")
        assert ("update", {"uid": "2", "data": {}}) in bridge.calls

    async def test_move_alone_not_read_back_is_a_warning(
        self, tools: TodoTools, client: _FakeClient, bridge: _Bridge
    ) -> None:
        client.drop = "todo/item/list"
        got = await tools.ha_set_todo_item(LIST, item="Bread", move_after="Buy milk")
        assert got["todo_item"] is None
        assert len(got["warnings"]) == 1
        assert "could not be read back" in got["warnings"][0]

    async def test_namesake_refusal_is_invalid_input(
        self, tools: TodoTools, client: _FakeClient, bridge: _Bridge
    ) -> None:
        client.items.insert(0, {"uid": "x", "summary": "2", "status": "needs_action"})
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Pay", rename="Paid")
        assert _error(excinfo)["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert client.items[2]["summary"] == "Pay"

    async def test_move_uses_the_resolved_uids(
        self, tools: TodoTools, client: _FakeClient, bridge: _Bridge
    ) -> None:
        await tools.ha_set_todo_item(LIST, item="Bread", move_after="Buy milk")
        moves = [c for c in client.calls if c["type"] == "todo/item/move"]
        assert moves == [
            {
                "type": "todo/item/move",
                "entity_id": LIST,
                "uid": "3",
                "previous_uid": "1",
            }
        ]
        assert [c[0] for c in bridge.calls] == ["resolve"]

    async def test_unknown_item_is_not_found(
        self, tools: TodoTools, client: _FakeClient, bridge: _Bridge
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Nope", status="completed")
        assert _error(excinfo)["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert [c[0] for c in bridge.calls] == ["resolve"]

    async def test_string_naming_an_item_is_that_item(
        self, tools: TodoTools, client: _FakeClient, bridge: _Bridge
    ) -> None:
        client.items += [
            {"uid": "4", "summary": "Milk", "status": "needs_action"},
            {"uid": "5", "summary": '["Milk"]', "status": "needs_action"},
        ]
        first = await tools.ha_remove_todo_item(LIST, item='["Milk"]')
        second = await tools.ha_remove_todo_item(LIST, item='["Milk"]')
        assert [i["uid"] for i in first["removed"]] == ["5"]
        assert [i["uid"] for i in second["removed"]] == ["4"]

    async def test_unavailable_list_refusal_is_entity_unavailable(
        self, tools: TodoTools, client: _FakeClient, bridge: _Bridge
    ) -> None:
        bridge.refusal = {"code": "list_unavailable", "message": "unavailable"}
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_remove_todo_item(LIST, item="Pay")
        assert _error(excinfo)["error"]["code"] == "ENTITY_UNAVAILABLE"

    async def test_write_without_answer_is_an_unknown_outcome(
        self, tools: TodoTools, client: _FakeClient, bridge: _Bridge
    ) -> None:
        async def lost(*args: Any, **fields: Any) -> Any:
            if args[2] == "resolve":
                return await bridge(*args, **fields)
            raise HomeAssistantError("connection lost")

        with (
            patch("ha_mcp.tools.component_todo.todo_item", lost),
            pytest.raises(ToolError) as excinfo,
        ):
            await tools.ha_remove_todo_item(LIST, item="Pay")
        error = _error(excinfo)
        assert error["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "did not return its outcome" in error["error"]["message"]
        assert error["write_outcome_unknown"] is True

    async def test_update_with_a_move_goes_to_the_component_in_one_call(
        self, tools: TodoTools, client: _FakeClient, bridge: _Bridge
    ) -> None:
        # Below x, a row named "2" comes first; the component updates before it
        # moves, so the update still lands on Pay.
        client.items.append({"uid": "x", "summary": "2", "status": "needs_action"})
        got = await tools.ha_set_todo_item(
            LIST, item="Pay", move_after="x", status="needs_action"
        )
        assert not [c for c in client.calls if c["type"] == "todo/item/move"]
        assert bridge.calls[1] == (
            "update",
            {"uid": "2", "data": {"status": "needs_action"}, "previous_uid": "x"},
        )
        assert [i["uid"] for i in client.items] == ["1", "3", "x", "2"]
        assert got["todo_item"]["status"] == "needs_action"
        assert client.items[2]["status"] == "needs_action"

    async def test_move_to_the_top_with_an_update_sends_no_previous_uid(
        self, tools: TodoTools, client: _FakeClient, bridge: _Bridge
    ) -> None:
        await tools.ha_set_todo_item(LIST, item="Bread", move_after="", rename="Rye")
        assert bridge.calls[1] == (
            "update",
            {"uid": "3", "data": {"rename": "Rye"}, "previous_uid": None},
        )
        assert [i["summary"] for i in client.items] == ["Rye", "Buy milk", "Pay"]

    @pytest.mark.parametrize(
        ("code", "expected", "message"),
        [
            (
                "not_supported",
                "VALIDATION_INVALID_PARAMETER",
                f"{LIST} does not support reordering items",
            ),
            (
                "move_failed",
                "SERVICE_CALL_FAILED",
                "The update was applied, but the move failed: gone",
            ),
        ],
    )
    async def test_move_refusal_or_failure_reaches_the_caller(
        self, tools: TodoTools, bridge: _Bridge, code: str, expected: str, message: str
    ) -> None:
        bridge.replies["update"] = {
            "success": False,
            "error": {"code": code, "message": message},
        }
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Pay", move_after="", status="x")
        error = _error(excinfo)["error"]
        assert error["code"] == expected
        assert error["message"] == message

    async def test_core_refusal_comes_through_as_invalid_input(
        self, tools: TodoTools, client: _FakeClient, bridge: _Bridge
    ) -> None:
        bridge.refusal = {"code": "invalid", "message": "no description support"}
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(LIST, item="Pay", description="x")
        error = _error(excinfo)["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "no description support" in error["message"]

    async def test_new_item_not_told_apart_is_a_warning(
        self, tools: TodoTools, bridge: _Bridge
    ) -> None:
        bridge.replies["add"] = {"success": True, "item": None}
        got = await tools.ha_set_todo_item(LIST, summary="Eggs")
        assert got["todo_item"] is None
        assert "not confirmed" in got["message"]
        assert got["warnings"][0].startswith("Could not tell the new item apart")

    @pytest.mark.parametrize("move_after", [None, ""])
    async def test_write_not_sent_after_the_resolve_is_a_connection_failure(
        self,
        tools: TodoTools,
        client: _FakeClient,
        bridge: _Bridge,
        move_after: str | None,
    ) -> None:
        bridge.replies["update"] = None
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_set_todo_item(
                LIST, item="Pay", status="needs_action", move_after=move_after
            )
        error = _error(excinfo)["error"]
        assert error["code"] == "CONNECTION_FAILED"
        # The move goes with the update, so nothing was moved either.
        assert not [c for c in client.calls if c["type"] == "todo/item/move"]

    async def test_removal_still_on_the_list_read_back_is_a_warning(
        self, tools: TodoTools, bridge: _Bridge
    ) -> None:
        bridge.replies["remove"] = {"success": True, "removed": []}
        got = await tools.ha_remove_todo_item(LIST, item="Pay")
        assert got["warnings"][0].startswith("The list read back after the removal")


class _Snapshots:
    def __init__(self, client: _FakeClient) -> None:
        self.client = client
        self.keys: list[str] = []
        # The list and the number of service writes when each backup was taken.
        self.seen: list[tuple[list[dict[str, Any]], int]] = []
        # Another client's change while a backup is taken.
        self.during: Any = None

    async def maybe_snapshot(self, domain: str, entity_id: str, **kwargs: Any) -> None:
        assert domain == "todo_item"
        self.keys.append(entity_id)
        self.seen.append(([dict(i) for i in self.client.items], self.client.writes))
        if self.during:
            self.during()
        if entity_id.endswith("::3"):
            kwargs["skip_reasons"].append("No pre-write backup of 3")


_GONE = (
    "'Buy milk' is gone from the list, but it was not completed when "
    "the list was read, so no backup of it was taken"
)


@pytest.fixture
def snapshots(client: _FakeClient) -> Iterator[_Snapshots]:
    fake = _Snapshots(client)
    settings = type("S", (), {"enable_auto_backup": True})()
    with (
        patch("ha_mcp.backup_todo.get_global_settings", return_value=settings),
        patch("ha_mcp.backup_manager.get_backup_manager", return_value=fake),
    ):
        yield fake


class TestBackups:
    """Each item a write changes is backed up under its uid first; a backup
    that was not taken is named in warnings."""

    async def test_each_removed_item_is_backed_up(
        self, tools: TodoTools, client: _FakeClient, snapshots: _Snapshots
    ) -> None:
        got = await tools.ha_remove_todo_item(LIST, item=["Pay", "Buy milk"])
        assert snapshots.keys == [f"{LIST}::2", f"{LIST}::1"]
        assert "warnings" not in got

    async def test_remove_completed_backs_up_the_completed_items(
        self, tools: TodoTools, client: _FakeClient, snapshots: _Snapshots
    ) -> None:
        got = await tools.ha_remove_todo_item(LIST, remove_completed=True)
        assert snapshots.keys == [f"{LIST}::2", f"{LIST}::3"]
        assert got["warnings"] == ["No pre-write backup of 3"]

    async def test_remove_completed_names_an_item_completed_after_the_read(
        self, tools: TodoTools, client: _FakeClient, snapshots: _Snapshots
    ) -> None:
        # Another client completes "Buy milk" while the backups are taken, so
        # Core's remove_completed_items removes it too.
        snapshots.during = lambda: client.items[0].update(status="completed")
        got = await tools.ha_remove_todo_item(LIST, remove_completed=True)
        assert snapshots.keys == [f"{LIST}::2", f"{LIST}::3"]
        assert client.items == []
        assert [i["uid"] for i in got["removed"]] == ["2", "3"]
        assert got["warnings"] == ["No pre-write backup of 3", _GONE]

    async def test_remove_completed_names_an_open_item_deleted_meanwhile(
        self, tools: TodoTools, client: _FakeClient, snapshots: _Snapshots
    ) -> None:
        def delete_buy_milk() -> None:
            client.items = [i for i in client.items if i["uid"] != "1"]

        # Another client deletes the open "Buy milk" while the backups are taken.
        snapshots.during = delete_buy_milk
        got = await tools.ha_remove_todo_item(LIST, remove_completed=True)
        assert [i["uid"] for i in got["removed"]] == ["2", "3"]
        assert got["warnings"] == ["No pre-write backup of 3", _GONE]

    async def test_remove_completed_names_nothing_gone_when_the_read_back_fails(
        self, tools: TodoTools, client: _FakeClient, snapshots: _Snapshots
    ) -> None:
        client.read_back_fails = True
        got = await tools.ha_remove_todo_item(LIST, remove_completed=True)
        assert got["removed"] is None
        assert got["warnings"][0] == "No pre-write backup of 3"
        assert len(got["warnings"]) == 2
        assert "could not be read back" in got["warnings"][1]

    @pytest.mark.parametrize(("enabled", "count"), [(True, 1), (False, 0)])
    def test_unbacked_removal_is_named_only_with_auto_backup_on(
        self, enabled: bool, count: int
    ) -> None:
        settings = type("S", (), {"enable_auto_backup": enabled})()
        before = [{"uid": "1", "summary": "Buy milk", "status": "needs_action"}]
        with patch("ha_mcp.backup_todo.get_global_settings", return_value=settings):
            assert len(unbacked_removals(before, [])) == count
            assert unbacked_removals(before, None) == []

    async def test_update_backs_up_the_resolved_item(
        self, tools: TodoTools, client: _FakeClient, snapshots: _Snapshots
    ) -> None:
        await tools.ha_set_todo_item(LIST, item="Pay", rename="Paid")
        assert snapshots.keys == [f"{LIST}::2"]

    @pytest.mark.parametrize(
        "write",
        [
            {"item": ["Pay"]},
            {"remove_completed": True},
        ],
    )
    async def test_backup_is_taken_before_the_removal(
        self,
        tools: TodoTools,
        client: _FakeClient,
        snapshots: _Snapshots,
        write: dict[str, Any],
    ) -> None:
        await tools.ha_remove_todo_item(LIST, **write)
        items, writes = snapshots.seen[0]
        assert writes == 0
        assert "2" in [i["uid"] for i in items]

    async def test_backup_is_taken_before_the_bridge_update(
        self,
        tools: TodoTools,
        client: _FakeClient,
        bridge: _Bridge,
        snapshots: _Snapshots,
    ) -> None:
        await tools.ha_set_todo_item(LIST, item="Pay", rename="Paid")
        items, _ = snapshots.seen[0]
        assert next(i for i in items if i["uid"] == "2")["summary"] == "Pay"
        assert client.items[1]["summary"] == "Paid"

    async def test_update_names_a_backup_not_taken(
        self, tools: TodoTools, client: _FakeClient, snapshots: _Snapshots
    ) -> None:
        got = await tools.ha_set_todo_item(LIST, item="Bread", rename="Rye")
        assert got["warnings"] == ["No pre-write backup of 3"]

    async def test_capture_error_is_a_warning_and_the_write_goes_ahead(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        settings = type("S", (), {"enable_auto_backup": True})()
        with (
            patch("ha_mcp.backup_todo.get_global_settings", return_value=settings),
            patch(
                "ha_mcp.backup_manager.get_backup_manager",
                side_effect=OSError("disk full"),
            ),
        ):
            got = await tools.ha_remove_todo_item(LIST, item="Pay")
        assert got["warnings"] == ["No pre-write backup was taken: OSError"]
        assert client.services() == [("remove_item", {"item": ["Pay"]})]

    async def test_no_backup_with_auto_backup_off(
        self, tools: TodoTools, client: _FakeClient
    ) -> None:
        settings = type("S", (), {"enable_auto_backup": False})()
        manager = AsyncMock()
        with (
            patch("ha_mcp.backup_todo.get_global_settings", return_value=settings),
            patch("ha_mcp.backup_manager.get_backup_manager", return_value=manager),
        ):
            await tools.ha_remove_todo_item(LIST, item="Pay")
        manager.maybe_snapshot.assert_not_called()
