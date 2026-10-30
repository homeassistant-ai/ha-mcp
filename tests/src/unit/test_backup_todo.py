"""Todo item backups (``ha_mcp.backup_todo``): snapshot and restore payloads.

A fake ``_ws_send`` holds one todo list and applies ``todo.*`` calls to it the
way Core's todo services do, so each test reads the list a restore leaves.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from starlette.requests import Request

from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_helper_target import safety_target as restore_safety_target
from ha_mcp.backup_todo import (
    TodoListNotLoaded,
    current_key,
    fetch_todo_item,
    find_item,
    restore_todo_item,
    safety_target,
)
from ha_mcp.client.rest_client import HomeAssistantCommandError, HomeAssistantError
from ha_mcp.settings_ui import _handlers_backups as settings_ui

from .test_backup_diff_error_mapping import _backup_settings, _dispatcher
from .test_backup_manager import _mk_handler, _mk_manager
from .test_backup_restore_consumers import response_for

LIST = "todo.tasks"


class _FakeList:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = [dict(i) for i in items]
        self.calls: list[dict[str, Any]] = []
        self.next_uid = 100
        self.state: str | None = "2"
        self.loaded = True
        self.registered = True
        self.refuse: str | None = None

    async def state_of(self, _client: Any, path: str) -> Any:
        if path != f"states/{LIST}" or self.state is None:
            return None
        return {"state": self.state}

    async def send(self, _client: Any, message: dict[str, Any]) -> Any:
        message = dict(message)
        kind = message.pop("type")
        if kind == "todo/item/list":
            if message["entity_id"] != LIST or not self.loaded:
                raise HomeAssistantCommandError("Entity not found", "not_found")
            return {"items": [dict(i) for i in self.items]}
        if kind == "config/entity_registry/get":
            if message["entity_id"] != LIST or not self.registered:
                raise HomeAssistantCommandError("Entity not found", "not_found")
            return {"entity_id": LIST}
        assert kind == "call_service"
        assert message["target"] == {"entity_id": LIST}
        self.calls.append(message)
        data = message["service_data"]
        if message["service"] == self.refuse:
            raise HomeAssistantCommandError("refused", "service_validation_error")
        if message["service"] == "add_item":
            self.next_uid += 1
            row = {"uid": str(self.next_uid), "summary": data["item"]}
            row["status"] = "needs_action"
            self._apply(row, data)
            self.items.append(row)
        elif message["service"] == "update_item":
            row = next(
                i for i in self.items if data["item"] in (i["uid"], i["summary"])
            )
            if data.get("rename"):
                row["summary"] = data["rename"]
            if data.get("status"):
                row["status"] = data["status"]
            self._apply(row, data)
        return None

    @staticmethod
    def _apply(row: dict[str, Any], data: dict[str, Any]) -> None:
        # Core keeps None fields off the listed row (api_items_factory).
        if "description" in data:
            row["description"] = data["description"]
        for key in ("due_date", "due_datetime"):
            if key in data:
                row["due"] = data[key]
        for key in ("description", "due"):
            if row.get(key) is None:
                row.pop(key, None)


@pytest.fixture(autouse=True)
def _no_component() -> Iterator[None]:
    """Without the component a restore takes Core's todo services."""
    with patch("ha_mcp.backup_todo.todo_item", AsyncMock(return_value=None)):
        yield


@pytest.fixture
def todo_manager(tmp_path: Any) -> Any:
    mgr = _mk_manager(tmp_path)
    mgr.register(bm.DomainHandler("todo_item", fetch_todo_item, restore_todo_item))
    return mgr


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> _FakeList:
    fake = _FakeList(
        [
            {"uid": "1", "summary": "Buy milk", "status": "needs_action"},
            {"uid": "2", "summary": "Pay", "status": "completed", "due": "2026-11-01"},
        ]
    )
    monkeypatch.setattr(bm, "_ws_send", fake.send)
    monkeypatch.setattr(bm, "_rest_get_or_none", fake.state_of)
    return fake


class TestFetch:
    @pytest.mark.parametrize("ref", ["Buy milk", "1"])
    async def test_matches_summary_or_uid(self, fake: _FakeList, ref: str) -> None:
        got = await fetch_todo_item(object(), f"{LIST}::{ref}")
        assert got == {
            "todo_entity_id": LIST,
            "uid": "1",
            "summary": "Buy milk",
            "status": "needs_action",
        }

    async def test_unknown_item_is_a_miss(self, fake: _FakeList) -> None:
        assert await fetch_todo_item(object(), f"{LIST}::Bread") is None

    async def test_list_not_loaded_is_no_miss(self, fake: _FakeList) -> None:
        # Whether the item is on a list Core has not loaded is unknown.
        fake.loaded = False
        with pytest.raises(TodoListNotLoaded):
            await fetch_todo_item(object(), f"{LIST}::Pay")

    async def test_list_with_a_state_is_no_miss(self, fake: _FakeList) -> None:
        fake.loaded = False
        fake.registered = False
        with pytest.raises(TodoListNotLoaded):
            await fetch_todo_item(object(), f"{LIST}::Pay")

    async def test_deleted_list_is_a_miss(self, fake: _FakeList) -> None:
        assert await fetch_todo_item(object(), "todo.gone::Bread") is None

    async def test_uid_wins_over_an_item_named_like_it(self, fake: _FakeList) -> None:
        fake.items.insert(0, {"uid": "9", "summary": "2", "status": "needs_action"})
        got = await fetch_todo_item(object(), f"{LIST}::2")
        assert got is not None
        assert got["summary"] == "Pay"

    async def test_key_without_item_is_a_miss(self, fake: _FakeList) -> None:
        assert await fetch_todo_item(object(), f"{LIST}::") is None

    async def test_malformed_reply_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def send(_client: Any, _message: dict[str, Any]) -> Any:
            return ["not", "a", "dict"]

        monkeypatch.setattr(bm, "_ws_send", send)
        with pytest.raises(HomeAssistantError, match="Expected a dict"):
            await fetch_todo_item(object(), f"{LIST}::Buy milk")


class TestRestoreEditedItem:
    async def test_writes_the_row_back_under_its_uid(self, fake: _FakeList) -> None:
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        row = fake.items[1]
        row.update(summary="Paid", status="needs_action", description="added")
        row["due"] = "2026-12-24T10:00:00+01:00"

        await restore_todo_item(object(), f"{LIST}::Pay", snapshot)

        assert fake.items[1] == {
            "uid": "2",
            "summary": "Pay",
            "status": "completed",
            "due": "2026-11-01",
        }
        assert fake.calls[-1]["service_data"] == {
            "item": "2",
            "rename": "Pay",
            "status": "completed",
            "description": None,
            "due_date": "2026-11-01",
        }

    async def test_clears_a_due_date_the_snapshot_did_not_have(
        self, fake: _FakeList
    ) -> None:
        snapshot = await fetch_todo_item(object(), f"{LIST}::Buy milk")
        fake.items[0]["due"] = "2026-12-24T10:00:00+01:00"

        await restore_todo_item(object(), f"{LIST}::Buy milk", snapshot)

        assert "due" not in fake.items[0]
        assert fake.calls[-1]["service_data"]["due_datetime"] is None

    async def test_sends_no_field_neither_side_has(self, fake: _FakeList) -> None:
        # Core refuses description/due on a list without those features.
        snapshot = await fetch_todo_item(object(), f"{LIST}::Buy milk")
        await restore_todo_item(object(), f"{LIST}::Buy milk", snapshot)
        assert set(fake.calls[-1]["service_data"]) == {"item", "rename", "status"}


class TestRestoreRemovedItem:
    async def test_adds_it_again_with_its_status(self, fake: _FakeList) -> None:
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        fake.items.pop(1)

        await restore_todo_item(object(), f"{LIST}::Pay", snapshot)

        assert [c["service"] for c in fake.calls] == ["add_item", "update_item"]
        assert fake.calls[0]["service_data"] == {
            "item": "Pay",
            "due_date": "2026-11-01",
        }
        assert fake.items[-1] == {
            "uid": "101",
            "summary": "Pay",
            "status": "completed",
            "due": "2026-11-01",
        }

    async def test_open_item_needs_no_status_call(self, fake: _FakeList) -> None:
        snapshot = await fetch_todo_item(object(), f"{LIST}::Buy milk")
        fake.items.pop(0)

        await restore_todo_item(object(), f"{LIST}::Buy milk", snapshot)

        assert [c["service"] for c in fake.calls] == ["add_item"]
        assert fake.items[-1]["summary"] == "Buy milk"

    async def test_unidentifiable_new_item_raises(
        self, fake: _FakeList, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        fake.items.pop(1)
        send = fake.send

        async def add_two(client: Any, message: dict[str, Any]) -> Any:
            result = await send(client, message)
            if message.get("service") == "add_item":
                fake.items.append(
                    {"uid": "x", "summary": "other", "status": "needs_action"}
                )
            return result

        monkeypatch.setattr(bm, "_ws_send", add_two)
        with pytest.raises(HomeAssistantError, match="could not be told apart"):
            await restore_todo_item(object(), f"{LIST}::Pay", snapshot)

    async def test_missing_list_raises(self, fake: _FakeList) -> None:
        snapshot = {"todo_entity_id": "todo.gone", "uid": "1", "summary": "x"}
        with pytest.raises(HomeAssistantError, match="no longer exists"):
            await restore_todo_item(object(), "todo.gone::x", snapshot)


class TestRestoreGuards:
    async def test_unavailable_list_is_refused_before_writing(
        self, fake: _FakeList
    ) -> None:
        # Core skips an unavailable list and still answers success.
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        fake.state = "unavailable"
        with pytest.raises(bm.BackupRestoreError, match="unavailable") as excinfo:
            await restore_todo_item(object(), f"{LIST}::Pay", snapshot)
        assert excinfo.value.outcome["apply_status"] == "not_applied"
        assert excinfo.value.outcome["reason"] == "entity_unavailable"
        assert fake.calls == []

    async def test_list_without_registry_entry_is_restored(
        self, fake: _FakeList
    ) -> None:
        # A list set up without a unique id has a state but no registry entry.
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        assert snapshot is not None
        fake.registered = False
        fake.items[1]["summary"] = "Paid"
        await restore_todo_item(object(), f"{LIST}::Pay", snapshot)
        assert fake.items[1]["summary"] == "Pay"

    async def test_list_not_loaded_is_refused_as_not_applied(
        self, fake: _FakeList, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        fake.loaded = False
        with pytest.raises(
            bm.BackupRestoreError, match="has not loaded the todo list"
        ) as excinfo:
            await restore_todo_item(object(), f"{LIST}::Pay", snapshot)
        assert excinfo.value.outcome["apply_status"] == "not_applied"
        assert excinfo.value.outcome["reason"] == "entity_unavailable"
        with pytest.raises(bm.BackupRestoreError) as safety:
            await safety_target(object(), f"{LIST}::Pay", snapshot)
        assert safety.value.outcome["apply_status"] == "not_applied"
        assert safety.value.outcome["reason"] == "entity_unavailable"
        assert fake.calls == []

    async def test_snapshot_without_uid_updates_the_item_by_summary(
        self, fake: _FakeList
    ) -> None:
        snapshot = {"todo_entity_id": LIST, "summary": "Pay", "status": "needs_action"}
        await restore_todo_item(object(), f"{LIST}::Pay", snapshot)
        assert [c["service"] for c in fake.calls] == ["update_item"]
        assert len(fake.items) == 2
        assert fake.items[1]["status"] == "needs_action"

    async def test_failed_status_step_says_the_item_was_added(
        self, fake: _FakeList
    ) -> None:
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        fake.items.pop(1)
        fake.refuse = "update_item"
        with pytest.raises(HomeAssistantError, match=r"Re-added 'Pay'.*no answer"):
            await restore_todo_item(object(), f"{LIST}::Pay", snapshot)
        assert fake.items[-1]["summary"] == "Pay"


class TestRenamedItem:
    """A backup keyed by the old summary finds the item by its uid."""

    def test_current_key_uses_the_uid(self) -> None:
        assert current_key(f"{LIST}::Pay", {"uid": "2"}) == f"{LIST}::2"
        assert current_key(f"{LIST}::Pay", {"summary": "Pay"}) == f"{LIST}::Pay"

    async def test_safety_backup_follows_the_uid(self, fake: _FakeList) -> None:
        fake.items[1]["summary"] = "Paid"
        target = await restore_safety_target(
            object(), "todo_item", f"{LIST}::Pay", {"uid": "2"}
        )
        assert target == f"{LIST}::2"

    async def test_no_safety_backup_for_a_removed_item(self, fake: _FakeList) -> None:
        fake.items.pop(1)
        assert await safety_target(object(), f"{LIST}::Pay", {"uid": "2"}) is None

    async def test_row_named_after_a_removed_items_uid_is_not_it(
        self, fake: _FakeList, todo_manager: Any
    ) -> None:
        # The restore adds the item again and leaves that row alone, so it
        # takes no safety backup of it and the diff finds the item missing.
        path = await todo_manager.maybe_snapshot("todo_item", f"{LIST}::2")
        assert path is not None
        fake.items[1] = {"uid": "9", "summary": "2", "status": "needs_action"}
        assert await safety_target(object(), f"{LIST}::2", {"uid": "2"}) is None
        result = await todo_manager.diff_snapshot(path.name)
        assert result["entity_missing"] is True

    async def test_snapshot_without_uid_is_the_item_named_so(
        self, fake: _FakeList
    ) -> None:
        item = {"summary": "Pay", "status": "needs_action"}
        assert await safety_target(object(), f"{LIST}::Pay", item) == f"{LIST}::Pay"

    async def test_diff_reads_the_item_under_its_uid(self, tmp_path: Any) -> None:
        mgr = _mk_manager(tmp_path)
        handler = _mk_handler("todo_item", fetched={"uid": "2", "summary": "Pay"})
        mgr.register(handler)
        path = await mgr.maybe_snapshot("todo_item", f"{LIST}::Pay")
        assert path is not None
        keys: list[str] = []

        async def fetch(_client: Any, key: str) -> Any:
            keys.append(key)
            return {"uid": "2", "summary": "Paid"}

        mgr.register(bm.DomainHandler("todo_item", fetch, handler.restore))
        result = await mgr.diff_snapshot(path.name)
        assert keys == [f"{LIST}::2"]
        assert result["entity_missing"] is False


class TestListNotLoadedCodes:
    """A list Home Assistant has not loaded is ENTITY_UNAVAILABLE; one it no
    longer has is ENTITY_NOT_FOUND. A disabled list may have no state (state
    None) but keeps its registry entry."""

    @staticmethod
    async def _restore(
        manager: Any, name: str, consumer: str, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[int | None, dict[str, Any]]:
        if consumer == "settings":
            response = response_for(monkeypatch, manager, name)
            return response.status_code, response.json()
        _patch_mcp(manager, monkeypatch)
        with pytest.raises(ToolError) as caught:
            await _dispatcher()(scope="edits", action="restore", backup_name=name)
        return None, json.loads(str(caught.value))

    @pytest.mark.parametrize("consumer", ["settings", "mcp"])
    @pytest.mark.parametrize("state", ["unavailable", None])
    @pytest.mark.parametrize("auto_backup", [True, False])
    async def test_restore(
        self,
        fake: _FakeList,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
        consumer: str,
        state: str | None,
        auto_backup: bool,
    ) -> None:
        mgr = _mk_manager(tmp_path, enable_auto_backup=auto_backup)
        mgr.register(bm.DomainHandler("todo_item", fetch_todo_item, restore_todo_item))
        path = await mgr.maybe_snapshot("todo_item", f"{LIST}::2", force=True)
        assert path is not None
        fake.loaded = False
        fake.state = state
        status, payload = await self._restore(mgr, path.name, consumer, monkeypatch)
        assert status in (None, 409)
        assert payload["error"]["code"] == "ENTITY_UNAVAILABLE"
        assert payload["data"]["apply_status"] == "not_applied"
        assert fake.calls == []

    @pytest.mark.parametrize("consumer", ["settings", "mcp"])
    @pytest.mark.parametrize("state", ["unavailable", None])
    async def test_diff(
        self,
        fake: _FakeList,
        todo_manager: Any,
        monkeypatch: pytest.MonkeyPatch,
        consumer: str,
        state: str | None,
    ) -> None:
        path = await todo_manager.maybe_snapshot("todo_item", f"{LIST}::2")
        assert path is not None
        fake.loaded = False
        fake.state = state
        if consumer == "settings":
            monkeypatch.setattr(settings_ui, "_backup_mgr", lambda server: todo_manager)
            response = await settings_ui._diff_backup(
                None, Request({"type": "http", "path_params": {"name": path.name}})
            )
            assert response.status_code == 409
            payload = json.loads(response.body)
        else:
            _patch_mcp(todo_manager, monkeypatch)
            with pytest.raises(ToolError) as caught:
                await _dispatcher()(scope="edits", action="diff", backup_name=path.name)
            payload = json.loads(str(caught.value))
            assert payload["backup_name"] == path.name
        assert payload["error"]["code"] == "ENTITY_UNAVAILABLE"
        assert "has not loaded the todo list" in payload["error"]["message"]

    async def test_diff_of_a_deleted_list_finds_the_item_missing(
        self, fake: _FakeList, todo_manager: Any
    ) -> None:
        path = await todo_manager.maybe_snapshot("todo_item", f"{LIST}::2")
        assert path is not None
        fake.loaded = False
        fake.state = None
        fake.registered = False
        result = await todo_manager.diff_snapshot(path.name)
        assert result["entity_missing"] is True

    @pytest.mark.parametrize("consumer", ["settings", "mcp"])
    @pytest.mark.parametrize("auto_backup", [True, False])
    async def test_restore_to_a_deleted_list(
        self,
        fake: _FakeList,
        tmp_path: Any,
        monkeypatch: pytest.MonkeyPatch,
        auto_backup: bool,
        consumer: str,
    ) -> None:
        # With auto-backup the safety backup reads the list first; both ways
        # a deleted list is not found.
        mgr = _mk_manager(tmp_path, enable_auto_backup=auto_backup)
        mgr.register(bm.DomainHandler("todo_item", fetch_todo_item, restore_todo_item))
        path = await mgr.maybe_snapshot("todo_item", f"{LIST}::2", force=True)
        assert path is not None
        fake.loaded = False
        fake.state = None
        fake.registered = False
        status, payload = await self._restore(mgr, path.name, consumer, monkeypatch)
        assert status in (None, 404)
        assert payload["error"]["code"] == "ENTITY_NOT_FOUND"
        assert "no longer exists" in payload["error"]["message"]
        assert fake.calls == []


def _patch_mcp(manager: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ha_mcp.tools.backup.get_backup_manager", lambda *args: manager)
    monkeypatch.setattr("ha_mcp.tools.backup.get_global_settings", _backup_settings)
    monkeypatch.setattr("ha_mcp.read_only.get_global_settings", _backup_settings)


class TestReviewGaps:
    async def test_other_list_errors_are_not_a_miss(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def send(_client: Any, _message: dict[str, Any]) -> Any:
            raise HomeAssistantCommandError("Unauthorized", "unauthorized")

        monkeypatch.setattr(bm, "_ws_send", send)
        with pytest.raises(HomeAssistantCommandError):
            await fetch_todo_item(object(), f"{LIST}::Pay")

    async def test_reply_without_item_list_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def send(_client: Any, _message: dict[str, Any]) -> Any:
            return {"items": None}

        monkeypatch.setattr(bm, "_ws_send", send)
        with pytest.raises(HomeAssistantError, match="Expected a list"):
            await fetch_todo_item(object(), f"{LIST}::Pay")

    async def test_restores_a_cleared_description(self, fake: _FakeList) -> None:
        fake.items[0]["description"] = "two litres"
        snapshot = await fetch_todo_item(object(), f"{LIST}::Buy milk")
        fake.items[0].pop("description")
        await restore_todo_item(object(), f"{LIST}::Buy milk", snapshot)
        assert fake.items[0]["description"] == "two litres"

    def test_find_item_takes_the_first_match(self) -> None:
        rows = [{"uid": "a", "summary": "Pay"}, {"uid": "b", "summary": "Pay"}]
        assert find_item(rows, "Pay") == rows[0]

    async def test_re_added_item_is_completed_by_its_new_uid(
        self, fake: _FakeList
    ) -> None:
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        fake.items[1]["uid"] = "older"
        fake.items[1]["status"] = "needs_action"
        snapshot = {**snapshot, "uid": "gone"}

        await restore_todo_item(object(), f"{LIST}::Pay", snapshot)

        assert fake.calls[-1]["service_data"] == {"item": "101", "status": "completed"}
        assert fake.items[1]["status"] == "needs_action"


class TestSnapshotFieldsAndGuards:
    async def test_snapshot_leaves_out_the_completed_time(
        self, fake: _FakeList
    ) -> None:
        fake.items[1]["completed"] = "2026-10-01T10:00:00+00:00"
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        assert snapshot is not None
        assert "completed" not in snapshot

    async def test_list_comes_from_the_backup_key(self, fake: _FakeList) -> None:
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        fake.items[1]["status"] = "needs_action"
        await restore_todo_item(
            object(), f"{LIST}::Pay", {**snapshot, "todo_entity_id": "todo.other"}
        )
        assert fake.items[1]["status"] == "completed"

    async def test_item_named_like_the_uid_blocks_the_restore(
        self, fake: _FakeList
    ) -> None:
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        fake.items.insert(0, {"uid": "9", "summary": "2", "status": "needs_action"})
        with pytest.raises(bm.BackupRestoreError, match="nothing was restored") as e:
            await restore_todo_item(object(), f"{LIST}::Pay", snapshot)
        assert e.value.outcome["apply_status"] == "not_applied"
        assert fake.calls == []

    async def test_foreign_new_item_is_not_completed(
        self, fake: _FakeList, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        fake.items.pop(1)
        send = fake.send

        async def swap_added(client: Any, message: dict[str, Any]) -> Any:
            result = await send(client, message)
            if message.get("service") == "add_item":
                # Another writer replaced the re-added item before the read.
                fake.items[-1]["summary"] = "other"
            return result

        monkeypatch.setattr(bm, "_ws_send", swap_added)
        with pytest.raises(HomeAssistantError, match="could not be told apart"):
            await restore_todo_item(object(), f"{LIST}::Pay", snapshot)
        assert fake.items[-1]["status"] == "needs_action"

    async def test_diff_ignores_completed_in_an_older_snapshot(
        self, tmp_path: Any
    ) -> None:
        # Snapshots from before this change stored the list's completion time.
        mgr = _mk_manager(tmp_path)
        row = {"uid": "2", "summary": "Pay", "status": "completed"}
        mgr.register(
            _mk_handler(
                "todo_item",
                fetched={**row, "completed": "2026-10-01T10:00:00+00:00"},
            )
        )
        path = await mgr.maybe_snapshot("todo_item", f"{LIST}::Pay")
        assert path is not None
        mgr.register(_mk_handler("todo_item", fetched=row))
        result = await mgr.diff_snapshot(path.name)
        assert result["unchanged"] is True

    async def test_re_added_item_without_uid_is_not_completed(
        self, fake: _FakeList, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        fake.items.pop(1)
        send = fake.send

        async def drop_uid(client: Any, message: dict[str, Any]) -> Any:
            result = await send(client, message)
            if message.get("service") == "add_item":
                fake.items[-1]["uid"] = ""
            return result

        monkeypatch.setattr(bm, "_ws_send", drop_uid)
        with pytest.raises(HomeAssistantError, match="could not be told apart"):
            await restore_todo_item(object(), f"{LIST}::Pay", snapshot)
        assert [c["service"] for c in fake.calls] == ["add_item"]

    async def test_item_named_like_the_new_uid_blocks_the_completion(
        self, fake: _FakeList
    ) -> None:
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        fake.items.pop(1)
        # The re-added item gets uid "101"; an earlier item is named "101".
        fake.items.insert(0, {"uid": "z", "summary": "101", "status": "needs_action"})
        with pytest.raises(HomeAssistantError, match="could not be told apart"):
            await restore_todo_item(object(), f"{LIST}::Pay", snapshot)
        assert [c["service"] for c in fake.calls] == ["add_item"]
        assert fake.items[0]["status"] == "needs_action"

    def test_warning_for_a_removed_item_names_its_summary(self) -> None:
        from ha_mcp.backup_helper_target import note_no_safety_backup

        outcome: dict[str, Any] = {}
        note_no_safety_backup(
            outcome, "todo_item", None, {"uid": "2", "summary": "Pay"}
        )
        assert outcome["warnings"] == [
            "No safety backup was taken: todo_item Pay no longer exists, "
            "so this restore overwrites none of its state."
        ]


class _Bridge:
    """The component's ``todo_item`` over the fake list."""

    def __init__(self, fake: _FakeList) -> None:
        self.fake = fake
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.refusal: dict[str, str] | None = None

    async def __call__(
        self, _client: Any, entity_id: str, action: str, **fields: Any
    ) -> dict[str, Any]:
        assert entity_id == LIST
        if action == "resolve":
            found = [find_item(self.fake.items, ref) for ref in fields["refs"]]
            return {"success": True, "items": found}
        self.calls.append((action, fields))
        if self.refusal:
            return {"success": False, "error": self.refusal}
        if action == "add":
            self.fake.next_uid += 1
            row = {"uid": str(self.fake.next_uid), "summary": fields["data"]["item"]}
            self.fake.items.append(row | {"status": "needs_action"})
            return {"success": True, "item": dict(self.fake.items[-1])}
        row = next(i for i in self.fake.items if i["uid"] == fields["uid"])
        row.update({k: v for k, v in fields["data"].items() if k == "status"})
        return {"success": True, "item": dict(row)}


@pytest.fixture
def bridge(fake: _FakeList) -> Iterator[_Bridge]:
    component = _Bridge(fake)
    with patch("ha_mcp.backup_todo.todo_item", component):
        yield component


class TestRestoreThroughTheComponent:
    """With the component, a restore writes by uid and hands Core's own date
    validator the snapshot's due as listed."""

    async def test_edited_item_is_updated_by_uid(
        self, fake: _FakeList, bridge: _Bridge
    ) -> None:
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        await restore_todo_item(object(), f"{LIST}::Pay", snapshot)
        assert bridge.calls == [
            (
                "update",
                {
                    "uid": "2",
                    "data": {"rename": "Pay", "status": "completed"},
                    "due": "2026-11-01",
                },
            )
        ]
        assert fake.calls == []

    async def test_removed_completed_item_is_added_then_completed(
        self, fake: _FakeList, bridge: _Bridge
    ) -> None:
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        fake.items.pop(1)
        await restore_todo_item(object(), f"{LIST}::Pay", snapshot)
        assert bridge.calls == [
            ("add", {"data": {"item": "Pay"}, "due": "2026-11-01"}),
            ("update", {"uid": "101", "data": {"status": "completed"}}),
        ]
        assert fake.items[-1]["status"] == "completed"

    async def test_refusal_is_not_applied(
        self, fake: _FakeList, bridge: _Bridge
    ) -> None:
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        bridge.refusal = {"code": "namesake", "message": "Another item is named"}
        with pytest.raises(bm.BackupRestoreError, match="Another item") as excinfo:
            await restore_todo_item(object(), f"{LIST}::Pay", snapshot)
        assert excinfo.value.outcome["apply_status"] == "not_applied"
        assert fake.items[1]["summary"] == "Pay"

    async def test_re_added_item_not_told_apart_is_reported_as_added(
        self, fake: _FakeList, bridge: _Bridge
    ) -> None:
        # A retry would add the item twice, so this is no not-applied refusal.
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")
        fake.items.pop(1)

        async def add_unclear(*args: Any, **fields: Any) -> dict[str, Any]:
            reply = await bridge(*args, **fields)
            return reply | {"item": None} if args[2] == "add" else reply

        with (
            patch("ha_mcp.backup_todo.todo_item", add_unclear),
            pytest.raises(HomeAssistantError, match="Re-added 'Pay'") as excinfo,
        ):
            await restore_todo_item(object(), f"{LIST}::Pay", snapshot)
        assert not isinstance(excinfo.value, bm.BackupRestoreError)
        assert [c[0] for c in bridge.calls] == ["add"]

    async def test_write_without_answer_is_no_refusal(
        self, fake: _FakeList, bridge: _Bridge
    ) -> None:
        snapshot = await fetch_todo_item(object(), f"{LIST}::Pay")

        async def lost(*args: Any, **fields: Any) -> dict[str, Any]:
            if args[2] == "resolve":
                return await bridge(*args, **fields)
            raise HomeAssistantError("connection lost")

        with (
            patch("ha_mcp.backup_todo.todo_item", lost),
            pytest.raises(HomeAssistantError, match="connection lost") as excinfo,
        ):
            await restore_todo_item(object(), f"{LIST}::Pay", snapshot)
        assert not isinstance(excinfo.value, bm.BackupRestoreError)
