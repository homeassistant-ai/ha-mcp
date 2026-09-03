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
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from fastmcp.exceptions import ToolError

from ha_mcp import backup_manager as bm
from ha_mcp.backup_manager import (
    _MAX_PATCH_OPS,
    SCHEMA_VERSION,
    BackupManager,
    DomainHandler,
    _build_text_diff_response,
    _compute_json_patch,
    _entity_id_aliases,
    _fetch_calendar_event,
    _fetch_todo_item,
    _pointer_segment,
    _require_dict,
    _require_list,
    _safe_entity_id,
    _summarize_patch_counts,
    get_backup_manager,
)
from ha_mcp.client.rest_client import HomeAssistantError
from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tools.auto_backup import with_auto_backup

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

    async def test_fetch_dashboard_without_stored_config_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A metadata-only dashboard has nothing to snapshot before first save."""
        import ha_mcp.tools.tools_config_dashboards as dash

        async def fake_resolve(_client: Any, identifier: str) -> Any:
            return {"url_path": identifier, "id": "no_config"}, None

        async def no_component_config(_client: Any, _url_path: str) -> None:
            return None

        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": "Command failed: No config found.",
                "error_code": "config_not_found",
            }
        )
        monkeypatch.setattr(dash, "_resolve_dashboard", fake_resolve)
        monkeypatch.setattr(dash, "_component_dashboard_config", no_component_config)

        assert await bm._fetch_dashboard(client, "no-config") is None
        client.send_websocket_message.assert_awaited_once_with(
            {"type": "lovelace/config", "force": True, "url_path": "no-config"}
        )

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
                self,
                domain: str,
                entity_id: str,
                *,
                tool_name: str | None = None,
                mandatory: bool = False,
            ) -> None:
                calls.append((domain, entity_id, tool_name))

        class _Settings:
            enable_auto_backup = True

        monkeypatch.setattr("ha_mcp.tools.auto_backup.get_global_settings", _Settings)
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

        monkeypatch.setattr("ha_mcp.tools.auto_backup.get_global_settings", _Settings)
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
    # (fname, func, domain, id_param-or-None, mandatory). The file/YAML writes
    # (#1579) are mandatory; ha_config_set_yaml computes its key via id_fn so it
    # has no id_param to assert.
    checks = [
        ("tools_entities.py", "ha_remove_entity", "entity", "entity_id", False),
        ("tools_registry.py", "ha_set_device", "device", "device_id", False),
        ("tools_registry.py", "ha_remove_device", "device", "device_id", False),
        ("tools_filesystem.py", "ha_write_file", "file", "path", True),
        ("tools_filesystem.py", "ha_delete_file", "file", "path", True),
        ("tools_yaml_config.py", "ha_config_set_yaml", "yaml", None, True),
    ]
    for fname, func, domain, id_param, mandatory in checks:
        src = (tools_dir / fname).read_text(encoding="utf-8")
        idx = src.index(f"async def {func}(")
        head = src[max(0, idx - 600) : idx]  # the decorator block above the def
        assert "with_auto_backup(" in head, f"{func} lost @with_auto_backup"
        assert f'domain="{domain}"' in head, f"{func} wrong/missing backup domain"
        if id_param is not None:
            assert f'id_param="{id_param}"' in head, f"{func} wrong/missing id_param"
        if mandatory:
            assert "mandatory=True" in head, f"{func} lost mandatory=True"


def test_ha_manage_blueprints_carries_auto_backup_decorator() -> None:
    """Source guard for the computed-key wiring (#2329).

    Deleting a blueprint unlinks a file core will not rebuild and saving over
    one replaces it, so the pre-write snapshot is the only way back. The
    behavioral tests below drive a stand-in callable, so only this guard notices
    the decorator (or its domain/skip lambdas) disappearing from the real tool.
    """
    from pathlib import Path

    tools_dir = Path(__file__).resolve().parents[3] / "src" / "ha_mcp" / "tools"
    src = (tools_dir / "tools_blueprints.py").read_text(encoding="utf-8")
    idx = src.index("async def ha_manage_blueprints(")
    head = src[max(0, idx - 1200) : idx]  # the decorator block above the def
    assert "with_auto_backup(" in head, "ha_manage_blueprints lost @with_auto_backup"
    assert "domain_fn=" in head, "ha_manage_blueprints lost its blueprint domain_fn"
    assert "blueprint_{kw.get(" in head, "the backup domain no longer follows `domain`"
    assert "id_fn=" in head, "ha_manage_blueprints wrong/missing id_fn"
    assert "blueprint_snapshot_target" in head, (
        "the snapshot key no longer normalises the path the way blueprint/save "
        "does - an overwriting save of 'user/motion' would replace "
        "user/motion.yaml with nothing captured"
    )
    assert 'kw.get("action") not in ("delete", "save")' in head, (
        "ha_manage_blueprints lost the delete/save-only skip_fn - either every "
        "action would attempt a snapshot, or an overwriting save would destroy "
        "the previous file with nothing captured"
    )
    assert 'not kw.get("confirm")' in head, (
        "ha_manage_blueprints lost the confirm half of its skip_fn - an "
        "unconfirmed delete changes nothing, but would again pay a component "
        "file read and an outbound source_url re-fetch before being refused"
    )


# ---------------------------------------------------------------- filenames


class TestFilenameSafety:
    def test_path_separators_replaced(self) -> None:
        assert "/" not in _safe_entity_id("a/b")
        assert "\\" not in _safe_entity_id("a\\b")

    def test_unicode_replaced(self) -> None:
        # Sanitising is lossy, so the result also carries a digest of the
        # original; what this pins is that no unsafe character survives.
        safe = _safe_entity_id("foo🎉bar")
        assert safe.startswith("foo_bar")
        assert "🎉" not in safe

    def test_leading_dot_stripped(self) -> None:
        assert not _safe_entity_id("...env").startswith(".")

    def test_empty_falls_back_to_underscore(self) -> None:
        assert _safe_entity_id("") == "_"
        assert _safe_entity_id("....") == "_"

    def test_keeps_safe_chars(self) -> None:
        assert _safe_entity_id("entity.foo_bar-1") == "entity.foo_bar-1"

    def test_sanitised_ids_do_not_collide(self) -> None:
        """Blueprint domains key on paths, where sanitising is lossy (#2329).

        ``user/motion.yaml`` and ``user_motion.yaml`` both clean to
        ``user_motion.yaml``. Sharing one snapshot namespace means captures in
        the same second overwrite each other and rotation counts both
        histories as one, so it can delete the only restore point for a
        blueprint that was never written.
        """
        assert _safe_entity_id("user/motion.yaml") != _safe_entity_id(
            "user_motion.yaml"
        )
        assert _safe_entity_id("a/b") != _safe_entity_id("a_b")

    def test_the_same_id_always_sanitises_the_same_way(self) -> None:
        """Rotation and listing glob on this, so it has to be stable."""
        assert _safe_entity_id("user/motion.yaml") == _safe_entity_id(
            "user/motion.yaml"
        )

    def test_snapshots_written_before_the_digest_stay_discoverable(self) -> None:
        """Rotation and entity filtering must still find the older names.

        Adding the digest changed the filename for path-shaped ids, so files
        captured under the bare sanitised name would otherwise stop being
        counted: never pruned, and absent from an entity-filtered listing.
        """
        aliases = _entity_id_aliases("user/motion.yaml")

        assert _safe_entity_id("user/motion.yaml") in aliases
        assert "user_motion.yaml" in aliases

    def test_an_already_safe_id_has_exactly_one_alias(self) -> None:
        """Entity ids never had a second spelling, so nothing is widened."""
        assert _entity_id_aliases("automation.morning") == ["automation.morning"]

    def test_already_safe_ids_are_returned_unchanged(self) -> None:
        """Entity ids must keep their existing filenames, so snapshots taken
        before this change stay discoverable."""
        for entity_id in ("automation.morning", "script.bedtime", "light.hall-1"):
            assert _safe_entity_id(entity_id) == entity_id


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

    async def test_two_captures_in_one_second_keep_both_states(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The throttle defaults to 0 and the filename timestamp is a second.

        A save then a delete of one blueprint, or two rapid edits of one
        automation, land inside a second; with one name for both the atomic
        replace discarded the earlier pre-write state (CodeRabbit, PR #2356).
        The second capture takes a suffixed name that still sorts newest-last
        and parses to the same timestamp.
        """
        monkeypatch.setattr(bm, "_now_ts", lambda: "20260903_120000")
        versions = iter([{"v": 1}, {"v": 2}, {"v": 3}])

        async def fetch(_client: Any, _entity_id: str) -> Any:
            return next(versions)

        async def restore(_client: Any, _entity_id: str, _config: Any) -> Any:
            return "ok"

        mgr = _mk_manager(tmp_path)
        mgr.register(DomainHandler(domain="automation", fetch=fetch, restore=restore))

        first = await mgr.maybe_snapshot("automation", "x")
        second = await mgr.maybe_snapshot("automation", "x")
        third = await mgr.maybe_snapshot("automation", "x")

        assert first is not None and second is not None and third is not None
        assert len({first.name, second.name, third.name}) == 3
        assert mgr.read_snapshot(first.name)["config"] == {"v": 1}
        assert mgr.read_snapshot(second.name)["config"] == {"v": 2}
        assert mgr.read_snapshot(third.name)["config"] == {"v": 3}
        # Newest first, and every name reports the second it was taken in.
        listed = mgr.list_snapshots(entity_id="x")
        assert [m["name"] for m in listed] == [third.name, second.name, first.name]
        assert {m["timestamp"] for m in listed} == {"20260903_120000"}

    async def test_same_second_suffix_counts_toward_rotation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Suffixed names are ordinary snapshots: rotation prunes the oldest of
        them and the entity's own retention cap still holds."""
        monkeypatch.setattr(bm, "_now_ts", lambda: "20260903_120000")
        mgr = _mk_manager(tmp_path, auto_backup_retain_per_entity=2)
        mgr.register(_mk_handler(fetched={"v": 1}))

        first = await mgr.maybe_snapshot("automation", "x")
        await mgr.maybe_snapshot("automation", "x")
        await mgr.maybe_snapshot("automation", "x")

        assert first is not None
        assert not first.exists()
        assert len(list(tmp_path.glob("automation.x.*.yaml"))) == 2


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

    @staticmethod
    def _write_named(tmp_path: Path, name: str, entity_id: str) -> Path:
        """Drop a snapshot file under an explicit filename.

        Lets a test place the legacy (pre-digest) stem and two colliding
        ids without racing the second-resolution timestamp.
        """
        target = tmp_path / name
        target.write_text(
            "# ha_mcp_backup\n"
            + yaml.safe_dump(
                {
                    "schema_version": SCHEMA_VERSION,
                    "domain": "blueprint_automation",
                    "entity_id": entity_id,
                    "captured": "2026-09-03T00:00:00+00:00",
                    "tool": "t",
                    "config": "blueprint: {}\n",
                    "kind": "text",
                },
                sort_keys=False,
            )
        )
        return target

    def _colliding_blueprint_snapshots(self, tmp_path: Path) -> dict[str, Path]:
        """Three files whose stems collide the way blueprint paths do (#2329).

        ``user/motion.yaml`` sanitises to ``user_motion.yaml``, which is the
        CURRENT stem of the distinct blueprint ``user_motion.yaml``. A snapshot
        of the path written before the digest suffix existed sits under that
        bare stem too, so the name alone cannot tell the two histories apart.
        """
        digest_stem = _safe_entity_id("user/motion.yaml")
        return {
            "digest": self._write_named(
                tmp_path,
                f"blueprint_automation.{digest_stem}.20260903_120000.yaml",
                "user/motion.yaml",
            ),
            "legacy": self._write_named(
                tmp_path,
                "blueprint_automation.user_motion.yaml.20260903_110000.yaml",
                "user/motion.yaml",
            ),
            "other": self._write_named(
                tmp_path,
                "blueprint_automation.user_motion.yaml.20260903_100000.yaml",
                "user_motion.yaml",
            ),
        }

    def test_entity_filter_reads_the_payload_across_the_stem_collision(
        self, tmp_path: Path
    ) -> None:
        """An entity-filtered listing is that entity's history, not its stem's.

        Rotation already asks the payload before trusting a filename; the
        listing has to as well, or ``user/motion.yaml`` reports the distinct
        blueprint ``user_motion.yaml``'s snapshots as its own — and the bulk
        delete built on it unlinks them (Patch76, PR #2356).
        """
        files = self._colliding_blueprint_snapshots(tmp_path)
        mgr = _mk_manager(tmp_path)

        for_path = {m["name"] for m in mgr.list_snapshots(entity_id="user/motion.yaml")}
        for_other = {
            m["name"] for m in mgr.list_snapshots(entity_id="user_motion.yaml")
        }

        assert for_path == {files["digest"].name, files["legacy"].name}
        assert for_other == {files["other"].name}

    def test_delete_bulk_by_entity_leaves_the_colliding_blueprint_alone(
        self, tmp_path: Path
    ) -> None:
        """The caller asked to delete ONE blueprint's history.

        Both spellings of that blueprint's stem go, including the pre-digest
        one; the other blueprint that happens to share the bare stem keeps its
        only restore point.
        """
        files = self._colliding_blueprint_snapshots(tmp_path)
        mgr = _mk_manager(tmp_path)

        bulk = mgr.delete_bulk(entity_id="user/motion.yaml")

        assert set(bulk["deleted"]) == {files["digest"].name, files["legacy"].name}
        assert bulk["failed"] == []
        assert not files["digest"].exists()
        assert not files["legacy"].exists()
        assert files["other"].exists()

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


# ---------------------------------------------------------------- diff helpers


class TestPointerSegment:
    """RFC 6901 §3 escape order: ``~`` first, then ``/``."""

    def test_plain_segment_passes_through(self) -> None:
        assert _pointer_segment("alias") == "alias"

    def test_slash_escapes_to_tilde_one(self) -> None:
        assert _pointer_segment("a/b") == "a~1b"

    def test_tilde_escapes_to_tilde_zero(self) -> None:
        assert _pointer_segment("a~b") == "a~0b"

    def test_tilde_one_in_input_round_trips(self) -> None:
        # Forward order escapes ``~`` → ``~0`` first, so the literal
        # ``~1`` becomes ``~01``. The reverse order is the real hazard:
        # a literal ``/`` escaped to ``~1`` would then be corrupted to
        # ``~01`` by the ``~`` pass (see ``_pointer_segment``).
        assert _pointer_segment("~1") == "~01"


class TestDiffNode:
    """``_compute_json_patch`` patch shape against representative configs."""

    def test_identical_configs_produce_empty_patch(self) -> None:
        out: list[dict[str, Any]] = []
        truncated = _compute_json_patch({"a": 1}, {"a": 1}, 50, out)
        assert out == []
        assert truncated is False

    def test_scalar_change_is_replace(self) -> None:
        out: list[dict[str, Any]] = []
        _compute_json_patch({"a": 2}, {"a": 1}, 50, out)
        assert out == [{"op": "replace", "path": "/a", "value": 2}]

    def test_missing_key_in_current_is_add(self) -> None:
        out: list[dict[str, Any]] = []
        _compute_json_patch({"a": 1, "b": 2}, {"a": 1}, 50, out)
        assert out == [{"op": "add", "path": "/b", "value": 2}]

    def test_extra_key_in_current_is_remove(self) -> None:
        out: list[dict[str, Any]] = []
        _compute_json_patch({"a": 1}, {"a": 1, "b": 2}, 50, out)
        assert out == [{"op": "remove", "path": "/b"}]

    def test_bool_vs_int_does_not_collapse(self) -> None:
        # ``True == 1`` in Python but they represent different states for
        # HA-side toggles; the diff must treat the type change as a
        # replace, not silently equate them.
        out: list[dict[str, Any]] = []
        _compute_json_patch({"enabled": True}, {"enabled": 1}, 50, out)
        assert out == [{"op": "replace", "path": "/enabled", "value": True}]

    def test_list_length_grew_in_current_emits_remove_in_reverse(self) -> None:
        out: list[dict[str, Any]] = []
        _compute_json_patch([1, 2], [1, 2, 3, 4], 50, out)
        # Removes in reverse index order so each op stays valid against
        # the document state it gets applied to.
        assert out == [
            {"op": "remove", "path": "/3"},
            {"op": "remove", "path": "/2"},
        ]

    def test_list_length_shrunk_in_current_emits_append(self) -> None:
        out: list[dict[str, Any]] = []
        _compute_json_patch([1, 2, 3], [1, 2], 50, out)
        assert out == [{"op": "add", "path": "/-", "value": 3}]

    def test_nested_change_uses_pointer_path(self) -> None:
        stored = {"trigger": [{"platform": "state", "entity_id": "binary_sensor.x"}]}
        current = {"trigger": [{"platform": "time", "entity_id": "binary_sensor.x"}]}
        out: list[dict[str, Any]] = []
        _compute_json_patch(stored, current, 50, out)
        assert out == [
            {"op": "replace", "path": "/trigger/0/platform", "value": "state"}
        ]

    def test_key_with_slash_gets_escaped(self) -> None:
        out: list[dict[str, Any]] = []
        _compute_json_patch({"a/b": 2}, {"a/b": 1}, 50, out)
        assert out == [{"op": "replace", "path": "/a~1b", "value": 2}]

    def test_root_type_swap_emits_root_replace(self) -> None:
        # When the captured shape is a dict but HA now returns a list
        # (schema migration, integration replaced), the only sane patch
        # is "replace whole doc" — JSON-Pointer ``""`` per RFC 6901 §6.
        out: list[dict[str, Any]] = []
        _compute_json_patch({"a": 1}, [1, 2], 50, out)
        assert out == [{"op": "replace", "path": "", "value": {"a": 1}}]

    def test_truncation_flag_set_when_max_ops_reached(self) -> None:
        # Build a config that produces > max_ops change ops.
        stored = {f"k{i}": i for i in range(10)}
        current: dict[str, Any] = {}
        out: list[dict[str, Any]] = []
        truncated = _compute_json_patch(stored, current, 3, out)
        assert truncated is True
        assert len(out) == 3
        # All three are adds since current is empty.
        assert {op["op"] for op in out} == {"add"}

    def test_patch_exactly_at_cap_is_not_truncated(self) -> None:
        # A patch that lands on exactly ``max_ops`` ops is complete, not
        # cut short — ``len(out) >= max_ops`` would have flagged it as a
        # false-positive truncation. The generator collects one beyond
        # the cap to tell "exactly full" from "overflowed".
        stored = {f"k{i}": i for i in range(3)}
        current: dict[str, Any] = {}
        out: list[dict[str, Any]] = []
        truncated = _compute_json_patch(stored, current, 3, out)
        assert truncated is False
        assert len(out) == 3
        assert {op["op"] for op in out} == {"add"}

    def test_nested_list_of_dicts_grows_emits_append(self) -> None:
        # A real trigger add: the stored automation has two triggers, the
        # live one only has the first — recovering ``stored`` means
        # appending the second trigger.
        stored = {
            "trigger": [
                {"platform": "state", "entity_id": "binary_sensor.a"},
                {"platform": "time", "at": "12:00:00"},
            ]
        }
        current = {"trigger": [{"platform": "state", "entity_id": "binary_sensor.a"}]}
        out: list[dict[str, Any]] = []
        _compute_json_patch(stored, current, 50, out)
        assert out == [
            {
                "op": "add",
                "path": "/trigger/-",
                "value": {"platform": "time", "at": "12:00:00"},
            }
        ]

    def test_nested_list_of_dicts_shrinks_emits_reverse_remove(self) -> None:
        # A real action removal: the stored script has one action, the
        # live one grew a second — recovering ``stored`` means removing
        # the extra tail entry.
        stored = {"action": [{"service": "light.turn_on"}]}
        current = {
            "action": [
                {"service": "light.turn_on"},
                {"service": "light.turn_off"},
            ]
        }
        out: list[dict[str, Any]] = []
        _compute_json_patch(stored, current, 50, out)
        assert out == [{"op": "remove", "path": "/action/1"}]

    def test_tilde_in_key_escapes_end_to_end(self) -> None:
        # ``~`` in a dict key must be escaped to ``~0`` in the pointer
        # path — exercised through ``_compute_json_patch`` (not just
        # ``_pointer_segment``) to cover the full diff path.
        out: list[dict[str, Any]] = []
        _compute_json_patch({"a~b": 2}, {"a~b": 1}, 50, out)
        assert out == [{"op": "replace", "path": "/a~0b", "value": 2}]


class TestRequireList:
    """``_require_list`` distinguishes a degraded fetch from a real miss."""

    def test_returns_list_unchanged(self) -> None:
        items = [{"id": "x"}]
        assert _require_list(items, "config/label_registry/list") is items

    @pytest.mark.parametrize("bad", [None, {"error": "denied"}, "oops", 42])
    def test_non_list_envelope_raises(self, bad: Any) -> None:
        # A non-list envelope is an unexpected/degraded response, not a
        # missing entity — it must raise (funnelling to a structured
        # error / capture warning) rather than return None, which callers
        # read as ``entity_missing``.
        with pytest.raises(HomeAssistantError):
            _require_list(bad, "config/label_registry/list")


class TestRequireDict:
    """``_require_dict`` distinguishes a degraded fetch from a real miss."""

    def test_returns_dict_unchanged(self) -> None:
        envelope = {"response": {}}
        assert _require_dict(envelope, "execute_script") is envelope

    @pytest.mark.parametrize("bad", [None, [1, 2], "oops", 42])
    def test_non_dict_envelope_raises(self, bad: Any) -> None:
        # Mirror of ``_require_list`` for the execute_script fetchers: a
        # non-dict envelope is a degraded/malformed 200, not a miss.
        with pytest.raises(HomeAssistantError):
            _require_dict(bad, "execute_script")


class TestExecuteScriptFetchers:
    """Calendar / todo fetchers route the ``execute_script`` envelope
    through ``_require_dict`` (Boy-Scout parity with the registry
    fetchers): a malformed 200 raises instead of masquerading as
    ``entity_missing``, while a genuine uid miss still returns ``None``."""

    @pytest.mark.parametrize("fetcher", [_fetch_calendar_event, _fetch_todo_item])
    async def test_non_dict_envelope_raises(
        self, fetcher: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _ws_send(_client: Any, _message: dict[str, Any]) -> Any:
            return ["unexpected", "list", "body"]

        monkeypatch.setattr(bm, "_ws_send", _ws_send)
        with pytest.raises(HomeAssistantError, match="Expected a dict"):
            await fetcher(object(), "calendar.x::uid-1")

    @pytest.mark.parametrize("fetcher", [_fetch_calendar_event, _fetch_todo_item])
    async def test_missing_uid_still_returns_none(
        self, fetcher: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A well-formed dict whose nested lookup finds no matching uid is a
        # real miss — it must stay ``None`` (entity_missing), not raise.
        async def _ws_send(_client: Any, _message: dict[str, Any]) -> Any:
            return {"response": {}}

        monkeypatch.setattr(bm, "_ws_send", _ws_send)
        assert await fetcher(object(), "calendar.x::absent-uid") is None

    @pytest.mark.parametrize(
        "fetcher, nested_key, id_key, entity",
        [
            (_fetch_calendar_event, "events", "calendar_entity_id", "calendar.x"),
            (_fetch_todo_item, "items", "todo_entity_id", "todo.x"),
        ],
    )
    async def test_matching_uid_returns_entry(
        self,
        fetcher: Any,
        nested_key: str,
        id_key: str,
        entity: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A well-formed envelope whose nested lookup finds the uid returns the
        # matching entry, merged with the ``<domain>_entity_id`` key the
        # restore path keys off of. Locks the success branch (and the
        # entity-keyed nesting) that the raise/miss tests don't exercise.
        uid = "uid-1"
        entry = {"uid": uid, "summary": "thing"}

        async def _ws_send(_client: Any, _message: dict[str, Any]) -> Any:
            return {"response": {nested_key: {entity: {nested_key: [entry]}}}}

        monkeypatch.setattr(bm, "_ws_send", _ws_send)
        result = await fetcher(object(), f"{entity}::{uid}")
        assert result == {id_key: entity, **entry}


class TestSummarizePatchCounts:
    def test_counts_each_op_class_and_total(self) -> None:
        patch = [
            {"op": "add", "path": "/x", "value": 1},
            {"op": "remove", "path": "/y"},
            {"op": "replace", "path": "/z", "value": 2},
            {"op": "add", "path": "/q", "value": 3},
        ]
        assert _summarize_patch_counts(patch) == {
            "add": 2,
            "remove": 1,
            "replace": 1,
            "total": 4,
        }

    def test_empty_patch_zero_counts(self) -> None:
        assert _summarize_patch_counts([]) == {
            "add": 0,
            "remove": 0,
            "replace": 0,
            "total": 0,
        }

    def test_unrecognized_op_warns_and_undercounts_classes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # ``_diff_node`` only emits add/remove/replace today; if a future
        # op type (move/copy/test) ever appears, the class counts sum to
        # less than ``total``. The warning makes that drift visible.
        patch = [
            {"op": "add", "path": "/x", "value": 1},
            {"op": "move", "from": "/a", "path": "/b"},
        ]
        with caplog.at_level("WARNING"):
            counts = _summarize_patch_counts(patch)
        assert counts == {"add": 1, "remove": 0, "replace": 0, "total": 2}
        assert counts["add"] + counts["remove"] + counts["replace"] < counts["total"]
        assert any(
            "unrecognized JSON-Patch op" in r.message and "move" in r.message
            for r in caplog.records
        )


class TestDiffSnapshot:
    """``BackupManager.diff_snapshot`` end-to-end against stub handlers."""

    async def test_diff_against_unchanged_current_returns_empty_patch(
        self, tmp_path: Path
    ) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(fetched={"alias": "kitchen"}))
        path = await mgr.maybe_snapshot("automation", "x")
        assert path is not None
        result = await mgr.diff_snapshot(path.name)
        assert result["kind"] == "dict"
        assert result["domain"] == "automation"
        assert result["entity_id"] == "x"
        assert result["entity_missing"] is False
        assert result["unchanged"] is True
        assert result["patch"] == []
        assert result["counts"] == {
            "add": 0,
            "remove": 0,
            "replace": 0,
            "total": 0,
        }
        assert result["truncated"] is False
        assert result["captured_at"] is not None

    async def test_diff_reports_scalar_replace(self, tmp_path: Path) -> None:
        # Capture, then mutate the handler's "current" so a subsequent
        # diff sees a divergent live state.
        captured = {"alias": "kitchen", "mode": "single"}
        current = {"alias": "kitchen", "mode": "queued"}
        handler_fetch_results: list[Any] = [captured, current]

        async def fetch(_client: Any, _entity_id: str) -> Any:
            return handler_fetch_results.pop(0)

        async def restore(_client: Any, _entity_id: str, _config: Any) -> Any:
            return None

        mgr = _mk_manager(tmp_path)
        mgr.register(DomainHandler(domain="automation", fetch=fetch, restore=restore))
        path = await mgr.maybe_snapshot("automation", "x")
        assert path is not None
        result = await mgr.diff_snapshot(path.name)
        assert result["unchanged"] is False
        assert result["patch"] == [
            {"op": "replace", "path": "/mode", "value": "single"}
        ]
        assert result["counts"]["replace"] == 1

    async def test_diff_flags_entity_missing_when_fetch_returns_none(
        self, tmp_path: Path
    ) -> None:
        # First fetch (capture) returns config; second (diff) returns None
        # to simulate the entity being deleted after capture.
        results: list[Any] = [{"alias": "x"}, None]

        async def fetch(_client: Any, _entity_id: str) -> Any:
            return results.pop(0)

        async def restore(_client: Any, _entity_id: str, _config: Any) -> Any:
            return None

        mgr = _mk_manager(tmp_path)
        mgr.register(DomainHandler(domain="automation", fetch=fetch, restore=restore))
        path = await mgr.maybe_snapshot("automation", "x")
        assert path is not None
        result = await mgr.diff_snapshot(path.name)
        assert result["entity_missing"] is True
        # No patch when entity is gone — the consumer-side action is "the
        # restore would re-create this", not "apply N ops".
        assert result["patch"] == []
        assert result["truncated"] is False
        # ``unchanged`` must be False under ``entity_missing``: the empty
        # patch is an artefact of the absent target, not a "live config
        # matches snapshot" signal. An agent scanning ``unchanged`` to
        # skip action would otherwise be wrong.
        assert result["unchanged"] is False
        # ``captured_at`` is the snapshot timestamp, present in both
        # branches — asserted so a rename to a non-existent key surfaces
        # as a failure instead of silently yielding None.
        assert result["captured_at"] is not None

    async def test_diff_raises_on_degraded_fetch_envelope(self, tmp_path: Path) -> None:
        # Item #1 end-to-end: a degraded fetch (non-list envelope from an
        # auth-scope change / API drift) must propagate out of
        # ``diff_snapshot`` as a raise, NOT be misread as
        # ``entity_missing``. The capture fetch succeeds; the diff fetch
        # routes a dict envelope through ``_require_list`` and raises.
        results: list[Any] = [{"alias": "x"}]

        async def fetch(_client: Any, _entity_id: str) -> Any:
            if results:
                return results.pop(0)
            return _require_list({"error": "permission denied"}, "automation/list")

        async def restore(_client: Any, _entity_id: str, _config: Any) -> Any:
            return None

        mgr = _mk_manager(tmp_path)
        mgr.register(DomainHandler(domain="automation", fetch=fetch, restore=restore))
        path = await mgr.maybe_snapshot("automation", "x")
        assert path is not None
        with pytest.raises(HomeAssistantError, match="Expected a list"):
            await mgr.diff_snapshot(path.name)

    async def test_diff_raises_lookup_error_on_unknown_domain(
        self, tmp_path: Path
    ) -> None:
        mgr = _mk_manager(tmp_path)
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
            await mgr.diff_snapshot(bogus.name)

    async def test_diff_raises_file_not_found_on_missing_snapshot(
        self, tmp_path: Path
    ) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(fetched={"alias": "x"}))
        with pytest.raises(FileNotFoundError):
            await mgr.diff_snapshot("automation.x.20260521_120000.yaml")

    async def test_diff_truncation_flagged_when_patch_exceeds_cap(
        self, tmp_path: Path
    ) -> None:
        # Capture a large config, then return a divergent one to force
        # >_MAX_PATCH_OPS ops.
        big_stored = {f"k{i}": i for i in range(_MAX_PATCH_OPS + 50)}
        results: list[Any] = [big_stored, {}]

        async def fetch(_client: Any, _entity_id: str) -> Any:
            return results.pop(0)

        async def restore(_client: Any, _entity_id: str, _config: Any) -> Any:
            return None

        mgr = _mk_manager(tmp_path)
        mgr.register(DomainHandler(domain="automation", fetch=fetch, restore=restore))
        path = await mgr.maybe_snapshot("automation", "x")
        assert path is not None
        result = await mgr.diff_snapshot(path.name)
        assert result["truncated"] is True
        assert len(result["patch"]) == _MAX_PATCH_OPS


# ---------------------------------------------------------------- file/YAML fold (#1579 PR2)


class TestBuildTextDiffResponse:
    def test_changed_emits_unified_diff(self) -> None:
        r = _build_text_diff_response(
            "snap", "file", "www/x.css", "t0", "a\nb\nc\n", "a\nB\nc\n"
        )
        assert r["kind"] == "text"
        assert r["entity_missing"] is False
        assert r["unchanged"] is False
        assert r["truncated"] is False
        assert "-B" in r["diff"] and "+b" in r["diff"]

    def test_unchanged_when_identical(self) -> None:
        r = _build_text_diff_response("snap", "file", "p", "t0", "same\n", "same\n")
        assert r["unchanged"] is True
        assert r["diff"] == ""

    def test_missing_target(self) -> None:
        r = _build_text_diff_response("snap", "file", "p", "t0", "x\n", None)
        assert r["entity_missing"] is True
        assert r["unchanged"] is False
        assert r["diff"] == ""

    def test_truncates_at_cap(self) -> None:
        stored = "".join(f"s{i}\n" for i in range(bm._MAX_DIFF_LINES + 50))
        r = _build_text_diff_response("snap", "file", "p", "t0", stored, "current\n")
        assert r["truncated"] is True
        assert len(r["diff"].splitlines()) == bm._MAX_DIFF_LINES


def _patch_services(
    monkeypatch: pytest.MonkeyPatch, responses: dict[str, Any], calls: list[Any]
) -> None:
    """Stub call_mcp_tools_service + unwrap so the file/yaml handlers run
    without a HA instance. ``responses`` maps service name -> return dict."""

    async def fake_call(_client: Any, service: str, data: dict[str, Any]) -> Any:
        calls.append((service, data))
        return responses[service]

    monkeypatch.setattr(
        "ha_mcp.tools.tools_filesystem.call_mcp_tools_service", fake_call
    )
    monkeypatch.setattr(
        "ha_mcp.tools.util_helpers.unwrap_service_response", lambda r: r
    )


class TestFileHandler:
    async def test_fetch_returns_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_services(
            monkeypatch, {"read_file": {"success": True, "content": "body"}}, []
        )
        assert await bm._fetch_file(_StubClient(), "www/x.css") == "body"

    async def test_fetch_missing_file_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_services(
            monkeypatch,
            {"read_file": {"success": False, "error": "File does not exist: www/x"}},
            [],
        )
        assert await bm._fetch_file(_StubClient(), "www/x.css") is None

    async def test_fetch_other_error_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_services(
            monkeypatch,
            {"read_file": {"success": False, "error": "Permission denied: www/x"}},
            [],
        )
        with pytest.raises(HomeAssistantError):
            await bm._fetch_file(_StubClient(), "www/x.css")

    async def test_restore_writes_via_write_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[Any] = []
        _patch_services(monkeypatch, {"write_file": {"success": True}}, calls)
        await bm._restore_file(_StubClient(), "www/x.css", "new body")
        service, data = calls[0]
        assert service == "write_file"
        assert data["path"] == "www/x.css"
        assert data["content"] == "new body"
        assert data["overwrite"] is True

    async def test_restore_raises_on_service_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_services(
            monkeypatch, {"write_file": {"success": False, "error": "nope"}}, []
        )
        with pytest.raises(HomeAssistantError):
            await bm._restore_file(_StubClient(), "www/x.css", "body")


class TestYamlHandler:
    async def test_fetch_returns_component_subtree(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The component (which has ruamel) extracts the subtree and returns
        # it under ``subtree``; the server just forwards it. Verify the call
        # passes ``yaml_path`` so the component knows what to extract.
        calls: list[Any] = []
        _patch_services(
            monkeypatch,
            {"read_file": {"success": True, "subtree": "resource: http://a\n"}},
            calls,
        )
        out = await bm._fetch_yaml(_StubClient(), "configuration.yaml::rest")
        assert out == "resource: http://a\n"
        service, data = calls[0]
        assert service == "read_file"
        assert data["path"] == "configuration.yaml"
        assert data["yaml_path"] == "rest"

    async def test_fetch_absent_key_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # File exists but the key is absent → component returns subtree=None.
        _patch_services(
            monkeypatch, {"read_file": {"success": True, "subtree": None}}, []
        )
        assert await bm._fetch_yaml(_StubClient(), "configuration.yaml::nope") is None

    async def test_fetch_missing_file_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_services(
            monkeypatch,
            {
                "read_file": {
                    "success": False,
                    "error": "File does not exist: configuration.yaml",
                }
            },
            [],
        )
        assert await bm._fetch_yaml(_StubClient(), "configuration.yaml::rest") is None

    async def test_fetch_other_error_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A non-not-found read failure must propagate (not silently skip the
        # backup) so the mandatory-gate promise holds — mirrors _fetch_file.
        _patch_services(
            monkeypatch,
            {"read_file": {"success": False, "error": "Permission denied"}},
            [],
        )
        with pytest.raises(HomeAssistantError):
            await bm._fetch_yaml(_StubClient(), "configuration.yaml::rest")

    async def test_fetch_bad_entity_id_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert await bm._fetch_yaml(_StubClient(), "no-separator") is None

    async def test_restore_replaces_via_edit_yaml_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[Any] = []
        _patch_services(monkeypatch, {"edit_yaml_config": {"success": True}}, calls)
        await bm._restore_yaml(
            _StubClient(), "configuration.yaml::rest", "resource: http://a\n"
        )
        service, data = calls[0]
        assert service == "edit_yaml_config"
        assert data["file"] == "configuration.yaml"
        assert data["yaml_path"] == "rest"
        assert data["action"] == "replace"
        assert data["content"] == "resource: http://a\n"

    async def test_restore_passes_operator_extra_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A snapshot of an operator-extra key must be restorable (#1887).

        The write that produced it was allowed only because of the extra-key
        setting, so the restore has to carry the same set or the component
        rejects it as "not in the allowed list".
        """
        from ha_mcp import config as ha_mcp_config
        from ha_mcp.tools import tools_filesystem as fsmod

        monkeypatch.setenv("HA_MCP_EXTRA_YAML_KEYS", "alert2")
        monkeypatch.setattr(ha_mcp_config, "_settings", None)
        calls: list[Any] = []
        # The effective-keys read hits get_extra_yaml_keys first (empty store
        # here); the union then carries the server setting to the wire.
        _patch_services(
            monkeypatch,
            {
                "get_extra_yaml_keys": {"success": True, "keys": []},
                "edit_yaml_config": {"success": True},
            },
            calls,
        )

        client = _StubClient()

        async def _ensure(_client, **_kwargs):
            return "token"

        monkeypatch.setattr(fsmod, "_ensure_caller_token", _ensure)
        fsmod._COMPONENT_VERSION_CACHE[client] = "1.2.4"

        await bm._restore_yaml(client, "packages/alerts.yaml::alert2", "defaults: {}\n")
        data = next(d for s, d in calls if s == "edit_yaml_config")
        assert data["extra_allowed_keys"] == ["alert2"]

    async def test_restore_blocks_on_component_too_old(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A component predating extra_allowed_keys must produce the same
        actionable update prompt as the write path, not the opaque
        "extra keys not allowed" schema rejection from Home Assistant."""
        from fastmcp.exceptions import ToolError

        from ha_mcp import config as ha_mcp_config
        from ha_mcp.tools import tools_filesystem as fsmod

        monkeypatch.setenv("HA_MCP_EXTRA_YAML_KEYS", "alert2")
        monkeypatch.setattr(ha_mcp_config, "_settings", None)
        calls: list[Any] = []
        _patch_services(monkeypatch, {"edit_yaml_config": {"success": True}}, calls)

        client = _StubClient()

        async def _ensure(_client, **_kwargs):
            return "token"

        monkeypatch.setattr(fsmod, "_ensure_caller_token", _ensure)
        fsmod._COMPONENT_VERSION_CACHE[client] = "1.2.3"

        with pytest.raises(ToolError) as excinfo:
            await bm._restore_yaml(
                client, "packages/alerts.yaml::alert2", "defaults: {}\n"
            )
        assert "1.2.4" in str(excinfo.value)
        assert not calls

    async def test_restore_omits_extra_keys_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default config must keep the pre-#1887 payload, so a component
        that predates the field still restores."""
        from ha_mcp import config as ha_mcp_config

        monkeypatch.delenv("HA_MCP_EXTRA_YAML_KEYS", raising=False)
        monkeypatch.setattr(ha_mcp_config, "_settings", None)
        calls: list[Any] = []
        _patch_services(monkeypatch, {"edit_yaml_config": {"success": True}}, calls)
        await bm._restore_yaml(
            _StubClient(), "configuration.yaml::rest", "resource: http://a\n"
        )
        _, data = calls[0]
        assert "extra_allowed_keys" not in data

    async def test_restore_bad_entity_id_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with pytest.raises(ValueError):
            await bm._restore_yaml(_StubClient(), "no-separator", "x")

    async def test_file_path_with_double_colon_splits_on_last(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A file path that itself contains "::" must split on the LAST "::"
        # so the (file, yaml_path) pair stays correct (rpartition, not
        # partition).
        calls: list[Any] = []
        _patch_services(monkeypatch, {"edit_yaml_config": {"success": True}}, calls)
        await bm._restore_yaml(
            _StubClient(), "packages/weird::name.yaml::rest", "resource: x\n"
        )
        _, data = calls[0]
        assert data["file"] == "packages/weird::name.yaml"
        assert data["yaml_path"] == "rest"


class TestTextSnapshotKind:
    async def test_write_snapshot_marks_text_domain(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)

        async def fetch(_c: Any, _e: str) -> Any:
            return "file body\n"

        async def restore(_c: Any, _e: str, cfg: Any) -> Any:
            return cfg

        mgr.register(DomainHandler("file", fetch, restore))
        snap = await mgr.maybe_snapshot("file", "www/x.css", force=True)
        assert snap is not None
        data = yaml.safe_load(snap.read_text())
        assert data["kind"] == "text"
        assert data["config"] == "file body\n"

    async def test_dict_domain_has_no_kind(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(domain="automation", fetched={"alias": "x"}))
        snap = await mgr.maybe_snapshot("automation", "a1", force=True)
        assert snap is not None
        data = yaml.safe_load(snap.read_text())
        assert "kind" not in data

    async def test_diff_snapshot_text_branch(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        seq = iter(["a\nb\nc\n", "a\nB\nc\n"])

        async def fetch(_c: Any, _e: str) -> Any:
            return next(seq)

        async def restore(_c: Any, _e: str, cfg: Any) -> Any:
            return cfg

        mgr.register(DomainHandler("file", fetch, restore))
        snap = await mgr.maybe_snapshot("file", "www/x.css", force=True)
        assert snap is not None
        diff = await mgr.diff_snapshot(snap.name)
        assert diff["kind"] == "text"
        assert diff["unchanged"] is False
        assert "-B" in diff["diff"] and "+b" in diff["diff"]


class TestMaybeSnapshotMandatory:
    """``maybe_snapshot(mandatory=True)`` raises ``MandatoryBackupError`` on a
    genuine capture failure, but still returns None for a legitimate skip."""

    async def test_raises_on_fetch_failure(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(
            _mk_handler(domain="file", raise_on_fetch=HomeAssistantError("boom"))
        )
        with pytest.raises(bm.MandatoryBackupError):
            await mgr.maybe_snapshot("file", "www/x.css", mandatory=True, force=True)

    async def test_raises_on_write_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(domain="file", fetched="body\n"))

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise OSError("No space left on device")

        monkeypatch.setattr(mgr, "_write_snapshot", _boom)
        with pytest.raises(bm.MandatoryBackupError) as exc:
            await mgr.maybe_snapshot("file", "www/x.css", mandatory=True, force=True)
        # disk-full remediation is carried for the structured error to surface.
        assert exc.value.suggestions

    async def test_raises_on_init_dir_error(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(domain="file", fetched="body\n"))
        mgr._init_dir_error = "backup dir not writable"
        with pytest.raises(bm.MandatoryBackupError):
            await mgr.maybe_snapshot("file", "www/x.css", mandatory=True, force=True)

    async def test_new_entity_returns_none_not_raise(self, tmp_path: Path) -> None:
        """config is None (new file/key) is a legitimate skip, not a failure."""
        mgr = _mk_manager(tmp_path)
        mgr.register(_mk_handler(domain="file", fetched=None))
        assert (
            await mgr.maybe_snapshot("file", "www/new.css", mandatory=True, force=True)
            is None
        )

    async def test_best_effort_fetch_failure_returns_none(self, tmp_path: Path) -> None:
        """Without mandatory, a fetch failure stays swallowed (returns None)."""
        mgr = _mk_manager(tmp_path)
        mgr.register(
            _mk_handler(domain="file", raise_on_fetch=HomeAssistantError("boom"))
        )
        assert await mgr.maybe_snapshot("file", "www/x.css", force=True) is None


class TestMandatoryGate:
    """``mandatory=True`` fails the write closed (#1579): a disabled toggle OR
    a genuine capture failure blocks the wrapped write with a structured error
    and never runs it; a legitimate "nothing to snapshot" skip proceeds."""

    def _decorate(self, *, mandatory: bool) -> Any:
        ran: list[str] = []

        @with_auto_backup(domain="file", id_param="path", mandatory=mandatory)
        async def tool(self_obj: Any, path: str) -> str:
            ran.append(path)
            return "wrote"

        tool.ran = ran  # type: ignore[attr-defined]
        return tool

    @staticmethod
    def _error_code(exc: ToolError) -> str:
        return str(json.loads(str(exc))["error"]["code"])

    async def test_refuses_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_global_settings",
            lambda: _StubSettings(enable_auto_backup=False),
        )

        class _Self:
            _client = None

        tool = self._decorate(mandatory=True)
        with pytest.raises(ToolError) as exc:
            await tool(_Self(), path="www/x.css")
        # Pin the code + the user-facing "blocked, nothing changed" guarantee so
        # a reword can't silently gut it.
        assert self._error_code(exc.value) == "CONFIG_VALIDATION_FAILED"
        assert "requires auto-backup" in str(exc.value)
        assert "blocked and nothing was changed" in str(exc.value)
        assert tool.ran == []  # type: ignore[attr-defined]

    async def test_no_client_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Toggle on but no client to capture with → block, don't write
        un-backed-up. (This used to silently proceed.)"""
        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_global_settings",
            lambda: _StubSettings(enable_auto_backup=True),
        )

        class _Self:
            _client = None

        tool = self._decorate(mandatory=True)
        with pytest.raises(ToolError) as exc:
            await tool(_Self(), path="www/x.css")
        assert self._error_code(exc.value) == "BACKUP_CAPTURE_FAILED"
        assert tool.ran == []  # type: ignore[attr-defined]

    async def test_capture_failure_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuine capture failure (MandatoryBackupError) blocks the write and
        surfaces its remediation in the structured error."""

        class _FakeMgr:
            async def maybe_snapshot(self, *a: Any, **k: Any) -> None:
                assert k.get("mandatory") is True
                raise bm.MandatoryBackupError(
                    "disk full", suggestions=["Free up disk space"]
                )

        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_global_settings",
            lambda: _StubSettings(enable_auto_backup=True),
        )
        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_backup_manager", lambda _c, _s: _FakeMgr()
        )

        ran: list[str] = []

        @with_auto_backup(
            domain="file", id_param="path", mandatory=True, client=object()
        )
        async def tool(*, path: str) -> str:
            ran.append(path)
            return "wrote"

        with pytest.raises(ToolError) as exc:
            await tool(path="www/x.css")
        assert self._error_code(exc.value) == "BACKUP_CAPTURE_FAILED"
        assert "disk full" in str(exc.value)
        assert "Free up disk space" in str(exc.value)
        assert ran == []  # write never ran

    async def test_component_not_installed_cause_surfaces_unwrapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A capture that failed because the File & YAML Tools entry is absent
        re-raises the original COMPONENT_NOT_INSTALLED error, not a backup
        complaint (#2292): the remediation is "add the entry", and retrying
        the backup cannot succeed. The write stays blocked either way."""
        component_error = ToolError(
            json.dumps(
                create_error_response(
                    ErrorCode.COMPONENT_NOT_INSTALLED,
                    "Add entry -> HA-MCP File & YAML Tools",
                )
            )
        )

        class _FakeMgr:
            async def maybe_snapshot(self, *a: Any, **k: Any) -> None:
                raise bm.MandatoryBackupError(
                    "could not read the current state to back it up"
                ) from component_error

        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_global_settings",
            lambda: _StubSettings(enable_auto_backup=True),
        )
        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_backup_manager", lambda _c, _s: _FakeMgr()
        )

        ran: list[str] = []

        @with_auto_backup(
            domain="file", id_param="path", mandatory=True, client=object()
        )
        async def tool(*, path: str) -> str:
            ran.append(path)
            return "wrote"

        with pytest.raises(ToolError) as exc:
            await tool(path="www/x.css")
        assert exc.value is component_error  # unwrapped, byte-identical
        assert self._error_code(exc.value) == "COMPONENT_NOT_INSTALLED"
        assert ran == []  # write never ran

    async def test_component_cause_found_behind_unrelated_explicit_cause(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``raise ... from other`` buries the component error in __context__
        while an unrelated exception occupies __cause__ — the cause walk must
        traverse BOTH links or the failure regresses to BACKUP_CAPTURE_FAILED."""
        component_error = ToolError(
            json.dumps(
                create_error_response(
                    ErrorCode.COMPONENT_NOT_INSTALLED,
                    "Add entry -> HA-MCP File & YAML Tools",
                )
            )
        )

        class _FakeMgr:
            async def maybe_snapshot(self, *a: Any, **k: Any) -> None:
                try:
                    raise component_error
                except ToolError:
                    # Explicit unrelated cause; the ToolError stays reachable
                    # only through __context__.
                    raise bm.MandatoryBackupError("capture failed") from RuntimeError(
                        "unrelated"
                    )

        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_global_settings",
            lambda: _StubSettings(enable_auto_backup=True),
        )
        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_backup_manager", lambda _c, _s: _FakeMgr()
        )

        ran: list[str] = []

        @with_auto_backup(
            domain="file", id_param="path", mandatory=True, client=object()
        )
        async def tool(*, path: str) -> str:
            ran.append(path)
            return "wrote"

        with pytest.raises(ToolError) as exc:
            await tool(path="www/x.css")
        assert exc.value is component_error
        assert ran == []

    async def test_unrelated_tool_error_in_the_chain_is_not_unwrapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cause walk must match on the CODE, not on "a ToolError is in
        there somewhere". A capture that failed for an unrelated structured
        reason still surfaces as BACKUP_CAPTURE_FAILED — unwrapping it would
        hand the caller an error whose remediation has nothing to do with why
        the write was blocked."""
        unrelated = ToolError(
            json.dumps(
                create_error_response(
                    ErrorCode.AUTH_INSUFFICIENT_PERMISSIONS,
                    "token cannot read that path",
                )
            )
        )

        class _FakeMgr:
            async def maybe_snapshot(self, *a: Any, **k: Any) -> None:
                raise bm.MandatoryBackupError("could not read the current state") from (
                    unrelated
                )

        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_global_settings",
            lambda: _StubSettings(enable_auto_backup=True),
        )
        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_backup_manager", lambda _c, _s: _FakeMgr()
        )

        ran: list[str] = []

        @with_auto_backup(
            domain="file", id_param="path", mandatory=True, client=object()
        )
        async def tool(*, path: str) -> str:
            ran.append(path)
            return "wrote"

        with pytest.raises(ToolError) as exc:
            await tool(path="www/x.css")
        assert self._error_code(exc.value) == "BACKUP_CAPTURE_FAILED"
        assert exc.value is not unrelated
        assert ran == []  # write never ran

    async def test_plain_text_tool_error_in_the_chain_is_tolerated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not every ToolError carries a JSON body — a bare-string one in the
        cause chain must be skipped rather than blowing up the walk, so the
        capture failure still lands as BACKUP_CAPTURE_FAILED."""

        class _FakeMgr:
            async def maybe_snapshot(self, *a: Any, **k: Any) -> None:
                raise bm.MandatoryBackupError("could not read the current state") from (
                    ToolError("something went wrong")
                )

        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_global_settings",
            lambda: _StubSettings(enable_auto_backup=True),
        )
        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_backup_manager", lambda _c, _s: _FakeMgr()
        )

        ran: list[str] = []

        @with_auto_backup(
            domain="file", id_param="path", mandatory=True, client=object()
        )
        async def tool(*, path: str) -> str:
            ran.append(path)
            return "wrote"

        with pytest.raises(ToolError) as exc:
            await tool(path="www/x.css")
        assert self._error_code(exc.value) == "BACKUP_CAPTURE_FAILED"
        assert ran == []

    async def test_legitimate_skip_proceeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A new file/key (maybe_snapshot returns None — nothing to snapshot) is
        not a failure: the write proceeds."""

        class _FakeMgr:
            async def maybe_snapshot(self, *a: Any, **k: Any) -> None:
                return None

        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_global_settings",
            lambda: _StubSettings(enable_auto_backup=True),
        )
        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_backup_manager", lambda _c, _s: _FakeMgr()
        )

        @with_auto_backup(
            domain="file", id_param="path", mandatory=True, client=object()
        )
        async def tool(*, path: str) -> str:
            return "wrote"

        assert await tool(path="www/new.css") == "wrote"

    async def test_target_resolution_error_is_not_swallowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An id_fn that raises a transient-tuple error must NOT be swallowed
        into a silent un-backed-up write — target resolution sits outside the
        best-effort capture handler, so the error aborts the call (fail closed).
        """
        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_global_settings",
            lambda: _StubSettings(enable_auto_backup=True),
        )

        def _boom(_kw: dict[str, Any]) -> str:
            raise bm.HomeAssistantError("cannot resolve backup target")

        ran: list[str] = []

        @with_auto_backup(domain="file", id_fn=_boom, mandatory=True, client=object())
        async def tool(*, path: str) -> str:
            ran.append(path)
            return "wrote"

        with pytest.raises(bm.HomeAssistantError):
            await tool(path="www/x.css")
        assert ran == []  # write never ran

    async def test_non_mandatory_proceeds_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_global_settings",
            lambda: _StubSettings(enable_auto_backup=False),
        )

        class _Self:
            _client = None

        tool = self._decorate(mandatory=False)
        assert await tool(_Self(), path="www/x.css") == "wrote"


# ----------------------------------------------------------------- #1579 PR2
# legacy store (.ha_mcp_tools_backups/) surfaced through scope="edits"


class TestYamlFileHandler:
    """yaml_file domain: whole-file config restore via edit_yaml_config(replace_file)."""

    def test_is_text_domain(self) -> None:
        assert "yaml_file" in bm._TEXT_DOMAINS

    async def test_registered_in_defaults(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        bm.register_default_handlers(mgr, _StubClient())
        assert mgr.handler_for("yaml_file") is not None
        assert "yaml_file" in mgr.supported_domains()

    async def test_restore_calls_replace_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[Any] = []
        _patch_services(monkeypatch, {"edit_yaml_config": {"success": True}}, calls)
        await bm._restore_yaml_file(_StubClient(), "configuration.yaml", "a: 1\n")
        service, data = calls[0]
        assert service == "edit_yaml_config"
        assert data["file"] == "configuration.yaml"
        assert data["action"] == "replace_file"
        assert data["yaml_path"] == ""
        assert data["content"] == "a: 1\n"

    async def test_restore_failure_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_services(
            monkeypatch,
            {"edit_yaml_config": {"success": False, "error": "bad path"}},
            [],
        )
        with pytest.raises(HomeAssistantError):
            await bm._restore_yaml_file(_StubClient(), "configuration.yaml", "a: 1\n")


async def _bp_noop_tool(**_kwargs: Any) -> str:
    """Stand-in for ``ha_manage_blueprints``: the decorator is what is tested."""
    return "ok"


class _BlueprintWsClient:
    """Dispatch-on-``type`` WebSocket spy for the blueprint handler's tier 2."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.sent: list[dict[str, Any]] = []

    async def send_websocket_message(self, msg: dict[str, Any]) -> Any:
        self.sent.append(msg)
        frame_type = msg.get("type")
        if frame_type not in self.responses:
            raise AssertionError(f"unexpected WebSocket frame: {frame_type}")
        return self.responses[frame_type]

    def frames(self, frame_type: str) -> list[dict[str, Any]]:
        return [m for m in self.sent if m.get("type") == frame_type]


_BP_PATH = "user/motion.yaml"
_BP_YAML = "blueprint:\n  name: Motion Light\n  domain: automation\n"
_BP_SOURCE_URL = "https://example.com/motion.yaml"


def _bp_listing(*, source_url: str | None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": "Motion Light"}
    if source_url is not None:
        metadata["source_url"] = source_url
    return {"success": True, "result": {_BP_PATH: {"metadata": metadata}}}


def _bp_import(suggested: str, raw: str = _BP_YAML) -> dict[str, Any]:
    return {
        "success": True,
        "result": {"suggested_filename": suggested, "raw_data": raw},
    }


class TestBlueprintHandler:
    """blueprint_automation / blueprint_script: the pre-delete snapshot (#2329)."""

    def test_both_domains_are_text_domains(self) -> None:
        assert "blueprint_automation" in bm._TEXT_DOMAINS
        assert "blueprint_script" in bm._TEXT_DOMAINS

    async def test_both_domains_registered_in_defaults(self, tmp_path: Path) -> None:
        mgr = _mk_manager(tmp_path)
        bm.register_default_handlers(mgr, _StubClient())
        for domain in ("blueprint_automation", "blueprint_script"):
            assert mgr.handler_for(domain) is not None, domain
            assert domain in mgr.supported_domains()

    async def test_reads_the_installed_file_through_the_tools_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The installed-file read is the faithful copy — no re-fetch needed."""
        calls: list[Any] = []
        _patch_services(
            monkeypatch, {"read_file": {"success": True, "content": _BP_YAML}}, calls
        )
        client = _BlueprintWsClient(
            {"blueprint/list": _bp_listing(source_url=_BP_SOURCE_URL)}
        )

        assert await bm._fetch_blueprint(client, _BP_PATH, "automation") == _BP_YAML
        assert calls[0][1]["path"] == f"blueprints/automation/{_BP_PATH}"
        # No listing and no re-fetch: a snapshot only ever reads the
        # installed file, so no third-party URL is contacted.
        assert client.sent == []

    async def test_read_uses_the_blueprint_domain_directory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[Any] = []
        _patch_services(
            monkeypatch, {"read_file": {"success": True, "content": _BP_YAML}}, calls
        )
        client = _BlueprintWsClient({"blueprint/list": _bp_listing(source_url=None)})
        await bm._fetch_blueprint(client, _BP_PATH, "script")
        assert calls[0][1]["path"] == f"blueprints/script/{_BP_PATH}"

    async def test_no_installed_file_tier_returns_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_services(
            monkeypatch,
            {"read_file": {"success": False, "error": "File does not exist"}},
            [],
        )
        client = _BlueprintWsClient({})

        assert await bm._fetch_blueprint(client, _BP_PATH, "automation") is None
        assert client.sent == []

    async def test_source_url_is_never_a_snapshot_source(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``get`` may fall back to re-downloading ``source_url``; a snapshot
        must not, because a restore from it could write what the author
        publishes now rather than what the delete destroyed (Codex P1 on
        #2356). With no installed-file tier the capture skips outright."""

        async def refuse(_c: Any, _s: str, _d: dict[str, Any]) -> Any:
            raise ToolError(
                json.dumps(
                    create_error_response(
                        ErrorCode.COMPONENT_NOT_INSTALLED, "entry not set up"
                    )
                )
            )

        monkeypatch.setattr(
            "ha_mcp.tools.tools_filesystem.call_mcp_tools_service", refuse
        )
        client = _BlueprintWsClient(
            {
                "blueprint/list": _bp_listing(source_url=_BP_SOURCE_URL),
                "blueprint/import": _bp_import("user/motion"),
            }
        )

        assert await bm._fetch_blueprint(client, _BP_PATH, "automation") is None
        assert client.frames("blueprint/import") == []
        assert client.frames("blueprint/list") == []

    async def test_restore_sends_blueprint_save(self) -> None:
        client = _BlueprintWsClient({"blueprint/save": {"success": True, "result": {}}})
        await bm._restore_blueprint(client, _BP_PATH, _BP_YAML, "automation")
        assert client.frames("blueprint/save") == [
            {
                "type": "blueprint/save",
                "domain": "automation",
                "path": _BP_PATH,
                "yaml": _BP_YAML,
                "allow_override": True,
            }
        ]

    async def test_restore_raises_on_failure(self) -> None:
        client = _BlueprintWsClient(
            {"blueprint/save": {"success": False, "error": "Invalid blueprint"}}
        )
        with pytest.raises(HomeAssistantError):
            await bm._restore_blueprint(client, _BP_PATH, _BP_YAML, "automation")

    async def test_registered_handler_round_trips_through_the_manager(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The registered handler pair - not the module functions - is what the
        decorator reaches, so drive it through ``handler_for``."""
        _patch_services(
            monkeypatch, {"read_file": {"success": True, "content": _BP_YAML}}, []
        )
        mgr = _mk_manager(tmp_path)
        bm.register_default_handlers(mgr, _StubClient())
        handler = mgr.handler_for("blueprint_script")
        assert handler is not None

        client = _BlueprintWsClient(
            {
                "blueprint/list": _bp_listing(source_url=None),
                "blueprint/save": {"success": True},
            }
        )
        assert await handler.fetch(client, _BP_PATH) == _BP_YAML
        await handler.restore(client, _BP_PATH, _BP_YAML)
        assert client.frames("blueprint/save")[0]["domain"] == "script"

    async def test_fetch_delegates_to_the_shared_tier_ladder(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The snapshot walks the SAME ladder ``action="get"`` walks.

        Two ladders would eventually disagree about which copy of a blueprint is
        authoritative, so this pins the delegation rather than re-testing the
        tiers (``test_blueprint_sources.py`` owns those).
        """
        seen: list[tuple[str, str, str | None]] = []

        async def fake_resolve(
            _client: Any, domain: str, path: str, *, source_url: str | None
        ) -> Any:
            seen.append((domain, path, source_url))
            from ha_mcp.tools.blueprint_sources import BlueprintSource

            return BlueprintSource(_BP_YAML, None, "tools_entry", None)

        monkeypatch.setattr(
            "ha_mcp.tools.blueprint_sources.resolve_blueprint_source", fake_resolve
        )
        client = _BlueprintWsClient(
            {"blueprint/list": _bp_listing(source_url=_BP_SOURCE_URL)}
        )

        assert await bm._fetch_blueprint(client, _BP_PATH, "automation") == _BP_YAML
        # source_url=None: the ladder's re-download tier is withheld from
        # snapshots on purpose.
        assert seen == [("automation", _BP_PATH, None)]


class TestBlueprintDecoratorWiring:
    """``ha_manage_blueprints`` captures only on delete, keyed by blueprint domain."""

    @staticmethod
    def _decorate() -> Any:
        return with_auto_backup(
            domain_fn=lambda kw: f"blueprint_{kw.get('domain') or 'automation'}",
            id_param="path",
            skip_fn=lambda kw: (
                kw.get("action") not in ("delete", "save")
                or (kw.get("action") == "delete" and not kw.get("confirm"))
            ),
            client=object(),
        )(_bp_noop_tool)

    async def _run(
        self, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
    ) -> list[tuple[str, str, str | None]]:
        calls: list[tuple[str, str, str | None]] = []

        class _FakeMgr:
            async def maybe_snapshot(
                self,
                domain: str,
                entity_id: str,
                *,
                tool_name: str | None = None,
                mandatory: bool = False,
            ) -> None:
                calls.append((domain, entity_id, tool_name))

        class _Settings:
            enable_auto_backup = True

        monkeypatch.setattr("ha_mcp.tools.auto_backup.get_global_settings", _Settings)
        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_backup_manager", lambda _c, _s: _FakeMgr()
        )
        assert await self._decorate()(**kwargs) == "ok"
        return calls

    async def test_list_captures_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert await self._run(monkeypatch, action="list", domain="automation") == []

    async def test_get_captures_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = await self._run(
            monkeypatch, action="get", domain="automation", path=_BP_PATH
        )
        assert calls == []

    async def test_unconfirmed_delete_captures_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The tool refuses an unconfirmed delete and changes nothing, so
        capturing for it would pay a component read and, with no component, an
        outbound re-fetch of a third-party source_url for no reason."""
        calls = await self._run(
            monkeypatch,
            action="delete",
            domain="automation",
            path=_BP_PATH,
            confirm=False,
        )
        assert calls == []

    async def test_delete_resolves_the_blueprint_domain_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = await self._run(
            monkeypatch,
            action="delete",
            domain="automation",
            path=_BP_PATH,
            confirm=True,
        )
        assert calls == [("blueprint_automation", _BP_PATH, "_bp_noop_tool")]

    async def test_delete_follows_the_script_domain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = await self._run(
            monkeypatch, action="delete", domain="script", path=_BP_PATH, confirm=True
        )
        assert calls[0][0] == "blueprint_script"

    async def test_save_captures_without_a_confirm_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An overwriting save destroys the previous file, so it captures too.

        ``save`` carries no ``confirm`` parameter — the confirm half of the skip
        test belongs to ``delete`` alone and must not suppress this capture.
        """
        calls = await self._run(
            monkeypatch, action="save", domain="automation", path=_BP_PATH
        )
        assert calls == [("blueprint_automation", _BP_PATH, "_bp_noop_tool")]

    async def test_import_captures_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Import addresses a path core chooses, not the ``path`` argument, so
        the decorator's ``id_param`` would name the wrong file."""
        calls = await self._run(
            monkeypatch, action="import", url="https://example.com/bp.yaml"
        )
        assert calls == []


class TestLegacyList:
    async def test_unavailable_service_degrades_to_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(_c: Any, _s: str, _d: dict[str, Any]) -> Any:
            raise HomeAssistantError("service not found")

        monkeypatch.setattr(
            "ha_mcp.tools.tools_filesystem.call_mcp_tools_service", boom
        )
        backups, reason = await bm._list_legacy_backups(_StubClient())
        assert backups == []
        assert reason == "service not found"

    async def test_tool_error_from_token_gates_degrades_to_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The caller-token gates (tools entry not set up / component too old)
        # raise ToolError, not HomeAssistantError — both the edits list and the
        # legacy read must still degrade instead of failing wholesale, and the
        # surfaced reason must be the human message, not the raw JSON envelope
        # (#1996 exercise path).
        from fastmcp.exceptions import ToolError

        from ha_mcp.errors import ErrorCode, create_error_response

        payload = json.dumps(
            create_error_response(
                ErrorCode.COMPONENT_NOT_INSTALLED,
                "Entry not set up.",
                suggestions=["Add the entry."],
            )
        )

        async def boom(_c: Any, _s: str, _d: dict[str, Any]) -> Any:
            raise ToolError(payload)

        monkeypatch.setattr(
            "ha_mcp.tools.tools_filesystem.call_mcp_tools_service", boom
        )
        backups, reason = await bm._list_legacy_backups(_StubClient())
        assert backups == []
        assert reason == "Entry not set up. Add the entry."
        read = await bm._read_legacy_backup(_StubClient(), "x.bak")
        assert read["success"] is False
        assert read["error"] == "Entry not set up. Add the entry."

    async def test_unsuccessful_response_degrades_to_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_services(monkeypatch, {"list_legacy_backups": {"success": False}}, [])
        assert await bm._list_legacy_backups(_StubClient()) == ([], None)

    async def test_manager_normalizes_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_services(
            monkeypatch,
            {
                "list_legacy_backups": {
                    "success": True,
                    "backups": [
                        {
                            "filename": "configuration.yaml.20260101_120000.bak",
                            "file_path": "configuration.yaml",
                            "path_ambiguous": False,
                            "timestamp": "20260101_120000",
                            "size": 12,
                        }
                    ],
                }
            },
            [],
        )
        mgr = _mk_manager(tmp_path)
        out, reason = await mgr.list_legacy()
        assert reason is None
        assert out[0]["name"] == "legacy:configuration.yaml.20260101_120000.bak"
        assert out[0]["domain"] == "yaml_file"
        assert out[0]["source"] == "legacy"
        assert out[0]["entity_id"] == "configuration.yaml"
        assert out[0]["path_ambiguous"] is False


class TestLegacyRead:
    async def test_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_services(
            monkeypatch,
            {
                "read_legacy_backup": {
                    "success": True,
                    "content": "a: 1\n",
                    "file_path": "configuration.yaml",
                    "path_ambiguous": False,
                }
            },
            [],
        )
        mgr = _mk_manager(tmp_path)
        info = await mgr.read_legacy("configuration.yaml.20260101_120000.bak")
        assert info["content"] == "a: 1\n"

    async def test_not_found_raises_filenotfound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_services(
            monkeypatch,
            {
                "read_legacy_backup": {
                    "success": False,
                    "error": "Backup does not exist: x",
                }
            },
            [],
        )
        mgr = _mk_manager(tmp_path)
        with pytest.raises(FileNotFoundError):
            await mgr.read_legacy("x.bak")

    async def test_other_error_raises_valueerror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_services(
            monkeypatch,
            {
                "read_legacy_backup": {
                    "success": False,
                    "error": "Invalid backup filename",
                }
            },
            [],
        )
        mgr = _mk_manager(tmp_path)
        with pytest.raises(ValueError):
            await mgr.read_legacy("bad")


class TestLegacyDiff:
    async def test_unambiguous_diffs_against_current(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_services(
            monkeypatch,
            {
                "read_legacy_backup": {
                    "success": True,
                    "content": "a: 1\nb: 2\n",
                    "file_path": "configuration.yaml",
                    "path_ambiguous": False,
                    "timestamp": "20260101_120000",
                },
                "read_file": {"success": True, "content": "a: 1\n"},
            },
            [],
        )
        mgr = _mk_manager(tmp_path)
        diff = await mgr.diff_snapshot("legacy:configuration.yaml.20260101_120000.bak")
        assert diff["kind"] == "text"
        assert diff["entity_missing"] is False
        assert "b: 2" in diff["diff"]

    async def test_ambiguous_is_entity_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # file_path None / ambiguous → no trustworthy live target → entity_missing,
        # and read_file must NOT be called (no response stubbed for it).
        _patch_services(
            monkeypatch,
            {
                "read_legacy_backup": {
                    "success": True,
                    "content": "a: 1\n",
                    "file_path": None,
                    "path_ambiguous": True,
                    "timestamp": None,
                }
            },
            [],
        )
        mgr = _mk_manager(tmp_path)
        diff = await mgr.diff_snapshot("legacy:weird_name.bak")
        assert diff["entity_missing"] is True


class TestLegacyRestore:
    async def test_happy_path_replace_file_with_safety(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[Any] = []
        _patch_services(
            monkeypatch,
            {
                "read_legacy_backup": {
                    "success": True,
                    "content": "a: 1\n",
                    "file_path": "configuration.yaml",
                    "path_ambiguous": False,
                    "timestamp": "20260101_120000",
                },
                "read_file": {"success": True, "content": "a: OLD\n"},
                "edit_yaml_config": {"success": True},
            },
            calls,
        )
        mgr = _mk_manager(tmp_path)
        bm.register_default_handlers(mgr, _StubClient())
        result = await mgr.restore_snapshot(
            "legacy:configuration.yaml.20260101_120000.bak"
        )
        assert (
            result["restored_from"] == "legacy:configuration.yaml.20260101_120000.bak"
        )
        assert result["domain"] == "yaml_file"
        assert result["entity_id"] == "configuration.yaml"
        # Pre-restore safety snapshot of the current whole file was captured...
        assert result["safety_backup"] is not None
        # ...and it actually landed on disk (mandatory — not just a metadata
        # field): a real yaml_file snapshot holding the prior content.
        safety_files = list(tmp_path.glob("yaml_file.*.yaml"))
        assert len(safety_files) == 1, safety_files
        assert safety_files[0].name == result["safety_backup"]
        assert "a: OLD" in safety_files[0].read_text()
        # The restore wrote via replace_file, not write_file.
        assert any(
            s == "edit_yaml_config" and d["action"] == "replace_file" for s, d in calls
        )

    async def test_ambiguous_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_services(
            monkeypatch,
            {
                "read_legacy_backup": {
                    "success": True,
                    "content": "a: 1\n",
                    "file_path": None,
                    "path_ambiguous": True,
                    "timestamp": None,
                }
            },
            [],
        )
        mgr = _mk_manager(tmp_path)
        bm.register_default_handlers(mgr, _StubClient())
        with pytest.raises(ValueError, match="unambiguously"):
            await mgr.restore_snapshot("legacy:weird.bak")

    async def test_missing_content_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_services(
            monkeypatch,
            {
                "read_legacy_backup": {
                    "success": True,
                    "content": None,
                    "file_path": "configuration.yaml",
                    "path_ambiguous": False,
                }
            },
            [],
        )
        mgr = _mk_manager(tmp_path)
        bm.register_default_handlers(mgr, _StubClient())
        with pytest.raises(ValueError):
            await mgr.restore_snapshot("legacy:configuration.yaml.20260101_120000.bak")

    async def test_safety_capture_failure_blocks_restore(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuine safety-snapshot failure fails the restore CLOSED — the
        MandatoryBackupError propagates (not swallowed), so the tool layer maps
        it to BACKUP_CAPTURE_FAILED and nothing is overwritten un-backed-up."""
        _patch_services(
            monkeypatch,
            {
                "read_legacy_backup": {
                    "success": True,
                    "content": "a: 1\n",
                    "file_path": "configuration.yaml",
                    "path_ambiguous": False,
                    "timestamp": "20260101_120000",
                }
            },
            [],
        )
        mgr = _mk_manager(tmp_path)
        bm.register_default_handlers(mgr, _StubClient())

        async def _boom(*_a: Any, **_k: Any) -> Any:
            raise bm.MandatoryBackupError("backup dir unusable")

        monkeypatch.setattr(mgr, "maybe_snapshot", _boom)
        with pytest.raises(bm.MandatoryBackupError):
            await mgr.restore_snapshot("legacy:configuration.yaml.20260101_120000.bak")


class TestListEditsAndLegacy:
    """list_edits_and_legacy merges edits snapshots + legacy entries (#1579)."""

    async def test_merges_when_unfiltered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mgr = _mk_manager(tmp_path)
        monkeypatch.setattr(
            mgr,
            "list_snapshots",
            lambda **_kw: [{"name": "file.x.20260101_000000.yaml", "domain": "file"}],
        )

        async def _legacy() -> tuple[list[Any], str | None]:
            return [{"name": "legacy:configuration.yaml.20200101_000000.bak"}], None

        monkeypatch.setattr(mgr, "list_legacy", _legacy)
        entries, warnings = await mgr.list_edits_and_legacy()
        names = [e["name"] for e in entries]
        assert "file.x.20260101_000000.yaml" in names
        assert "legacy:configuration.yaml.20200101_000000.bak" in names
        assert warnings == []

    async def test_legacy_skipped_when_filtered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mgr = _mk_manager(tmp_path)
        monkeypatch.setattr(mgr, "list_snapshots", lambda **_kw: [])

        async def _legacy() -> tuple[list[Any], str | None]:
            return [{"name": "legacy:x.bak"}], None

        monkeypatch.setattr(mgr, "list_legacy", _legacy)
        # A non-yaml_file domain filter excludes legacy (legacy maps to yaml_file).
        assert await mgr.list_edits_and_legacy(domain="automation") == ([], [])
        # An entity_id filter excludes legacy (decoded path != sanitized filter).
        assert await mgr.list_edits_and_legacy(entity_id="foo") == ([], [])
        # Unfiltered (or explicit yaml_file) surfaces it.
        out, _ = await mgr.list_edits_and_legacy()
        assert any(e["name"] == "legacy:x.bak" for e in out)
        out2, _ = await mgr.list_edits_and_legacy(domain="yaml_file")
        assert any(e["name"] == "legacy:x.bak" for e in out2)

    async def test_limit_reserves_room_for_legacy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mgr = _mk_manager(tmp_path)

        # list_snapshots honours the limit it's handed (mimics a full edits
        # store): without reserved room the legacy entries get truncated out.
        def _snaps(**kw: Any) -> list[Any]:
            n = kw.get("limit") or 100
            return [{"name": f"file.e{i}.20260101_000000.yaml"} for i in range(n)]

        monkeypatch.setattr(mgr, "list_snapshots", _snaps)

        async def _legacy() -> tuple[list[Any], str | None]:
            return [{"name": "legacy:a.bak"}, {"name": "legacy:b.bak"}], None

        monkeypatch.setattr(mgr, "list_legacy", _legacy)
        out, _ = await mgr.list_edits_and_legacy(limit=4)
        assert len(out) == 4
        names = {e["name"] for e in out}
        # Legacy survives the cap (room reserved): 2 edits + 2 legacy.
        assert "legacy:a.bak" in names and "legacy:b.bak" in names

    async def test_suppressed_legacy_fetch_surfaces_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When the legacy sub-fetch is unavailable (tools entry not set up /
        # component too old), the merged list must carry a warning instead of
        # reading as a clean, complete listing (#1996).
        mgr = _mk_manager(tmp_path)
        monkeypatch.setattr(mgr, "list_snapshots", lambda **_kw: [])

        async def _legacy() -> tuple[list[Any], str | None]:
            return [], "Entry not set up. Add the entry."

        monkeypatch.setattr(mgr, "list_legacy", _legacy)
        entries, warnings = await mgr.list_edits_and_legacy()
        assert entries == []
        assert len(warnings) == 1
        assert "omitted" in warnings[0]
        assert "Entry not set up. Add the entry." in warnings[0]

    async def test_warnings_reach_the_tool_response(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Top-level warnings contract (.gemini/styleguide.md Tool Tags and
        # Return Values): present when legacy fetch is suppressed; absent when clean.
        from ha_mcp.tools.backup import _edits_list

        mgr = _mk_manager(tmp_path)
        monkeypatch.setattr(mgr, "list_snapshots", lambda **_kw: [])

        async def _legacy_down() -> tuple[list[Any], str | None]:
            return [], "Entry not set up."

        monkeypatch.setattr(mgr, "list_legacy", _legacy_down)
        out = await _edits_list(mgr, _StubSettings(), None, None, 50)
        assert out["success"] is True
        assert "Entry not set up." in out["warnings"][0]

        async def _legacy_ok() -> tuple[list[Any], str | None]:
            return [], None

        monkeypatch.setattr(mgr, "list_legacy", _legacy_ok)
        out = await _edits_list(mgr, _StubSettings(), None, None, 50)
        assert "warnings" not in out
