"""Entity backups restore the registry settings ``ha_set_entity`` changes.

The fake registry follows Core's ``config/entity_registry/update``: only the
category scopes it is sent change, hidden_by/disabled_by take only None or
"user", and a rename keeps the entry's ``id``. Each edit below is the update
``ha_set_entity`` sends; restoring the backup taken before it must give back
the snapshot exactly.
"""

import copy
from pathlib import Path
from typing import Any

import pytest

from ha_mcp import backup_manager
from ha_mcp.backup_entities import _NOT_RESTORED, current_entity_id
from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tools.tools_entities import _single_entity_target

from .test_backup_manager import _StubSettings
from .test_zone_backup import _handler

DESK = {
    "id": "reg-desk",
    "entity_id": "light.desk",
    "platform": "hue",
    "unique_id": "u-desk",
    "name": None,
    "icon": None,
    "area_id": "office",
    "device_class": None,
    "aliases": ["desk lamp"],
    "labels": ["work"],
    "categories": {"automation": "cat-a"},
    "hidden_by": None,
    "disabled_by": None,
    "options": {"light": {"favorite_colors": []}},
}
# Core lists entries without these; config/entity_registry/get includes them.
_GET_ONLY = ("aliases", "device_class")


class FakeRegistry:
    def __init__(self, *entries: dict[str, Any]) -> None:
        self.entries = {e["entity_id"]: copy.deepcopy(e) for e in entries}
        self.sent: list[dict[str, Any]] = []

    async def ws(self, _client: Any, msg: dict[str, Any]) -> Any:
        self.sent.append(copy.deepcopy(msg))
        if msg["type"] == "config/entity_registry/list":
            return [
                {k: v for k, v in e.items() if k not in _GET_ONLY}
                for e in self.entries.values()
            ]
        # Core's schema refuses a missing or malformed entity_id outright.
        assert isinstance(msg["entity_id"], str)
        entry = self.entries.get(msg["entity_id"])
        if entry is None:
            raise HomeAssistantCommandError(
                "Command failed: Entity not found", "not_found"
            )
        if msg["type"] == "config/entity_registry/get":
            return copy.deepcopy(entry)
        assert msg["type"] == "config/entity_registry/update"
        return {"entity_entry": self._update(entry, msg)}

    def _update(self, entry: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("new_entity_id") in self.entries:
            raise HomeAssistantCommandError(
                "Command failed: Entity with this ID is already registered",
                "invalid_info",
            )
        for field in ("hidden_by", "disabled_by"):
            if field in msg and msg[field] not in (None, "user"):
                raise HomeAssistantCommandError(
                    "Command failed: invalid", "invalid_format"
                )
        for key, value in msg.items():
            if key == "categories":
                merged = {**entry["categories"], **value}
                entry["categories"] = {s: c for s, c in merged.items() if c is not None}
            elif key not in ("type", "entity_id", "new_entity_id"):
                entry[key] = value
        if (new := msg.get("new_entity_id")) is not None:
            del self.entries[entry["entity_id"]]
            entry["entity_id"] = new
            self.entries[new] = entry
        return copy.deepcopy(entry)

    def edit(self, entity_id: str, **fields: Any) -> None:
        self._update(self.entries[entity_id], fields)


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> FakeRegistry:
    fake = FakeRegistry(DESK)
    monkeypatch.setattr(backup_manager, "_ws_send", fake.ws)
    return fake


def _manager(tmp_path: Path) -> backup_manager.BackupManager:
    mgr = backup_manager.BackupManager(
        _StubSettings(auto_backup_dir=str(tmp_path)), object()
    )
    backup_manager.register_default_handlers(mgr, None)
    return mgr


class TestEntityBackup:
    @pytest.mark.parametrize(
        "edit",
        [
            {"name": "Desk"},
            {"name": ""},  # use_device_name=True
            {"icon": "mdi:lamp"},
            {"area_id": "kitchen"},
            {"area_id": None},
            {"device_class": "outlet"},
            {"aliases": [None, "lamp"]},
            {"labels": []},
            {"categories": {"automation": "cat-b"}},
            {"categories": {"script": "cat-s"}},
            {"categories": {"automation": None}},
            {"hidden_by": "user"},
            {"disabled_by": "user"},
            {"new_entity_id": "light.lamp"},
            {"new_entity_id": "light.lamp", "name": "Lamp", "hidden_by": "user"},
        ],
    )
    async def test_restore_gives_back_the_snapshot(
        self, registry: FakeRegistry, edit: dict[str, Any]
    ) -> None:
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.edit("light.desk", **edit)

        result = await handler.restore(None, "light.desk", snapshot)

        assert await handler.fetch(None, "light.desk") == snapshot
        assert result["warnings"] == [_NOT_RESTORED]

    async def test_snapshot_holds_the_registry_fields_only(
        self, registry: FakeRegistry
    ) -> None:
        """Options are not restored, so a diff must not show them either."""
        snapshot = await _handler("entity").fetch(None, "light.desk")

        assert snapshot == {
            k: v
            for k, v in DESK.items()
            if k not in ("platform", "unique_id", "options")
        }

    async def test_an_entity_without_a_registry_entry_has_no_snapshot(
        self, registry: FakeRegistry
    ) -> None:
        assert await _handler("entity").fetch(None, "light.yaml_only") is None

    @pytest.mark.parametrize(
        ("saved", "now", "warning"),
        [
            (
                "integration",
                None,
                "hidden_by stays None: only Home Assistant itself sets 'integration'",
            ),
            (
                None,
                "integration",
                "hidden_by stays 'integration': only Home Assistant itself sets 'integration'",
            ),
            ("integration", "integration", None),
        ],
    )
    async def test_a_hider_only_core_sets_is_left_and_named(
        self,
        registry: FakeRegistry,
        saved: str | None,
        now: str | None,
        warning: str | None,
    ) -> None:
        """Core rejects any other value than None or "user", which would fail
        the whole restore."""
        registry.entries["light.desk"]["hidden_by"] = saved
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.entries["light.desk"]["hidden_by"] = now
        registry.edit("light.desk", name="Desk")

        result = await handler.restore(None, "light.desk", snapshot)

        entry = registry.entries["light.desk"]
        assert (entry["hidden_by"], entry["name"]) == (now, None)
        assert result["warnings"] == [_NOT_RESTORED, *([warning] if warning else [])]

    async def test_a_registry_read_error_fails_the_snapshot(
        self, registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a missing entry means "nothing to back up"; any other error must
        reach the capture, which blocks a restore without its safety backup."""

        async def failing(_client: Any, _msg: dict[str, Any]) -> Any:
            raise HomeAssistantCommandError("Command failed: boom", "unknown_error")

        monkeypatch.setattr(backup_manager, "_ws_send", failing)

        with pytest.raises(HomeAssistantCommandError):
            await _handler("entity").fetch(None, "light.desk")

    async def test_a_removed_entity_points_to_reloading_its_integration(
        self, registry: FakeRegistry
    ) -> None:
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        del registry.entries["light.desk"]

        with pytest.raises(backup_manager.BackupRestoreError) as err:
            await handler.restore(None, "light.desk", snapshot)

        assert err.value.outcome["reason"] == "entry_deleted"
        assert "Reload the integration" in err.value.outcome["suggestions"][0]
        assert not [m for m in registry.sent if m["type"].endswith("update")]

    @pytest.mark.parametrize(
        "legacy",
        [{"entity_id": "light.desk", "state": "on", "attributes": {}}, "on"],
    )
    async def test_a_state_snapshot_from_an_earlier_version_is_refused(
        self, registry: FakeRegistry, legacy: Any
    ) -> None:
        """It holds no registry settings; re-posting its state would only
        overwrite what Home Assistant shows for the entity."""

        with pytest.raises(backup_manager.BackupRestoreError) as err:
            await _handler("entity").restore(None, "light.desk", legacy)

        assert err.value.outcome["reason"] == "invalid_snapshot"
        assert registry.sent == []


class TestRenamedWhileWaiting:
    @pytest.mark.parametrize("taken", [False, True])
    async def test_a_rename_between_lookup_and_lock_writes_nothing(
        self, registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch, taken: bool
    ) -> None:
        """Without the re-read the update would go to whatever now holds the
        entity_id: nothing, or another entity."""
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        listed = registry.ws

        async def rename_after_list(client: Any, msg: dict[str, Any]) -> Any:
            result = await listed(client, msg)
            if msg["type"] == "config/entity_registry/list":
                registry.edit("light.desk", new_entity_id="light.lamp")
                if taken:
                    registry.entries["light.desk"] = {**DESK, "id": "reg-other"}
            return result

        monkeypatch.setattr(backup_manager, "_ws_send", rename_after_list)

        with pytest.raises(backup_manager.BackupRestoreError) as err:
            await handler.restore(None, "light.desk", snapshot)

        assert err.value.outcome["reason"] == "entity_identity_mismatch"
        assert not [m for m in registry.sent if m["type"].endswith("update")]

    async def test_a_rename_back_holds_both_entity_locks_in_order(
        self, registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.edit("light.desk", new_entity_id="light.lamp")
        held: list[str] = []
        real_lock = backup_manager.registry_update_lock

        def recording_lock(kind: str, entity_id: str) -> Any:
            held.append(f"{kind}:{entity_id}")
            return real_lock(kind, entity_id)

        monkeypatch.setattr(backup_manager, "registry_update_lock", recording_lock)

        await handler.restore(None, "light.desk", snapshot)

        assert held == ["entity:light.desk", "entity:light.lamp"]


async def test_the_current_entity_id_follows_the_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot without an entry id keeps its entity_id; a gone entry has none."""
    fake = FakeRegistry({**DESK, "entity_id": "light.lamp"})
    monkeypatch.setattr(backup_manager, "_ws_send", fake.ws)

    assert await current_entity_id(None, "light.desk", DESK) == "light.lamp"
    assert await current_entity_id(None, "light.desk", {"state": "on"}) == "light.desk"
    assert await current_entity_id(None, "light.desk", {**DESK, "id": "x"}) is None


class TestRenamedEntity:
    """The backup is keyed by the entity_id the entity had; after a rename the
    safety backup and the diff must still find it by its entry id."""

    async def test_restore_backs_up_the_renamed_entity_first(
        self, registry: FakeRegistry, tmp_path: Path
    ) -> None:
        mgr = _manager(tmp_path)
        path = await mgr.maybe_snapshot("entity", "light.desk", tool_name="t")
        assert path is not None
        registry.edit("light.desk", new_entity_id="light.lamp", area_id="kitchen")

        outcome = await mgr.restore_snapshot(path.name)

        safety = mgr.read_snapshot(outcome["safety_backup"])
        assert (safety["entity_id"], safety["config"]["area_id"]) == (
            "light.lamp",
            "kitchen",
        )
        assert registry.entries["light.desk"]["area_id"] == "office"

    async def test_diff_compares_with_the_renamed_entity(
        self, registry: FakeRegistry, tmp_path: Path
    ) -> None:
        mgr = _manager(tmp_path)
        path = await mgr.maybe_snapshot("entity", "light.desk", tool_name="t")
        assert path is not None
        registry.edit("light.desk", new_entity_id="light.lamp")

        _, current = await mgr.snapshot_comparison(path.name)

        assert current is not None and current["entity_id"] == "light.lamp"

    async def test_diff_of_a_removed_entity_has_no_current_entry(
        self, registry: FakeRegistry, tmp_path: Path
    ) -> None:
        mgr = _manager(tmp_path)
        path = await mgr.maybe_snapshot("entity", "light.desk", tool_name="t")
        assert path is not None
        del registry.entries["light.desk"]

        _, current = await mgr.snapshot_comparison(path.name)

        assert current is None

    async def test_an_entity_now_holding_the_old_id_stops_the_rename_back(
        self, registry: FakeRegistry, tmp_path: Path
    ) -> None:
        """Core refuses the rename onto a taken ID before it writes anything, so
        neither entity changes."""
        mgr = _manager(tmp_path)
        path = await mgr.maybe_snapshot("entity", "light.desk", tool_name="t")
        assert path is not None
        registry.edit("light.desk", new_entity_id="light.lamp", area_id="kitchen")
        registry.entries["light.desk"] = {**DESK, "id": "reg-other"}

        with pytest.raises(HomeAssistantCommandError):
            await mgr.restore_snapshot(path.name)

        assert registry.entries["light.lamp"]["area_id"] == "kitchen"
        assert registry.entries["light.desk"]["id"] == "reg-other"


def test_a_bulk_call_backs_up_no_entity() -> None:
    """Snapshotting the first entity of a list would leave the rest without a
    backup while looking covered, so a list names no entity. A list of
    one is a single edit to ha_set_entity."""
    assert _single_entity_target({"entity_id": ["light.a", "light.b"]}) == ""
    assert _single_entity_target({"entity_id": "light.a"}) == "light.a"
    assert _single_entity_target({"entity_id": ["light.a"]}) == "light.a"
    src = (
        Path(__file__).resolve().parents[3] / "src/ha_mcp/tools/tools_entities.py"
    ).read_text(encoding="utf-8")
    for tool in ("ha_set_entity", "ha_remove_entity"):
        head = src[: src.index(f"async def {tool}(")][-200:]
        assert "id_fn=_single_entity_target" in head, tool
