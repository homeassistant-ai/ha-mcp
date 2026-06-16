# ruff: noqa: ASYNC240
"""Unit tests for ``ha_mcp.backup_manager.BackupManager`` (#1288).

Covers throttle math, retention rotation, filename safety, schema-version
validation on restore, list/read/delete behavior, and the best-effort
error handling that the decorator relies on to never block writes.

These tests do not require a HA instance — they use lightweight stubs for
the client and handler fetch/restore coroutines.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from ha_mcp import backup_manager as bm
from ha_mcp.backup_manager import (
    SCHEMA_VERSION,
    BackupManager,
    DomainHandler,
    _safe_entity_id,
    get_backup_manager,
)

# ---------------------------------------------------------------- fixtures


@dataclass
class _StubSettings:
    enable_auto_backup: bool = True
    auto_backup_throttle_minutes: int = 0
    auto_backup_retain_per_entity: int = 5
    auto_backup_dir: str = ""


class _StubClient:
    """Bare-bones client object — handlers receive it but our test handlers
    don't dereference anything off it."""


def _mk_manager(tmp_path: Path, **settings_overrides: Any) -> BackupManager:
    settings = _StubSettings(auto_backup_dir=str(tmp_path), **settings_overrides)
    return BackupManager(settings, _StubClient())


def _mk_handler(
    domain: str = "automation",
    fetched: Any = None,
    *,
    restore_result: Any = "ok",
    raise_on_fetch: BaseException | None = None,
    raise_on_restore: BaseException | None = None,
) -> DomainHandler:
    async def fetch(_client: Any, _entity_id: str) -> Any:
        if raise_on_fetch is not None:
            raise raise_on_fetch
        return fetched

    async def restore(_client: Any, entity_id: str, config: Any) -> Any:
        if raise_on_restore is not None:
            raise raise_on_restore
        return {
            "entity_id": entity_id,
            "config": config,
            "ok": True,
            "result": restore_result,
        }

    return DomainHandler(domain=domain, fetch=fetch, restore=restore)


# ---------------------------------------------------------------- automation_backup_target


class TestAutomationBackupTarget:
    """Pins the #1404-aware backup-target resolution for automation.

    HA stores automations by the body's ``id`` field, not the URL's
    ``unique_id`` — so when caller passes both ``identifier`` and a
    ``config.id`` that differ, the backup MUST capture the entity at
    ``config.id`` (the actual victim of the write), not the entity at
    ``identifier`` (which HA's storage layer ignores).
    """

    def test_prefers_config_id_over_identifier(self) -> None:
        from ha_mcp.tools.auto_backup import automation_backup_target

        target = automation_backup_target(
            {"identifier": "automation.foo", "config": {"id": "BBB", "alias": "x"}}
        )
        # config.id wins: BBB is the actual storage target HA will overwrite.
        assert target == "BBB"

    def test_falls_back_to_identifier_entity_id_form_preserved(self) -> None:
        from ha_mcp.tools.auto_backup import automation_backup_target

        # Regression (#auto-backup-capture): the fallback path must NOT
        # strip the ``automation.`` prefix. The capture/restore fetch
        # resolves the target via _resolve_automation_id, which only does
        # the entity_id -> numeric unique_id state lookup when the prefix
        # is present. Stripping it to a bare slug made the resolver treat
        # the slug as a unique_id -> 404 -> snapshot silently skipped.
        target = automation_backup_target(
            {"identifier": "automation.foo", "config": {"alias": "x"}}
        )
        assert target == "automation.foo"

    def test_falls_back_to_identifier_bare_id_unchanged(self) -> None:
        from ha_mcp.tools.auto_backup import automation_backup_target

        # Bare-id identifiers (no ``automation.`` prefix) pass through
        # unchanged so callers that already supply the unique_id form
        # aren't surprised.
        target = automation_backup_target(
            {"identifier": "my_unique_id", "config": {"alias": "x"}}
        )
        assert target == "my_unique_id"

    def test_parses_config_as_json_string(self) -> None:
        from ha_mcp.tools.auto_backup import automation_backup_target

        target = automation_backup_target(
            {
                "identifier": "automation.foo",
                "config": '{"id": "BBB", "alias": "x"}',
            }
        )
        assert target == "BBB"

    def test_invalid_json_falls_back_to_identifier(self) -> None:
        from ha_mcp.tools.auto_backup import automation_backup_target

        # Falls through to the identifier fallback path, which preserves
        # the entity_id form (prefix kept so the fetch resolver can map it
        # to the numeric unique_id).
        target = automation_backup_target(
            {"identifier": "automation.foo", "config": "not-json"}
        )
        assert target == "automation.foo"

    def test_no_identifier_no_config_id_returns_empty(self) -> None:
        from ha_mcp.tools.auto_backup import automation_backup_target

        # Create path: no identifier, no config.id yet — nothing to back up.
        target = automation_backup_target({"config": {"alias": "x"}})
        assert target == ""

    def test_matched_id_and_identifier(self) -> None:
        from ha_mcp.tools.auto_backup import automation_backup_target

        # Same target — config.id wins but both point at the same entity.
        target = automation_backup_target(
            {"identifier": "automation.foo", "config": {"id": "automation.foo"}}
        )
        assert target == "automation.foo"


# ---------------------------------------------------- fetcher id resolution
#
# Regression tests for the silent-skip capture bugs: each fetch handler must
# resolve the id form a realistic caller passes so a pre-write snapshot is
# actually written (a fetch that returns None makes maybe_snapshot skip
# silently). These monkeypatch the module-level ``_ws_send`` so no real HA
# instance is needed.


class TestFetcherIdResolution:
    async def test_fetch_todo_item_matches_by_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The decorator passes "<entity_id>::<item>" where <item> is the
        # human-readable summary (the documented form). Matching only uid
        # silently skipped the snapshot; the fix matches uid OR summary.
        async def fake_ws(_client: Any, _msg: dict[str, Any]) -> Any:
            return {
                "response": {
                    "items": {
                        "todo.shopping": {
                            "items": [
                                {
                                    "uid": "abc-123",
                                    "summary": "Buy milk",
                                    "status": "needs_action",
                                }
                            ]
                        }
                    }
                }
            }

        monkeypatch.setattr(bm, "_ws_send", fake_ws)
        got = await bm._fetch_todo_item(_StubClient(), "todo.shopping::Buy milk")
        assert got is not None
        assert got["uid"] == "abc-123"
        assert got["todo_entity_id"] == "todo.shopping"

    async def test_fetch_todo_item_still_matches_by_uid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_ws(_client: Any, _msg: dict[str, Any]) -> Any:
            return {
                "response": {
                    "items": {"todo.x": {"items": [{"uid": "u-9", "summary": "Pay"}]}}
                }
            }

        monkeypatch.setattr(bm, "_ws_send", fake_ws)
        got = await bm._fetch_todo_item(_StubClient(), "todo.x::u-9")
        assert got is not None
        assert got["uid"] == "u-9"

    async def test_fetch_helper_resolves_renamed_via_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # After an entity_id rename the object_id no longer equals the
        # collection id; the fix falls back to the registry unique_id.
        async def fake_ws(_client: Any, msg: dict[str, Any]) -> Any:
            if msg.get("type") == "input_boolean/list":
                return [{"id": "original_slug", "name": "X"}]
            if msg.get("type") == "config/entity_registry/get":
                assert msg.get("entity_id") == "input_boolean.renamed_slug"
                return {"unique_id": "original_slug"}
            raise AssertionError(f"unexpected ws message: {msg}")

        monkeypatch.setattr(bm, "_ws_send", fake_ws)
        got = await bm._fetch_helper(
            _StubClient(), "input_boolean.renamed_slug", "input_boolean"
        )
        assert got is not None
        assert got["id"] == "original_slug"

    async def test_fetch_helper_direct_match_skips_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_ws(_client: Any, msg: dict[str, Any]) -> Any:
            if msg.get("type") == "input_boolean/list":
                return [{"id": "my_bool", "name": "X"}]
            raise AssertionError("registry fallback must not run on a direct hit")

        monkeypatch.setattr(bm, "_ws_send", fake_ws)
        got = await bm._fetch_helper(
            _StubClient(), "input_boolean.my_bool", "input_boolean"
        )
        assert got is not None
        assert got["id"] == "my_bool"

    async def test_fetch_helper_propagates_non_notfound_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Rename-fallback registry lookup: a transport/5xx error must
        # propagate (so maybe_snapshot logs a WARNING) rather than be
        # swallowed as a not-found and silently skip the snapshot.
        async def fake_ws(_client: Any, msg: dict[str, Any]) -> Any:
            if msg.get("type") == "input_boolean/list":
                return [{"id": "original_slug", "name": "X"}]
            raise bm.HomeAssistantError("Internal Server Error")

        monkeypatch.setattr(bm, "_ws_send", fake_ws)
        with pytest.raises(bm.HomeAssistantError):
            await bm._fetch_helper(
                _StubClient(), "input_boolean.renamed_slug", "input_boolean"
            )

    async def test_fetch_device_returns_registry_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_ws(_client: Any, msg: dict[str, Any]) -> Any:
            assert msg.get("type") == "config/device_registry/list"
            return [
                {
                    "id": "dev-1",
                    "name_by_user": "Hub",
                    "area_id": "lr",
                    "disabled_by": None,
                    "labels": ["x"],
                },
                {"id": "dev-2"},
            ]

        monkeypatch.setattr(bm, "_ws_send", fake_ws)
        got = await bm._fetch_device(_StubClient(), "dev-1")
        assert got is not None
        assert got["name_by_user"] == "Hub"

    async def test_restore_device_sends_update_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: dict[str, Any] = {}

        async def fake_ws(_client: Any, msg: dict[str, Any]) -> Any:
            sent.update(msg)
            return {"success": True}

        monkeypatch.setattr(bm, "_ws_send", fake_ws)
        await bm._restore_device(
            _StubClient(),
            "dev-1",
            {
                "name_by_user": "Hub",
                "area_id": "lr",
                "disabled_by": None,
                "labels": ["x"],
            },
        )
        assert sent["type"] == "config/device_registry/update"
        assert sent["device_id"] == "dev-1"
        assert sent["name_by_user"] == "Hub"
        assert sent["labels"] == ["x"]

    async def test_fetch_dashboard_resolves_internal_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The internal (underscored) id must be pre-resolved to the canonical
        # url_path before the lovelace/config fetch, else it 404s and the
        # snapshot is silently skipped.
        import ha_mcp.tools.tools_config_dashboards as dash

        seen: dict[str, Any] = {}

        async def fake_resolve(_client: Any, identifier: str) -> Any:
            return {"url_path": "my-dash", "id": identifier}, None

        async def fake_get_internal(_client: Any, url_path: str | None) -> Any:
            seen["url_path"] = url_path
            return {"views": []}, "hash"

        monkeypatch.setattr(dash, "_resolve_dashboard", fake_resolve)
        monkeypatch.setattr(dash, "_get_dashboard_config_internal", fake_get_internal)
        got = await bm._fetch_dashboard(_StubClient(), "my_dash")
        assert got == {"views": []}
        # fetched via the canonical url_path, not the raw internal id
        assert seen["url_path"] == "my-dash"

    async def test_fetch_dashboard_unknown_config_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # HA's "Unknown config specified" for an unresolved url_path (also the
        # brand-new-dashboard create path) must map to None ("nothing to back
        # up"), not propagate as a hard failure.
        from fastmcp.exceptions import ToolError

        import ha_mcp.tools.tools_config_dashboards as dash

        async def fake_resolve(_client: Any, _identifier: str) -> Any:
            return None, None  # no registry match -> fall through with raw id

        async def fake_get_internal(_client: Any, _url_path: str | None) -> Any:
            raise ToolError("Dashboard fetch failed: Unknown config specified: x")

        monkeypatch.setattr(dash, "_resolve_dashboard", fake_resolve)
        monkeypatch.setattr(dash, "_get_dashboard_config_internal", fake_get_internal)
        assert await bm._fetch_dashboard(_StubClient(), "x_dash") is None

    async def test_fetch_dashboard_propagates_other_toolerror(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A non-not-found failure must propagate (so maybe_snapshot logs a
        # WARNING) rather than be misclassified as "nothing to back up".
        from fastmcp.exceptions import ToolError

        import ha_mcp.tools.tools_config_dashboards as dash

        async def fake_resolve(_client: Any, _identifier: str) -> Any:
            return None, None

        async def fake_get_internal(_client: Any, _url_path: str | None) -> Any:
            raise ToolError("Dashboard fetch failed: 500 Internal Server Error")

        monkeypatch.setattr(dash, "_resolve_dashboard", fake_resolve)
        monkeypatch.setattr(dash, "_get_dashboard_config_internal", fake_get_internal)
        with pytest.raises(ToolError):
            await bm._fetch_dashboard(_StubClient(), "x_dash")

    async def test_fetch_calendar_event_matches_uid(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Hard guard for the calendar lane (the e2e is skippable): the
        # pre-delete fetch must find the event by uid and tag the calendar.
        async def fake_ws(_client: Any, _msg: dict[str, Any]) -> Any:
            return {
                "response": {
                    "events": {
                        "calendar.fam": {
                            "events": [{"uid": "evt-1", "summary": "Dinner"}]
                        }
                    }
                }
            }

        monkeypatch.setattr(bm, "_ws_send", fake_ws)
        got = await bm._fetch_calendar_event(_StubClient(), "calendar.fam::evt-1")
        assert got is not None
        assert got["uid"] == "evt-1"
        assert got["calendar_entity_id"] == "calendar.fam"

    async def test_fetch_helper_registry_without_unique_id_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Rename fallback tail: registry entry exists but carries no
        # resolvable unique_id (or it matches nothing) -> genuine not-found.
        async def fake_ws(_client: Any, msg: dict[str, Any]) -> Any:
            if msg.get("type") == "input_boolean/list":
                return [{"id": "original_slug", "name": "X"}]
            if msg.get("type") == "config/entity_registry/get":
                return {}  # no unique_id
            raise AssertionError(f"unexpected ws message: {msg}")

        monkeypatch.setattr(bm, "_ws_send", fake_ws)
        got = await bm._fetch_helper(
            _StubClient(), "input_boolean.renamed_slug", "input_boolean"
        )
        assert got is None

    async def test_fetch_automation_preserves_entity_id_form(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Headline bug at the FETCH layer: the entity_id form must reach
        # client.get_automation_config UNSTRIPPED so its resolver maps it to
        # the numeric unique_id (a bare slug would 404 -> silent skip).
        import ha_mcp.tools.tools_config_automations as autos

        seen: dict[str, Any] = {}

        class _FakeClient:
            async def get_automation_config(self, identifier: str) -> Any:
                seen["id"] = identifier
                return {"id": "1781613420568", "alias": "X"}

        monkeypatch.setattr(autos, "_normalize_config_for_roundtrip", lambda c: c)
        got = await bm._fetch_automation(_FakeClient(), "automation.kitchen_lights")
        assert seen["id"] == "automation.kitchen_lights"
        assert got == {"id": "1781613420568", "alias": "X"}

    async def test_fetch_automation_returns_none_on_404(self) -> None:
        from ha_mcp.client.rest_client import HomeAssistantAPIError

        class _FakeClient:
            async def get_automation_config(self, identifier: str) -> Any:
                raise HomeAssistantAPIError("Automation not found", status_code=404)

        assert await bm._fetch_automation(_FakeClient(), "automation.gone") is None


# --------------------------------------------- with_auto_backup decorator wiring


class TestWithAutoBackupDecorator:
    """The @with_auto_backup wiring the new destructive tools rely on."""

    async def test_id_param_fires_maybe_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ha_mcp.tools.auto_backup import with_auto_backup

        calls: list[tuple[str, str, str | None]] = []

        class _FakeMgr:
            async def maybe_snapshot(
                self, domain: str, entity_id: str, *, tool_name: str | None = None
            ) -> None:
                calls.append((domain, entity_id, tool_name))

        class _Settings:
            enable_auto_backup = True

        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_global_settings", lambda: _Settings()
        )
        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_backup_manager", lambda _c, _s: _FakeMgr()
        )

        ran: list[str] = []

        @with_auto_backup(domain="device", id_param="device_id", client=object())
        async def fake_tool(*, device_id: str) -> str:
            ran.append(device_id)
            return "ok"

        assert await fake_tool(device_id="dev-1") == "ok"
        assert ran == ["dev-1"]  # wrapped write still runs
        assert calls == [("device", "dev-1", "fake_tool")]

    async def test_capture_failure_does_not_block_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ha_mcp.tools.auto_backup import with_auto_backup

        class _FakeMgr:
            async def maybe_snapshot(self, *a: Any, **k: Any) -> None:
                raise bm.HomeAssistantError("transient capture failure")

        class _Settings:
            enable_auto_backup = True

        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_global_settings", lambda: _Settings()
        )
        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_backup_manager", lambda _c, _s: _FakeMgr()
        )

        ran: list[str] = []

        @with_auto_backup(domain="device", id_param="device_id", client=object())
        async def fake_tool(*, device_id: str) -> str:
            ran.append(device_id)
            return "ok"

        # best-effort contract: a capture error must NOT block the write
        assert await fake_tool(device_id="dev-1") == "ok"
        assert ran == ["dev-1"]


def test_destructive_tools_carry_auto_backup_decorator() -> None:
    """Source guard: the three destructive tools this PR wired must keep
    their @with_auto_backup decorator — a deleted line would silently stop
    capturing without failing any behavioral test."""
    from pathlib import Path

    tools_dir = Path(__file__).resolve().parents[3] / "src" / "ha_mcp" / "tools"
    checks = [
        ("tools_entities.py", "ha_remove_entity", "entity", "entity_id"),
        ("tools_registry.py", "ha_set_device", "device", "device_id"),
        ("tools_registry.py", "ha_remove_device", "device", "device_id"),
    ]
    for fname, func, domain, id_param in checks:
        src = (tools_dir / fname).read_text(encoding="utf-8")
        idx = src.index(f"async def {func}(")
        head = src[max(0, idx - 600) : idx]  # the decorator block above the def
        assert "with_auto_backup(" in head, f"{func} lost @with_auto_backup"
        assert f'domain="{domain}"' in head, f"{func} wrong/missing backup domain"
        assert f'id_param="{id_param}"' in head, f"{func} wrong/missing id_param"


# ---------------------------------------------------------------- filenames


class TestFilenameSafety:
    def test_path_separators_replaced(self) -> None:
        assert "/" not in _safe_entity_id("a/b")
        assert "\\" not in _safe_entity_id("a\\b")

    def test_unicode_replaced(self) -> None:
        assert _safe_entity_id("foo🎉bar") == "foo_bar"

    def test_leading_dot_stripped(self) -> None:
        assert not _safe_entity_id("...env").startswith(".")

    def test_empty_falls_back_to_underscore(self) -> None:
        assert _safe_entity_id("") == "_"
        assert _safe_entity_id("....") == "_"

    def test_keeps_safe_chars(self) -> None:
        assert _safe_entity_id("entity.foo_bar-1") == "entity.foo_bar-1"


# ---------------------------------------------------------------- capture


class TestCapture:
    async def test_disabled_means_no_snapshot(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path, enable_auto_backup=False)
        mgr.register(_mk_handler(fetched={"x": 1}))
        path = await mgr.maybe_snapshot("automation", "kitchen")
        assert path is None
        assert not any(tmp_path.iterdir())

    async def test_no_handler_skips(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        path = await mgr.maybe_snapshot("unknown_domain", "x")
        assert path is None

    async def test_empty_entity_id_skips(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(fetched={"x": 1}))
        path = await mgr.maybe_snapshot("automation", "")
        assert path is None

    async def test_fetch_returning_none_skips(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(fetched=None))
        path = await mgr.maybe_snapshot("automation", "missing")
        assert path is None
        assert not any(tmp_path.iterdir())

    async def test_fetch_transient_exception_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        mgr = _mk_manager(tmp_path)
        # Transient/expected exceptions (HA / network / FS / yaml errors) are
        # swallowed — capture is best-effort, the wrapped write must still run.
        mgr.register(_mk_handler(raise_on_fetch=OSError("disk gone")))
        path = await mgr.maybe_snapshot("automation", "x", tool_name="t")
        assert path is None

    async def test_fetch_programming_error_propagates(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        # Programming errors (AttributeError/TypeError/KeyError/RuntimeError)
        # propagate so they surface as a real test failure rather than being
        # silently masked by the best-effort capture path.
        mgr.register(_mk_handler(raise_on_fetch=AttributeError("typo")))
        with pytest.raises(AttributeError):
            await mgr.maybe_snapshot("automation", "x", tool_name="t")

    async def test_successful_capture_writes_file(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(fetched={"alias": "Kitchen", "trigger": []}))
        path = await mgr.maybe_snapshot(
            "automation", "kitchen_lights", tool_name="ha_config_set_automation"
        )
        assert path is not None
        assert path.exists()
        data = yaml.safe_load(path.read_text())
        assert data["schema_version"] == SCHEMA_VERSION
        assert data["domain"] == "automation"
        assert data["entity_id"] == "kitchen_lights"
        assert data["tool"] == "ha_config_set_automation"
        assert data["config"] == {"alias": "Kitchen", "trigger": []}

    async def test_throttle_blocks_second_capture(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path, auto_backup_throttle_minutes=10)
        mgr.register(_mk_handler(fetched={"v": 1}))
        first = await mgr.maybe_snapshot("automation", "x")
        # Need a sleep > 1s so the second snapshot's filename timestamp
        # would differ if it were written — guarantees the "throttled"
        # assertion is about throttle, not just filename collision.
        await asyncio.sleep(1.1)
        second = await mgr.maybe_snapshot("automation", "x")
        assert first is not None
        assert second is None
        # Only one file landed.
        assert len(list(tmp_path.glob("automation.x.*.yaml"))) == 1

    async def test_throttle_zero_captures_every_time(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path, auto_backup_throttle_minutes=0)
        mgr.register(_mk_handler(fetched={"v": 1}))
        first = await mgr.maybe_snapshot("automation", "x")
        await asyncio.sleep(1.1)
        second = await mgr.maybe_snapshot("automation", "x")
        assert first is not None
        assert second is not None
        assert first != second

    async def test_throttle_is_per_entity(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path, auto_backup_throttle_minutes=10)
        mgr.register(_mk_handler(fetched={"v": 1}))
        p1 = await mgr.maybe_snapshot("automation", "alpha")
        p2 = await mgr.maybe_snapshot("automation", "beta")
        assert p1 is not None
        assert p2 is not None


# ---------------------------------------------------------------- retention


class TestRetention:
    async def test_rotation_removes_oldest(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path, auto_backup_retain_per_entity=3)
        mgr.register(_mk_handler(fetched={"v": 1}))
        # Force distinct timestamps with sleeps.
        for _ in range(5):
            await mgr.maybe_snapshot("automation", "x")
            await asyncio.sleep(1.1)
        remaining = sorted(tmp_path.glob("automation.x.*.yaml"))
        assert len(remaining) == 3

    async def test_rotation_does_not_touch_other_entities(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path, auto_backup_retain_per_entity=2)
        mgr.register(_mk_handler(fetched={"v": 1}))
        for _ in range(3):
            await mgr.maybe_snapshot("automation", "alpha")
            await asyncio.sleep(1.1)
        await mgr.maybe_snapshot("automation", "beta")
        # alpha rotated to 2, beta kept its single file.
        assert len(list(tmp_path.glob("automation.alpha.*.yaml"))) == 2
        assert len(list(tmp_path.glob("automation.beta.*.yaml"))) == 1


# ---------------------------------------------------------------- list/read/delete


class TestListReadDelete:
    async def test_list_filters_by_domain(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler("automation", fetched={"a": 1}))
        mgr.register(_mk_handler("script", fetched={"s": 1}))
        await mgr.maybe_snapshot("automation", "a")
        await asyncio.sleep(1.1)
        await mgr.maybe_snapshot("script", "s")
        all_entries = mgr.list_snapshots()
        autos = mgr.list_snapshots(domain="automation")
        scripts = mgr.list_snapshots(domain="script")
        assert len(all_entries) == 2
        assert len(autos) == 1
        assert len(scripts) == 1
        assert autos[0]["domain"] == "automation"

    async def test_list_filters_by_entity_id(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(fetched={"v": 1}))
        await mgr.maybe_snapshot("automation", "alpha")
        await asyncio.sleep(1.1)
        await mgr.maybe_snapshot("automation", "beta")
        only_alpha = mgr.list_snapshots(entity_id="alpha")
        assert len(only_alpha) == 1
        assert only_alpha[0]["entity_id"] == "alpha"

    async def test_read_snapshot_returns_full_payload(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(fetched={"alias": "x"}))
        path = await mgr.maybe_snapshot("automation", "x", tool_name="t")
        assert path is not None
        data = mgr.read_snapshot(path.name)
        assert data["domain"] == "automation"
        assert data["entity_id"] == "x"
        assert data["config"] == {"alias": "x"}

    def test_read_snapshot_rejects_path_traversal(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        with pytest.raises(ValueError):
            mgr.read_snapshot("../../etc/passwd")
        with pytest.raises(ValueError):
            mgr.read_snapshot("subdir/file.yaml")

    def test_read_snapshot_rejects_unknown_schema(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        bad = tmp_path / "automation.x.20260521_120000.yaml"
        bad.write_text(yaml.safe_dump({"schema_version": 999, "config": {}}))
        with pytest.raises(ValueError, match="schema_version"):
            mgr.read_snapshot(bad.name)

    async def test_delete_snapshot_removes_file(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(fetched={"v": 1}))
        path = await mgr.maybe_snapshot("automation", "x")
        assert path is not None
        mgr.delete_snapshot(path.name)
        assert not path.exists()

    async def test_delete_bulk_by_age(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(fetched={"v": 1}))
        # Write a snapshot, then backdate its mtime to look 30 days old.
        path = await mgr.maybe_snapshot("automation", "x")
        assert path is not None
        old = time.time() - (40 * 86400)
        import os as _os

        _os.utime(path, (old, old))
        await asyncio.sleep(1.1)
        recent = await mgr.maybe_snapshot("automation", "x")
        assert recent is not None
        bulk = mgr.delete_bulk(older_than_days=30)
        assert path.name in bulk["deleted"]
        assert recent.name not in bulk["deleted"]
        assert bulk["failed"] == []

    def test_delete_bulk_requires_filter(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        # Bulk delete with no filter still returns an empty result rather
        # than erroring; the route layer enforces "at least one filter".
        # The manager is permissive so MCP tool callers can pass
        # already-validated filters through.
        assert mgr.delete_bulk() == {"deleted": [], "failed": []}


# ---------------------------------------------------------------- restore


class TestRestore:
    async def test_restore_calls_handler_and_returns_metadata(
        self, tmp_path: Path
    ) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(fetched={"alias": "x"}))
        path = await mgr.maybe_snapshot("automation", "x")
        assert path is not None
        result = await mgr.restore_snapshot(path.name)
        assert result["domain"] == "automation"
        assert result["entity_id"] == "x"
        # Restore takes a safety backup of the current state (which itself
        # is the same one our handler returns) → safety_backup is a
        # filename, not None.
        assert result["safety_backup"] is not None
        assert result["restored_from"] == path.name
        assert result["result"]["ok"] is True

    async def test_restore_propagates_value_error_on_unknown_domain(
        self, tmp_path: Path
    ) -> None:
        mgr = _mk_manager(tmp_path)
        # Write a snapshot file with a domain that has no handler.
        bogus = tmp_path / "alien.x.20260521_120000.yaml"
        bogus.write_text(
            yaml.safe_dump(
                {
                    "schema_version": SCHEMA_VERSION,
                    "domain": "alien",
                    "entity_id": "x",
                    "config": {"v": 1},
                }
            )
        )
        with pytest.raises(LookupError):
            await mgr.restore_snapshot(bogus.name)

    async def test_restore_safety_backup_disabled(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path, enable_auto_backup=False)
        # Snapshot manually (since capture is disabled).
        path = tmp_path / "automation.x.20260521_120000.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": SCHEMA_VERSION,
                    "domain": "automation",
                    "entity_id": "x",
                    "config": {"alias": "x"},
                }
            )
        )
        mgr.register(_mk_handler(fetched={"alias": "x"}))
        result = await mgr.restore_snapshot(path.name)
        # Capture is disabled → safety backup wasn't taken.
        assert result["safety_backup"] is None


# ---------------------------------------------------------------- factory


class TestFactory:
    def test_get_backup_manager_caches_on_client(self, tmp_path: Path) -> None:
        settings = _StubSettings(auto_backup_dir=str(tmp_path))
        client = _StubClient()
        mgr1 = get_backup_manager(client, settings)
        mgr2 = get_backup_manager(client, settings)
        assert mgr1 is mgr2

    def test_get_backup_manager_registers_default_handlers(
        self, tmp_path: Path
    ) -> None:
        settings = _StubSettings(auto_backup_dir=str(tmp_path))
        mgr = get_backup_manager(_StubClient(), settings)
        # Sanity: a handful of the expected domains exist.
        for d in [
            "automation",
            "script",
            "scene",
            "dashboard",
            "label",
            "category",
            "group",
            "zone",
            "area_or_floor",
            "todo_item",
            "calendar_event",
            "entity",
            "device",
            "integration",
            "helper_input_boolean",
            "helper_timer",
        ]:
            assert mgr.handler_for(d) is not None, f"missing handler: {d}"

    def test_helper_flow_types_have_no_handler(self, tmp_path: Path) -> None:
        # Flow-helper types (template, group, utility_meter, ...) live in
        # config entries with a separate update API — registering them
        # would produce unrestorable snapshots (entity-state stubs).
        # They must NOT be registered as backup domains.
        settings = _StubSettings(auto_backup_dir=str(tmp_path))
        mgr = get_backup_manager(_StubClient(), settings)
        for d in [
            "helper_template",
            "helper_group",
            "helper_utility_meter",
            "helper_threshold",
            "helper_derivative",
        ]:
            assert mgr.handler_for(d) is None, (
                f"flow-helper domain {d!r} should NOT be registered (unrestorable)"
            )


# ---------------------------------------------------------------- bookkeeping


class TestTrackerPrune:
    def test_prune_triggers_above_cap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Patch the cap to a small number so we can exercise the prune
        # path without needing 10_000 entries.
        monkeypatch.setattr(bm, "_TRACKER_SOFT_CAP", 5)
        monkeypatch.setattr(bm, "_TRACKER_PRUNE_BATCH", 2)
        mgr = BackupManager(_StubSettings(auto_backup_dir=str(tmp_path)), _StubClient())
        # Fill the tracker past the patched cap.
        for i in range(7):
            mgr._last_snapshot[f"automation:e{i}"] = float(i)
        mgr._maybe_prune_trackers()
        # 7 - 2 = 5 remaining; the two smallest timestamps drop out.
        assert len(mgr._last_snapshot) == 5
        assert "automation:e0" not in mgr._last_snapshot
        assert "automation:e1" not in mgr._last_snapshot
        assert "automation:e6" in mgr._last_snapshot

    def test_prune_noop_under_cap(self, tmp_path: Path) -> None:
        mgr = BackupManager(_StubSettings(auto_backup_dir=str(tmp_path)), _StubClient())
        mgr._last_snapshot["automation:x"] = 1.0
        mgr._maybe_prune_trackers()
        assert mgr._last_snapshot == {"automation:x": 1.0}


class TestConcurrentCapture:
    async def test_concurrent_same_key_serializes(self, tmp_path: Path) -> None:
        # Two captures racing on the same (domain, entity_id) must
        # serialize via the per-key lock. With throttle_minutes=10, the
        # second call inside the same window MUST be skipped — without
        # the lock both could see "no prior snapshot" and write twice.
        mgr = _mk_manager(tmp_path, auto_backup_throttle_minutes=10)
        mgr.register(_mk_handler(fetched={"v": 1}))
        results = await asyncio.gather(
            mgr.maybe_snapshot("automation", "x"),
            mgr.maybe_snapshot("automation", "x"),
        )
        non_null = [r for r in results if r is not None]
        assert len(non_null) == 1
        assert len(list(tmp_path.glob("automation.x.*.yaml"))) == 1


class TestEnabledRespectsDirError:
    def test_enabled_false_when_dir_init_failed(self, tmp_path: Path) -> None:
        # Simulate a backup dir that can't be created. ``enabled`` must
        # report False so listing/status surfaces don't lie about
        # backup health.
        mgr = BackupManager(
            _StubSettings(enable_auto_backup=True, auto_backup_dir=str(tmp_path)),
            _StubClient(),
        )
        mgr._init_dir_error = "OSError: read-only filesystem"
        assert mgr.enabled is False
        assert mgr.init_dir_error == "OSError: read-only filesystem"


class TestForceSnapshot:
    """Pins the ``force=True`` path on ``maybe_snapshot`` — exercised by
    the ``ha_manage_backup(scope='edits', action='create')`` on-demand
    action. ``force`` must bypass the ``enable_auto_backup`` toggle and
    the per-entity throttle window, but the handler-missing and
    fetch-returned-None skips still apply (force can't conjure data
    that doesn't exist).
    """

    async def test_force_bypasses_disabled_toggle(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path, enable_auto_backup=False)
        mgr.register(_mk_handler(fetched={"alias": "x"}))
        # Without force, disabled → None.
        path = await mgr.maybe_snapshot("automation", "foo")
        assert path is None
        # With force, writes the snapshot.
        path = await mgr.maybe_snapshot("automation", "foo", force=True)
        assert path is not None
        assert path.exists()

    async def test_force_bypasses_throttle_window(self, tmp_path: Path) -> None:
        # Throttle=60s; first capture lands, second within window normally
        # blocks — force=True must override.
        mgr = _mk_manager(
            tmp_path, enable_auto_backup=True, auto_backup_throttle_minutes=1
        )
        mgr.register(_mk_handler(fetched={"alias": "x"}))
        first = await mgr.maybe_snapshot("automation", "foo")
        assert first is not None
        # Sleep less than the throttle window — without force, returns None.
        second = await mgr.maybe_snapshot("automation", "foo")
        assert second is None
        # With force, captures again despite the window. (Both calls may
        # land in the same wall-clock second and overwrite the same
        # filename — what matters here is that ``maybe_snapshot``
        # returned a Path rather than the throttle-skip None.)
        third = await mgr.maybe_snapshot("automation", "foo", force=True)
        assert third is not None

    async def test_force_still_skips_when_handler_missing(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        # No handler registered for "automation".
        path = await mgr.maybe_snapshot("automation", "foo", force=True)
        assert path is None

    async def test_force_still_skips_when_fetch_returns_none(
        self, tmp_path: Path
    ) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(fetched=None))
        path = await mgr.maybe_snapshot("automation", "foo", force=True)
        assert path is None

    async def test_force_still_respects_init_dir_error(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(fetched={"alias": "x"}))
        mgr._init_dir_error = "OSError: read-only filesystem"
        # Even with force, an unreachable backup dir can't accept writes.
        path = await mgr.maybe_snapshot("automation", "foo", force=True)
        assert path is None


class TestSupportedDomains:
    def test_returns_sorted_registered_domains(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(domain="zone"))
        mgr.register(_mk_handler(domain="automation"))
        mgr.register(_mk_handler(domain="label"))
        # Sorted output for stable user-facing error messages.
        assert mgr.supported_domains() == ["automation", "label", "zone"]

    def test_empty_when_no_handlers(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        assert mgr.supported_domains() == []
