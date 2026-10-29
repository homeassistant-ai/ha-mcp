"""ha_mcp_tools/todo_item, and the server client that sends it.

The component runs against a fake Core todo: a list entity whose rows Core's
services change, and the first-match lookup Core's services resolve an item
value with.
"""

from __future__ import annotations

import dataclasses
import importlib
import sys
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.client.rest_client import HomeAssistantCommandNotSent, HomeAssistantError
from ha_mcp.tools import component_todo
from ha_mcp.tools.component_api import ComponentCaps

# The sibling module installs the homeassistant.* stubs the component needs, so
# the component module is imported only after it.
from .test_component_ws_search import (
    _REAL_VOL,
    FakeHass,
    _FakeConnection,
    _Unauthorized,
    functional_ws,  # noqa: F401 - pytest fixture
    wsapi,
)

todo_items = importlib.import_module("custom_components.ha_mcp_tools.todo_items")

LIST = "todo.tasks"


class _CoreError(Exception):
    pass


class _ServiceValidationError(_CoreError):
    pass


class _Invalid(Exception):
    pass


@dataclasses.dataclass
class _Item:
    uid: str
    summary: str
    status: str = "needs_action"
    due: date | datetime | None = None
    description: str | None = None


def _find(ref: str, items: list[_Item]) -> _Item | None:
    # Core's _find_by_uid_or_summary: the first row whose uid or summary is ref.
    return next((i for i in items if ref in (i.uid, i.summary)), None)


_MOVE = 8


class _ListEntity:
    def __init__(self, items: list[_Item]) -> None:
        self.entity_id = LIST
        self.todo_items = items
        self.available = True
        self.supported_features = _MOVE
        self.move_error: Exception | None = None
        # (uid, previous_uid, rows as they were when the move came)
        self.moves: list[tuple[str, str | None, list[str]]] = []

    async def async_move_todo_item(self, uid: str, previous_uid: str | None) -> None:
        self.moves.append((uid, previous_uid, [i.summary for i in self.todo_items]))
        if self.move_error is not None:
            raise self.move_error
        row = next(i for i in self.todo_items if i.uid == uid)
        self.todo_items.remove(row)
        at = 0
        if previous_uid is not None:
            at = [i.uid for i in self.todo_items].index(previous_uid) + 1
        self.todo_items.insert(at, row)


class _Services:
    def __init__(self, entity: _ListEntity) -> None:
        self.entity = entity
        self.calls: list[tuple[str, dict[str, Any], Any]] = []
        self.raise_: Exception | None = None
        self.change_then_raise = False

    async def async_call(
        self, domain, service, data, *, target, blocking, context
    ) -> None:
        assert (domain, target, blocking) == ("todo", {"entity_id": LIST}, True)
        self.calls.append((service, data, context))
        items = self.entity.todo_items
        if self.change_then_raise:
            items.append(_Item("listener", "added by a listener"))
        if self.raise_ is not None:
            raise self.raise_
        if service == "add_item":
            items.append(_Item(f"n{len(items)}", data["item"].strip()))
        elif service == "update_item":
            row = _find(data["item"], items)
            assert row is not None
            row.summary = data.get("rename", row.summary)
            row.status = data.get("status", row.status)
        elif service == "remove_item":
            gone = {row.uid for ref in data["item"] if (row := _find(ref, items))}
            self.entity.todo_items = [i for i in items if i.uid not in gone]


def _items_factory(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    return {
        k: v.isoformat() if hasattr(v, "isoformat") else v
        for k, v in pairs
        if v is not None
    }


@pytest.fixture
def entity() -> _ListEntity:
    return _ListEntity(
        [
            _Item("1", "Milk"),
            _Item("2", "Pay", due=datetime(2026, 11, 1, 10, 0)),
            _Item("3", "Milk"),
        ]
    )


@pytest.fixture
def hass(entity: _ListEntity, monkeypatch: pytest.MonkeyPatch) -> Any:
    component = SimpleNamespace(
        get_entity=lambda entity_id: entity if entity_id == LIST else None
    )
    core = SimpleNamespace(
        component_key="todo_component",
        domain="todo",
        list_entity=_ListEntity,
        can_move=_MOVE,
        items_factory=_items_factory,
        find=_find,
        refused=(_Invalid, _ServiceValidationError),
        error=_CoreError,
        date=date.fromisoformat,
    )
    monkeypatch.setattr(todo_items, "_core", lambda: core)
    return SimpleNamespace(
        data={"todo_component": component}, services=_Services(entity)
    )


async def _run(hass: Any, action: str, **fields: Any) -> dict[str, Any]:
    msg = {"entity_id": LIST, "action": action, "ha_mcp_context": "ctx", **fields}
    result: dict[str, Any] = await todo_items.async_todo_item(hass, msg)
    return result


class TestComponent:
    async def test_resolve_takes_cores_first_match(self, hass: Any) -> None:
        got = await _run(hass, "resolve", refs=["Milk", "2", "Nope"])
        assert [row and row["uid"] for row in got["items"]] == ["1", "2", None]
        assert hass.services.calls == []

    async def test_add_returns_the_row_core_stored(self, hass: Any) -> None:
        got = await _run(hass, "add", data={"item": "  Eggs "})
        assert got["item"] == {"uid": "n3", "summary": "Eggs", "status": "needs_action"}
        assert hass.services.calls == [("add_item", {"item": "  Eggs "}, "ctx")]

    async def test_update_goes_to_the_row_with_that_uid(self, hass: Any) -> None:
        got = await _run(hass, "update", uid="3", data={"status": "completed"})
        assert got["item"]["uid"] == "3"
        assert got["item"]["status"] == "completed"
        assert hass.services.calls == [
            ("update_item", {"status": "completed", "item": "3"}, "ctx")
        ]

    async def test_uid_another_item_is_named_after_is_refused(
        self, hass: Any, entity: _ListEntity
    ) -> None:
        entity.todo_items.insert(0, _Item("x", "3"))
        got = await _run(hass, "update", uid="3", data={"status": "completed"})
        assert got["error"]["code"] == "namesake"
        assert hass.services.calls == []

    async def test_unknown_uid_is_not_found(self, hass: Any) -> None:
        got = await _run(hass, "update", uid="Milk", data={"status": "completed"})
        assert got["error"]["code"] == "item_not_found"

    async def test_remove_reports_the_rows_it_removed(self, hass: Any) -> None:
        got = await _run(hass, "remove", refs=["Milk"])
        assert [row["uid"] for row in got["removed"]] == ["1"]

    async def test_unavailable_list_is_refused_before_the_call(
        self, hass: Any, entity: _ListEntity
    ) -> None:
        # Core's services skip an unavailable list and still answer success.
        entity.available = False
        got = await _run(hass, "remove", refs=["Milk"])
        assert got["error"]["code"] == "list_unavailable"
        assert hass.services.calls == []

    async def test_list_not_loaded(self, hass: Any) -> None:
        msg = {"entity_id": "todo.gone", "action": "resolve", "refs": ["Milk"]}
        got = await todo_items.async_todo_item(hass, msg)
        assert got["error"]["code"] == "not_loaded"

    @pytest.mark.parametrize(
        "error", [_ServiceValidationError("no due date"), _Invalid("bad status")]
    )
    async def test_core_refusal_that_changed_nothing(
        self, hass: Any, error: Exception
    ) -> None:
        hass.services.raise_ = error
        got = await _run(hass, "update", uid="2", data={"status": "completed"})
        assert got["error"] == {"code": "invalid", "message": str(error)}

    @pytest.mark.parametrize("error", [_CoreError("timeout"), ValueError("bad json")])
    async def test_integration_error_is_raised_though_the_rows_are_unchanged(
        self, hass: Any, error: Exception
    ) -> None:
        # The integration may have reached its backend before it failed.
        hass.services.raise_ = error
        with pytest.raises(type(error)):
            await _run(hass, "update", uid="2", data={"status": "completed"})

    async def test_a_row_without_uid_does_not_break_the_reply(
        self, hass: Any, entity: _ListEntity
    ) -> None:
        entity.todo_items.append(_Item(None, "No uid"))  # type: ignore[arg-type]
        got = await _run(hass, "remove", refs=["Milk"])
        assert [row["uid"] for row in got["removed"]] == ["1"]

    async def test_add_and_update_read_back_past_a_row_without_uid(
        self, hass: Any, entity: _ListEntity
    ) -> None:
        entity.todo_items.insert(0, _Item(None, "No uid"))  # type: ignore[arg-type]
        added = await _run(hass, "add", data={"item": "Eggs"})
        assert added["item"]["summary"] == "Eggs"
        updated = await _run(hass, "update", uid="2", data={"status": "completed"})
        assert updated["item"]["uid"] == "2"

    async def test_data_shaped_like_a_refusal_goes_to_core(self, hass: Any) -> None:
        await _run(hass, "add", data={"item": "Eggs", "success": False})
        assert hass.services.calls[0][1] == {"item": "Eggs", "success": False}

    async def test_error_after_the_list_changed_is_raised(self, hass: Any) -> None:
        hass.services.raise_ = _ServiceValidationError("listener failed")
        hass.services.change_then_raise = True
        with pytest.raises(_ServiceValidationError):
            await _run(hass, "update", uid="2", data={"status": "completed"})

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
        self, hass: Any, due: str | None, sent: dict[str, Any]
    ) -> None:
        await _run(hass, "update", uid="2", data={}, due=due)
        assert hass.services.calls[0][1] == {"item": "2", **sent}

    async def test_clear_of_a_due_date_names_due_date(
        self, hass: Any, entity: _ListEntity
    ) -> None:
        entity.todo_items.append(_Item("4", "D", due=date(2026, 11, 1)))
        await _run(hass, "update", uid="4", data={}, due=None)
        assert hass.services.calls[0][1] == {"item": "4", "due_date": None}

    async def test_clear_of_an_item_without_due_sends_no_field(self, hass: Any) -> None:
        await _run(hass, "update", uid="1", data={"status": "completed"}, due=None)
        assert hass.services.calls[0][1] == {"status": "completed", "item": "1"}

    async def test_missing_field_for_the_action_is_invalid(self, hass: Any) -> None:
        got = await _run(hass, "update", data={})
        assert got["error"]["code"] == "invalid"

    async def test_update_then_move_by_uid(
        self, hass: Any, entity: _ListEntity
    ) -> None:
        # Below x, the row named "2" comes first; the update goes before the
        # move, on the order Core resolves "2" in, so it changes Pay.
        entity.todo_items.append(_Item("x", "2"))
        got = await _run(
            hass, "update", uid="2", data={"status": "completed"}, previous_uid="x"
        )
        assert got["item"]["status"] == "completed"
        assert entity.moves == [("2", "x", ["Milk", "Pay", "Milk", "2"])]
        assert [i.uid for i in entity.todo_items] == ["1", "3", "x", "2"]
        assert entity.todo_items[2].status == "needs_action"

    async def test_move_to_the_top(self, hass: Any, entity: _ListEntity) -> None:
        await _run(hass, "update", uid="3", data={}, previous_uid=None)
        assert entity.moves[0][:2] == ("3", None)
        assert [i.uid for i in entity.todo_items] == ["3", "1", "2"]

    async def test_list_without_reordering_refuses_before_the_update(
        self, hass: Any, entity: _ListEntity
    ) -> None:
        entity.supported_features = 0
        got = await _run(hass, "update", uid="2", data={}, previous_uid=None)
        assert got["error"]["code"] == "not_supported"
        assert hass.services.calls == []
        assert entity.moves == []

    async def test_failed_move_after_the_update_says_it_was_applied(
        self, hass: Any, entity: _ListEntity
    ) -> None:
        entity.move_error = _CoreError("backend gone")
        got = await _run(
            hass, "update", uid="2", data={"status": "completed"}, previous_uid="1"
        )
        assert got["error"] == {
            "code": "move_failed",
            "message": "The update was applied, but the move failed: backend gone",
        }
        assert entity.todo_items[1].status == "completed"

    async def test_unexpected_move_error_is_raised(
        self, hass: Any, entity: _ListEntity
    ) -> None:
        entity.move_error = RuntimeError("bug")
        with pytest.raises(RuntimeError):
            await _run(hass, "update", uid="2", data={}, previous_uid="1")

    @pytest.mark.parametrize("features", [0, None])
    async def test_list_without_reordering_still_takes_an_update(
        self, hass: Any, entity: _ListEntity, features: Any
    ) -> None:
        entity.supported_features = features
        got = await _run(hass, "update", uid="2", data={"status": "completed"})
        assert got["item"]["status"] == "completed"
        assert entity.moves == []

    async def test_list_without_features_refuses_a_move(
        self, hass: Any, entity: _ListEntity
    ) -> None:
        entity.supported_features = None
        got = await _run(hass, "update", uid="2", data={}, previous_uid=None)
        assert got["error"]["code"] == "not_supported"
        assert hass.services.calls == []

    async def test_previous_uid_moves_only_on_update(
        self, hass: Any, entity: _ListEntity
    ) -> None:
        await _run(hass, "remove", refs=["Pay"], previous_uid="1")
        assert entity.moves == []

    async def test_core_without_its_todo_parts_is_unsupported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(todo_items, "_core", lambda: None)
        got = await todo_items.async_todo_item(
            SimpleNamespace(data={}), {"entity_id": LIST, "action": "resolve"}
        )
        assert got["error"]["code"] == "unsupported"


class _HAError(Exception):
    pass


class _SVError(_HAError):
    pass


def _core_over_stand_ins(monkeypatch: pytest.MonkeyPatch, **const: Any) -> Any:
    """The real _core() over stand-ins for Core's modules."""
    const = {"DATA_COMPONENT": "todo", "DOMAIN": "todo"} | const
    todo = SimpleNamespace(
        const=SimpleNamespace(**const),
        entity=SimpleNamespace(TodoListEntity=_ListEntity, api_items_factory=dict),
        services=SimpleNamespace(_find_by_uid_or_summary=_find),
    )
    cv = SimpleNamespace(date=date.fromisoformat)
    modules = {
        "homeassistant.components.todo": todo,
        "homeassistant.exceptions": SimpleNamespace(
            HomeAssistantError=_HAError, ServiceValidationError=_SVError
        ),
        "homeassistant.helpers": SimpleNamespace(config_validation=cv),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return todo_items._core()


def test_core_parts_refuse_only_validation_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An integration's HomeAssistantError or ValueError must not count as a
    # refusal.
    core = _core_over_stand_ins(
        monkeypatch,
        TodoListEntityFeature=SimpleNamespace(MOVE_TODO_ITEM=_MOVE),
    )
    assert core is not None
    assert _SVError in core.refused
    assert not [e for e in core.refused if issubclass(_HAError, e)]
    assert not [e for e in core.refused if issubclass(ValueError, e)]
    assert core.error is _HAError


def test_core_without_the_move_feature_has_no_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _core_over_stand_ins(monkeypatch) is None


@pytest.mark.parametrize("previous_uid", [None, "1"])
def test_command_schema_takes_previous_uid(previous_uid: str | None) -> None:
    schema = _REAL_VOL.Schema(todo_items.command_specs(_REAL_VOL)[0][0])
    msg = {
        "type": todo_items.WS_TODO_ITEM,
        "entity_id": LIST,
        "action": "update",
        "uid": "2",
        "data": {},
        "previous_uid": previous_uid,
    }
    assert schema(msg) == msg


def test_command_registered_and_advertised(functional_ws) -> None:  # noqa: F811
    assert todo_items.WS_TODO_ITEM in functional_ws.registered
    assert todo_items.CAPABILITY in wsapi.CAPABILITIES


def test_command_is_admin_gated(functional_ws) -> None:  # noqa: F811
    handler = functional_ws.registered[todo_items.WS_TODO_ITEM]
    msg = {"id": 1, "type": todo_items.WS_TODO_ITEM, "entity_id": LIST}
    msg["action"] = "resolve"
    msg["refs"] = ["Milk"]
    with pytest.raises(_Unauthorized):
        handler(FakeHass(), _FakeConnection(is_admin=False), msg)
    connection = _FakeConnection()
    handler(FakeHass(), connection, msg)
    assert 1 in connection.results


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


class TestClient:
    async def test_reply_comes_back(self, caps: MagicMock) -> None:
        reply = {"success": True, "items": [None]}
        client = _Client({"success": True, "result": reply})
        got = await component_todo.todo_item(client, LIST, "resolve", refs=["x"])
        assert got == reply
        assert client.sent == [
            {
                "type": component_todo.WS_TODO_ITEM,
                "entity_id": LIST,
                "action": "resolve",
                "refs": ["x"],
            }
        ]

    async def test_refusal_comes_back(self, caps: MagicMock) -> None:
        reply = {"success": False, "error": {"code": "namesake", "message": "m"}}
        client = _Client({"success": True, "result": reply})
        assert await component_todo.todo_item(client, LIST, "update") == reply

    async def test_capability_absent_sends_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            component_todo, "get_component_caps", AsyncMock(return_value=None)
        )
        client = _Client()
        assert await component_todo.todo_item(client, LIST, "resolve") is None
        assert client.sent == []

    async def test_not_sent_falls_back(self, caps: MagicMock) -> None:
        client = _Client(raise_=HomeAssistantCommandNotSent("down"))
        assert await component_todo.todo_item(client, LIST, "add") is None

    async def test_unknown_command_falls_back(self, caps: MagicMock) -> None:
        result = {"success": False, "error": "x", "error_code": "unknown_command"}
        client = _Client(result)
        assert await component_todo.todo_item(client, LIST, "add") is None
        caps.assert_called_once_with(client)

    async def test_core_without_todo_parts_falls_back(self, caps: MagicMock) -> None:
        reply = {"success": False, "error": {"code": "unsupported", "message": "m"}}
        client = _Client({"success": True, "result": reply})
        assert await component_todo.todo_item(client, LIST, "add") is None

    async def test_failed_command_is_an_error(self, caps: MagicMock) -> None:
        result = {
            "success": False,
            "error": "boom",
            "error_code": "home_assistant_error",
        }
        with pytest.raises(HomeAssistantError, match="boom"):
            await component_todo.todo_item(_Client(result), LIST, "add")
