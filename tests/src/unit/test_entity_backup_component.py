"""Entity backups restored through the component's in-process write.

The fake component in ``test_entity_backup.py`` answers ``entity_entry`` and
``entity_restore``; these tests cover what only that path does and how the
server reads the component's answers. The tests both paths share live there.
"""

from pathlib import Path
from typing import Any

import pytest

from ha_mcp import backup_entities, backup_manager
from ha_mcp.backup_entities import _NOT_RESTORED
from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantError,
)

from .test_entity_backup import FakeRegistry, _manager, _registry, _writes
from .test_zone_backup import _handler


@pytest.fixture
def component_registry(monkeypatch: pytest.MonkeyPatch) -> FakeRegistry:
    return _registry(monkeypatch, "component")


class TestComponentRestore:
    @pytest.mark.parametrize(
        ("field", "value"), [("hidden_by", "integration"), ("disabled_by", "hass")]
    )
    async def test_the_component_writes_a_hider_only_core_sets(
        self, component_registry: FakeRegistry, field: str, value: str
    ) -> None:
        registry = component_registry
        registry.entries["light.desk"][field] = value
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.entries["light.desk"][field] = None

        result = await handler.restore(registry, "light.desk", snapshot)

        assert registry.entries["light.desk"][field] == value
        assert result["warnings"] == [_NOT_RESTORED]
        [write] = _writes(registry)
        assert write["settings"][field] == value

    async def test_a_disabler_change_stays_in_the_component_write(
        self, component_registry: FakeRegistry
    ) -> None:
        """Only an enable goes through Core's update; Core would refuse "hass"."""
        registry = component_registry
        registry.entries["light.desk"]["disabled_by"] = "hass"
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.edit("light.desk", disabled_by="user")

        result = await handler.restore(registry, "light.desk", snapshot)

        assert registry.entries["light.desk"]["disabled_by"] == "hass"
        assert result["warnings"] == [_NOT_RESTORED]
        assert [w["type"] for w in _writes(registry)] == ["ha_mcp_tools/entity_restore"]

    async def test_the_components_warnings_reach_the_result(
        self, component_registry: FakeRegistry
    ) -> None:
        registry = component_registry
        registry.entries["light.desk"]["disabled_by"] = "device"
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.entries["light.desk"]["disabled_by"] = "user"

        result = await handler.restore(registry, "light.desk", snapshot)

        assert registry.entries["light.desk"]["disabled_by"] == "user"
        assert result["warnings"] == [
            _NOT_RESTORED,
            "disabled_by stays: the device is not disabled now",
        ]

    async def test_the_component_enables_through_cores_update(
        self, component_registry: FakeRegistry
    ) -> None:
        """The server leaves disabled_by None out of the component write and
        enables through Core's update, which answers with its restart flag."""
        registry = component_registry
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.edit("light.desk", disabled_by="user", name="Desk")

        result = await handler.restore(registry, "light.desk", snapshot)

        assert await handler.fetch(None, "light.desk") == snapshot
        assert result["require_restart"] is True
        assert [w["type"] for w in _writes(registry)] == [
            "ha_mcp_tools/entity_restore",
            "config/entity_registry/update",
        ]
        assert "disabled_by" not in _writes(registry)[0]["settings"]

    @pytest.mark.parametrize(
        ("deleted", "suggestion"),
        [
            (
                {
                    "entity_id": "light.desk",
                    "config_entry_id": "ce1",
                    "kept_until": None,
                },
                "Home Assistant keeps the registry settings of "
                "light.desk while its config entry exists",
            ),
            (
                {
                    "entity_id": "light.desk",
                    "config_entry_id": None,
                    "kept_until": "2026-11-26T10:00:00+00:00",
                },
                "Home Assistant keeps the registry settings of "
                "light.desk until 2026-11-26T10:00:00+00:00",
            ),
            (None, "Home Assistant no longer keeps this entity's registry settings"),
        ],
    )
    async def test_the_component_says_what_core_keeps_of_a_removed_entity(
        self,
        component_registry: FakeRegistry,
        deleted: dict[str, Any] | None,
        suggestion: str,
    ) -> None:
        registry = component_registry
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        del registry.entries["light.desk"]
        registry.deleted["reg-desk"] = deleted

        with pytest.raises(backup_manager.BackupRestoreError) as err:
            await handler.restore(registry, "light.desk", snapshot)

        assert err.value.outcome["reason"] == "entry_deleted"
        assert err.value.outcome["suggestions"][0].startswith(suggestion)
        assert _writes(registry) == []

    async def test_an_entry_removed_before_the_component_write_is_reported(
        self, component_registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = component_registry
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.edit("light.desk", name="Desk")
        sent = registry.ws

        async def remove_before_write(client: Any, msg: dict[str, Any]) -> Any:
            if msg["type"] == "ha_mcp_tools/entity_restore":
                del registry.entries["light.desk"]
            return await sent(client, msg)

        monkeypatch.setattr(backup_manager, "_ws_send", remove_before_write)

        with pytest.raises(backup_manager.BackupRestoreError) as err:
            await handler.restore(registry, "light.desk", snapshot)

        assert err.value.outcome["reason"] == "entry_deleted"

    async def test_a_core_refusal_in_the_component_writes_nothing(
        self, component_registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registry = component_registry
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.edit("light.desk", name="Desk")
        sent = registry.ws

        async def refuse(client: Any, msg: dict[str, Any]) -> Any:
            if msg["type"] == "ha_mcp_tools/entity_restore":
                error = {"code": "invalid", "message": "no own name"}
                return {"success": False, "error": error}
            return await sent(client, msg)

        monkeypatch.setattr(backup_manager, "_ws_send", refuse)

        with pytest.raises(backup_manager.BackupRestoreError) as err:
            await handler.restore(registry, "light.desk", snapshot)

        assert err.value.outcome["reason"] == "rejected_by_home_assistant"
        assert err.value.outcome["apply_status"] == "not_applied"
        assert "no own name" in str(err.value)
        assert registry.entries["light.desk"]["name"] == "Desk"

    @pytest.mark.parametrize(
        ("error", "reason", "apply_status"),
        [
            (
                {"code": "entity_identity_mismatch"},
                "entity_identity_mismatch",
                "not_applied",
            ),
            ({"code": "from_a_newer_component"}, "upstream_error", "unknown"),
            ({}, "upstream_error", "unknown"),
        ],
    )
    async def test_a_component_refusal_says_what_it_knows_was_written(
        self,
        component_registry: FakeRegistry,
        monkeypatch: pytest.MonkeyPatch,
        error: dict[str, Any],
        reason: str,
        apply_status: str,
    ) -> None:
        registry = component_registry
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        sent = registry.ws

        async def refuse(client: Any, msg: dict[str, Any]) -> Any:
            if msg["type"] == "ha_mcp_tools/entity_restore":
                return {"success": False, "error": error}
            return await sent(client, msg)

        monkeypatch.setattr(backup_manager, "_ws_send", refuse)

        with pytest.raises(backup_manager.BackupRestoreError) as err:
            await handler.restore(registry, "light.desk", snapshot)

        assert err.value.outcome["reason"] == reason
        assert err.value.outcome["apply_status"] == apply_status

    @pytest.mark.parametrize(
        "failure",
        [
            HomeAssistantCommandError("Command failed: boom", "unknown_error"),
            HomeAssistantCommandTimeout("ha_mcp_tools/entity_restore timed out"),
        ],
    )
    async def test_an_error_after_the_component_write_was_sent_is_unknown(
        self,
        component_registry: FakeRegistry,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        failure: Exception,
    ) -> None:
        """The write may have run, so Core's commands do not write it again."""
        registry = component_registry
        mgr = _manager(tmp_path, registry)
        path = await mgr.maybe_snapshot("entity", "light.desk", tool_name="t")
        assert path is not None
        registry.edit("light.desk", name="Desk")
        sent = registry.ws

        async def fail(client: Any, msg: dict[str, Any]) -> Any:
            if msg["type"] == "ha_mcp_tools/entity_restore":
                registry.sent.append(msg)
                raise failure
            return await sent(client, msg)

        monkeypatch.setattr(backup_manager, "_ws_send", fail)

        with pytest.raises(type(failure)) as err:
            await mgr.restore_snapshot(path.name)

        assert err.value.restore_outcome["apply_status"] == "unknown"
        assert [w["type"] for w in _writes(registry)] == ["ha_mcp_tools/entity_restore"]

    async def test_an_empty_component_reply_is_not_followed_by_core_writes(
        self, component_registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The component ran; only its reply is missing."""
        registry = component_registry
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.edit("light.desk", name="Desk")
        sent = registry.ws

        async def empty(client: Any, msg: dict[str, Any]) -> Any:
            if msg["type"] == "ha_mcp_tools/entity_restore":
                registry.sent.append(msg)
                return None
            return await sent(client, msg)

        monkeypatch.setattr(backup_manager, "_ws_send", empty)

        with pytest.raises(HomeAssistantError):
            await handler.restore(registry, "light.desk", snapshot)

        assert [w["type"] for w in _writes(registry)] == ["ha_mcp_tools/entity_restore"]

    async def test_a_component_without_the_commands_falls_back(
        self, component_registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An older component answers unknown_command; Core's commands restore,
        and the capabilities are probed again next time."""
        registry = component_registry
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.edit("light.desk", name="Desk", categories={"script": "cat-s"})
        sent = registry.ws
        invalidated: list[Any] = []

        async def unknown(client: Any, msg: dict[str, Any]) -> Any:
            if msg["type"].startswith("ha_mcp_tools/"):
                raise HomeAssistantCommandError(
                    "Command failed: Unknown command", "unknown_command"
                )
            return await sent(client, msg)

        monkeypatch.setattr(backup_manager, "_ws_send", unknown)
        monkeypatch.setattr(backup_entities, "invalidate_caps", invalidated.append)

        await handler.restore(registry, "light.desk", snapshot)

        assert await handler.fetch(None, "light.desk") == snapshot
        assert invalidated == [registry, registry]
        assert "config/entity_registry/list" in [m["type"] for m in registry.sent]
