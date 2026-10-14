"""ha_mcp_tools home context storage: revisions, conflicts and scope resolution."""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace
from typing import Any

import pytest

# The sibling module installs the homeassistant.* stubs the component needs.
from ._component_ws_fakes import _FakeConnection, _FakeWSApi, _Unauthorized
from .test_component_ws_search import _REAL_VOL, FakeHass, wsapi

hc = importlib.import_module("custom_components.ha_mcp_tools.home_context")


class FakeStore:
    def __init__(self, data: Any = None) -> None:
        self.data = data
        self.saves = 0

    async def async_load(self) -> Any:
        return self.data

    async def async_save(self, data: Any) -> None:
        # Yield like the real executor-backed save, so concurrent writers interleave.
        await asyncio.sleep(0)
        self.data = data
        self.saves += 1


def _book(store: FakeStore | None = None) -> Any:
    ticks = iter(range(1, 10_000))
    return hc.HomeContextBook(store or FakeStore(), lambda: f"t{next(ticks)}")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _write(book: Any, scope: str, content: str | None, expected: int) -> Any:
    return await book.write(scope, content, expected, None, "agent", "user-1")


class TestRevisions:
    def test_a_write_based_on_a_stale_revision_is_refused(self) -> None:
        async def scenario() -> Any:
            book = _book()
            await _write(book, "home", "first", 0)
            await _write(book, "home", "second", 1)
            return await _write(book, "home", "lost update", 1), await book.get(
                "home", None, False
            )

        refused, current = run(scenario())
        assert refused["success"] is False
        assert refused["error"]["code"] == "conflict"
        assert refused["error"]["latest_revision"] == 2
        assert current["document"]["content"] == "second"

    def test_a_delete_keeps_the_history_and_numbering_continues(self) -> None:
        async def scenario() -> Any:
            book = _book()
            await _write(book, "topic:pv", "battery", 0)
            await _write(book, "topic:pv", None, 1)
            deleted = await book.get("topic:pv", None, True)
            recreated = await _write(book, "topic:pv", "again", 2)
            old = await book.get("topic:pv", 1, False)
            return deleted, recreated, old, await book.index()

        deleted, recreated, old, index = run(scenario())
        assert deleted["exists"] is False
        assert [h["deleted"] for h in deleted["history"]] == [True, False]
        assert recreated["revision"] == 3
        assert old["document"]["content"] == "battery"
        assert [row["scope"] for row in index] == ["topic:pv"]

    def test_deleting_a_missing_document_is_not_a_revision(self) -> None:
        book = _book()
        result = run(_write(book, "home", None, 0))
        assert result["error"]["code"] == "not_found"
        assert run(book.get("home", None, True))["history"] == []

    def test_history_is_capped_but_revision_numbers_keep_growing(self) -> None:
        async def scenario() -> Any:
            book = _book()
            for expected in range(hc.MAX_HISTORY + 5):
                await _write(book, "home", f"v{expected + 1}", expected)
            return await book.get("home", None, True), await book.get("home", 1, False)

        current, first = run(scenario())
        assert current["latest_revision"] == hc.MAX_HISTORY + 5
        assert len(current["history"]) == hc.MAX_HISTORY
        assert first["error"]["code"] == "not_found"

    def test_documents_survive_a_restart(self) -> None:
        store = FakeStore()
        run(_write(_book(store), "area:bath", "# Bath\nslow floor heating", 0))
        reloaded = run(_book(store).get("area:bath", None, False))
        assert reloaded["document"]["content"].endswith("slow floor heating")
        assert reloaded["document"]["author"] == "agent"
        assert reloaded["document"]["user_id"] == "user-1"

    def test_a_failed_save_leaves_the_loaded_state_unchanged(self) -> None:
        class FailingStore(FakeStore):
            async def async_save(self, data: Any) -> None:
                raise OSError("disk full")

        book = _book(FailingStore())
        with pytest.raises(OSError):
            run(_write(book, "home", "text", 0))
        assert run(book.get("home", None, False))["latest_revision"] == 0

    def test_concurrent_writes_on_one_revision_let_exactly_one_win(self) -> None:
        async def scenario() -> list[Any]:
            book = _book()
            return await asyncio.gather(
                *(_write(book, "home", f"writer {n}", 0) for n in range(5))
            )

        results = run(scenario())
        assert sum(r["success"] for r in results) == 1


def _registries() -> Any:
    entities = {
        "light.desk": SimpleNamespace(device_id="dev1", area_id=None),
        "sensor.loose": SimpleNamespace(device_id=None, area_id="garden"),
    }
    devices = {"dev1": SimpleNamespace(area_id="office")}
    areas = {
        "office": SimpleNamespace(floor_id="upstairs"),
        "garden": SimpleNamespace(floor_id=None),
    }
    return SimpleNamespace(
        entity=SimpleNamespace(async_get=entities.get),
        device=SimpleNamespace(async_get=devices.get),
        area=SimpleNamespace(async_get_area=areas.get),
    )


class TestScopes:
    @pytest.mark.parametrize(
        ("scope", "expected"),
        [
            (
                "entity:light.desk",
                ["device:dev1", "area:office", "floor:upstairs", "home"],
            ),
            ("entity:sensor.loose", ["area:garden", "home"]),
            ("device:dev1", ["area:office", "floor:upstairs", "home"]),
            ("topic:heating", ["home"]),
            ("home", []),
        ],
    )
    def test_a_scope_inherits_the_documents_of_where_it_sits(
        self, scope: str, expected: list[str]
    ) -> None:
        assert hc.ancestors(_registries(), scope) == expected

    def test_reading_an_entity_brings_its_area_and_home_documents(self) -> None:
        async def scenario() -> Any:
            book = _book()
            await _write(book, "area:office", "office rules", 0)
            await _write(book, "home", "house rules", 0)
            return await hc.read(book, _registries, {"scope": "entity:light.desk"})

        result = run(scenario())
        assert result["document"] is None
        assert [r["scope"] for r in result["related"]] == ["area:office", "home"]
        assert result["related"][0]["content"] == "office rules"

    def test_a_write_target_matches_documents_of_its_broader_scopes(self) -> None:
        async def scenario() -> Any:
            book = _book()
            await _write(book, "floor:upstairs", "upstairs", 0)
            await _write(book, "topic:pv", "pv", 0)
            return await hc.match(
                book, _registries(), {"entity_ids": ["light.desk", "light.turn_on"]}
            )

        assert [s["scope"] for s in run(scenario())["scopes"]] == ["floor:upstairs"]

    def test_a_write_without_targets_is_covered_by_the_home_document(self) -> None:
        async def scenario() -> Any:
            book = _book()
            await _write(book, "home", "house rules", 0)
            return await hc.match(book, _registries(), {})

        assert [s["scope"] for s in run(scenario())["scopes"]] == ["home"]

    @pytest.mark.parametrize(
        "scope", ["Area:Kitchen", "room:kitchen", "topic:", "topic:a b", "homes"]
    )
    def test_malformed_scopes_are_rejected(self, scope: str) -> None:
        assert not hc.SCOPE_PATTERN.match(scope)


@pytest.fixture
def ws(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The registered WS handlers, backed by one in-memory store."""
    store = FakeStore()
    fake = _FakeWSApi()
    monkeypatch.setattr(wsapi, "websocket_api", fake)
    monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: _registries())
    monkeypatch.setattr(wsapi, "_home_context_store", lambda hass: store)
    monkeypatch.setattr(hc, "_now_iso", lambda: "2026-10-14T08:00:00+00:00")
    wsapi.async_register_commands(FakeHass())
    return fake


class TestWsCommands:
    def _call(self, ws: Any, hass: Any, conn: Any, **msg: Any) -> Any:
        ws.registered[msg["type"]](hass, conn, {"id": 1, **msg})
        return conn.results[1]

    @pytest.mark.parametrize("command", hc.COMMANDS)
    def test_non_admins_cannot_read_or_write(self, ws: Any, command: str) -> None:
        with pytest.raises(_Unauthorized):
            ws.registered[command](
                FakeHass(), _FakeConnection(is_admin=False), {"id": 1, "type": command}
            )

    def test_a_write_is_attributed_to_the_calling_user(self, ws: Any) -> None:
        hass = FakeHass()
        written = self._call(
            ws,
            hass,
            _FakeConnection(user_id="u-admin"),
            type=hc.WS_HOME_CONTEXT_WRITE,
            scope="area:office",
            content="desk lamp is on a smart plug",
            expected_revision=0,
        )
        read = self._call(
            ws,
            hass,
            _FakeConnection(),
            type=hc.WS_HOME_CONTEXT_GET,
            scope="entity:light.desk",
        )
        assert written["revision"] == 1
        assert read["related"][0]["user_id"] == "u-admin"

    def test_the_capability_is_advertised_with_the_commands(self) -> None:
        assert set(hc.CAPABILITIES) <= set(wsapi.CAPABILITIES)
