"""Entity backups restore the registry settings ``ha_set_entity`` changes.

The fake registry follows Core 2026.10's ``config/entity_registry/update``:
only the category scopes it is sent change, hidden_by/disabled_by take only
None or "user", an update carrying disabled_by None asks for a restart (as for
an entity without a config entry), options are set one domain at a time, and
a rename keeps the entry's ``id``. Each edit below is an update
``ha_set_entity`` sends; restoring the backup taken before it must give back
the snapshot exactly.

The ``registry`` fixture runs each test twice: on Core's commands alone, and
with the component's ``entity_entry`` and ``entity_restore``, which the fake
answers the way the component's in-process write does: hidden_by and
disabled_by values other than "user", the whole categories dict, a device or
config_entry disabler only while that parent is disabled, and no enable.
"""

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ha_mcp import backup_entities, backup_manager
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.backup_entities import _NOT_RESTORED, current_entity_id, restore_entity
from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
)
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
    "options": {
        "light": {"favorite_colors": []},
        "conversation": {"should_expose": False},
        "cloud.alexa": {"should_expose": True},
    },
}
# Core lists entries without these; config/entity_registry/get includes them.
_GET_ONLY = ("aliases", "device_class")


class FakeRegistry:
    """The entity registry, and the client the restore reads the area, label
    and category registries and the states through."""

    def __init__(self, *entries: dict[str, Any]) -> None:
        self.entries = {e["entity_id"]: copy.deepcopy(e) for e in entries}
        self.sent: list[dict[str, Any]] = []
        self.ids = {
            "area": {"office", "kitchen"},
            "label": {"work"},
            "category": {"cat-a", "cat-b", "cat-s"},
        }
        self.unreadable: set[str] = set()
        self.path = "websocket"
        # What Core keeps of removed entries, by entry id.
        self.deleted: dict[str, dict[str, Any] | None] = {}

    async def caps(self, _client: Any) -> Any:
        if self.path == "websocket":
            return None
        return SimpleNamespace(capabilities=["entity_entry", "entity_restore"])

    async def send_websocket_message(self, msg: dict[str, Any]) -> Any:
        name = msg["type"].split("/")[1].removesuffix("_registry")
        if name in self.unreadable:
            raise ConnectionError(f"{name} registry unavailable")
        return [{f"{name}_id": item} for item in self.ids[name]]

    async def get_states(self) -> list[dict[str, Any]]:
        return [{"entity_id": entity_id} for entity_id in self.entries]

    async def ws(self, _client: Any, msg: dict[str, Any]) -> Any:
        self.sent.append(copy.deepcopy(msg))
        if msg["type"].startswith("ha_mcp_tools/"):
            return self._component(msg)
        if msg["type"] == "config/entity_registry/list":
            return [
                {k: v for k, v in e.items() if k not in _GET_ONLY}
                for e in self.entries.values()
            ]
        if msg["type"] == "homeassistant/expose_entity":
            for entity_id in msg["entity_ids"]:
                options = self.entries[entity_id]["options"]
                for assistant in msg["assistants"]:
                    options[assistant] = {
                        **options.get(assistant, {}),
                        "should_expose": msg["should_expose"],
                    }
            return None
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
        result: dict[str, Any] = {"entity_entry": self._update(entry, msg)}
        if "disabled_by" in msg and msg["disabled_by"] is None:
            result["require_restart"] = True
        return result

    def _update(self, entry: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any]:
        self._refuse(entry, msg)
        for key, value in msg.items():
            if key == "categories":
                merged = {**entry["categories"], **value}
                entry["categories"] = {s: c for s, c in merged.items() if c is not None}
            elif key == "options_domain":
                options = {d: o for d, o in entry["options"].items() if d != value}
                if msg["options"] is not None:
                    options[value] = msg["options"]
                entry["options"] = options
            elif key not in ("type", "entity_id", "new_entity_id", "options"):
                entry[key] = value
        if (new := msg.get("new_entity_id")) is not None:
            del self.entries[entry["entity_id"]]
            entry["entity_id"] = new
            self.entries[new] = entry
        return copy.deepcopy(entry)

    def _refuse(self, entry: dict[str, Any], msg: dict[str, Any]) -> None:
        """The updates Core's handler refuses before it writes anything."""
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
        if (
            "disabled_by" in msg
            and msg["disabled_by"] is None
            and entry.get("device_disabled")
        ):
            raise HomeAssistantCommandError(
                "Command failed: Device is disabled", "invalid_info"
            )

    def edit(self, entity_id: str, **fields: Any) -> None:
        self._update(self.entries[entity_id], fields)

    def _component(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Stands in for the component: applies the settings, rename and
        options to the fake entry in one step, refuses a taken new_entity_id,
        and leaves out a device disabler, with a warning, unless
        ``device_disabled`` is set on the entry."""
        entry = next(
            (e for e in self.entries.values() if e["id"] == msg["entry_id"]), None
        )
        deleted = self.deleted.get(msg["entry_id"])
        if msg["type"] == "ha_mcp_tools/entity_entry":
            if entry is None:
                return {"entry": None, "deleted": deleted}
            return {"entry": {"id": entry["id"], "entity_id": entry["entity_id"]}}
        assert msg["type"] == "ha_mcp_tools/entity_restore"
        assert "disabled_by" not in msg["settings"] or msg["settings"]["disabled_by"]
        if entry is None:
            error = {"code": "entry_deleted", "message": "gone", "deleted": deleted}
            return {"success": False, "error": error}
        if entry["entity_id"] != msg["entity_id"]:
            error = {"code": "entity_identity_mismatch", "message": "renamed"}
            return {"success": False, "error": error}
        if msg.get("new_entity_id") in self.entries:
            error = {"code": "invalid", "message": "Entity ID is already registered"}
            return {"success": False, "error": error}
        settings = copy.deepcopy(msg["settings"])
        warnings = []
        if settings.get("disabled_by") == "device" and not entry.get("device_disabled"):
            del settings["disabled_by"]
            warnings.append("disabled_by stays: the device is not disabled now")
        entry.update(settings)
        if (new := msg.get("new_entity_id")) is not None:
            del self.entries[entry["entity_id"]]
            entry["entity_id"] = new
            self.entries[new] = entry
        for domain, options in msg["options"].items():
            entry["options"] = {
                d: o for d, o in entry["options"].items() if d != domain
            } | ({domain: options} if options is not None else {})
        entity_entry = copy.deepcopy(entry)
        return {"success": True, "entity_entry": entity_entry, "warnings": warnings}


def _registry(monkeypatch: pytest.MonkeyPatch, path: str) -> FakeRegistry:
    fake = FakeRegistry(DESK)
    fake.path = path
    monkeypatch.setattr(backup_manager, "_ws_send", fake.ws)
    monkeypatch.setattr(backup_entities, "get_component_caps", fake.caps)
    return fake


@pytest.fixture(params=["websocket", "component"])
def registry(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> FakeRegistry:
    return _registry(monkeypatch, request.param)


@pytest.fixture
def ws_registry(monkeypatch: pytest.MonkeyPatch) -> FakeRegistry:
    """Core's commands alone, for what only they send."""
    return _registry(monkeypatch, "websocket")


def _manager(tmp_path: Path, client: Any) -> backup_manager.BackupManager:
    mgr = backup_manager.BackupManager(
        _StubSettings(auto_backup_dir=str(tmp_path)), client
    )
    backup_manager.register_default_handlers(mgr, None)
    return mgr


def _writes(registry: FakeRegistry) -> list[dict[str, Any]]:
    return [
        m
        for m in registry.sent
        if m["type"]
        in (
            "config/entity_registry/update",
            "homeassistant/expose_entity",
            "ha_mcp_tools/entity_restore",
        )
    ]


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
            {"options_domain": "light", "options": {"favorite_colors": [[1, 2]]}},
            {"options_domain": "light", "options": None},
            {"options_domain": "sensor", "options": {"display_precision": 2}},
            # What expose_to={"conversation": True} leaves in the registry.
            {"options_domain": "conversation", "options": {"should_expose": True}},
            {"options_domain": "cloud.alexa", "options": {"should_expose": False}},
            # Writes after the rename go to the entity_id it gave back.
            {
                "new_entity_id": "light.lamp",
                "options_domain": "conversation",
                "options": {"should_expose": True},
            },
        ],
    )
    async def test_restore_gives_back_the_snapshot(
        self, registry: FakeRegistry, edit: dict[str, Any]
    ) -> None:
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.edit("light.desk", **edit)

        result = await handler.restore(registry, "light.desk", snapshot)

        assert await handler.fetch(None, "light.desk") == snapshot
        assert result["warnings"] == [_NOT_RESTORED]
        # The reply carries the entry as the last write left it.
        assert result["entity_entry"] == registry.entries["light.desk"]

    async def test_snapshot_holds_the_registry_fields_and_options(
        self, registry: FakeRegistry
    ) -> None:
        snapshot = await _handler("entity").fetch(None, "light.desk")

        assert snapshot == {
            k: v for k, v in DESK.items() if k not in ("platform", "unique_id")
        }

    async def test_an_entity_without_a_registry_entry_has_no_snapshot(
        self, registry: FakeRegistry
    ) -> None:
        assert await _handler("entity").fetch(None, "light.yaml_only") is None

    @pytest.mark.parametrize(
        ("edit", "restart"),
        [({"name": "Desk"}, False), ({"disabled_by": "user"}, True)],
    )
    async def test_only_re_enabling_asks_for_a_restart(
        self, registry: FakeRegistry, edit: dict[str, Any], restart: bool
    ) -> None:
        """Core asks for one when an update carries disabled_by None for an
        entity without a config entry, also one that was never disabled."""
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.edit("light.desk", **edit)

        result = await handler.restore(registry, "light.desk", snapshot)

        assert ("require_restart" in result) is restart

    async def test_a_hider_only_core_sets_is_left_with_cores_refusal(
        self, ws_registry: FakeRegistry
    ) -> None:
        """Core refuses it; the other settings are still restored."""
        registry = ws_registry
        registry.entries["light.desk"]["hidden_by"] = "integration"
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.entries["light.desk"]["hidden_by"] = None
        registry.edit("light.desk", name="Desk")

        result = await handler.restore(registry, "light.desk", snapshot)

        entry = registry.entries["light.desk"]
        assert (entry["hidden_by"], entry["name"]) == (None, None)
        assert result["warnings"] == [
            _NOT_RESTORED,
            "hidden_by stays None: Home Assistant refused 'integration': "
            "Command failed: invalid",
        ]

    async def test_an_entity_of_a_disabled_device_is_left_with_cores_refusal(
        self, registry: FakeRegistry
    ) -> None:
        """Core refuses to enable it; the settings written before stay."""
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.entries["light.desk"].update(
            disabled_by="device", device_disabled=True
        )
        registry.edit("light.desk", name="Desk")

        result = await handler.restore(registry, "light.desk", snapshot)

        entry = registry.entries["light.desk"]
        assert (entry["disabled_by"], entry["name"]) == ("device", None)
        assert result["warnings"] == [
            _NOT_RESTORED,
            "disabled_by stays 'device': Home Assistant refused None: "
            "Command failed: Device is disabled",
        ]

    async def test_an_entity_its_integration_hid_since_is_shown_again(
        self, registry: FakeRegistry
    ) -> None:
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.entries["light.desk"]["hidden_by"] = "integration"

        await handler.restore(registry, "light.desk", snapshot)

        assert registry.entries["light.desk"]["hidden_by"] is None

    async def test_exposure_is_set_before_the_options_write(
        self, ws_registry: FakeRegistry
    ) -> None:
        """Core tells the voice assistant about an exposure change only when
        homeassistant/expose_entity is the command that makes it."""
        registry = ws_registry
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.edit(
            "light.desk",
            options_domain="conversation",
            options={"should_expose": True},
        )

        await handler.restore(registry, "light.desk", snapshot)

        writes = [
            (m["type"], m.get("options_domain"), m.get("should_expose"))
            for m in _writes(registry)
        ]
        assert writes[1:] == [
            ("homeassistant/expose_entity", None, False),
            ("config/entity_registry/update", "conversation", None),
        ]

    async def test_an_assistant_the_backup_holds_nothing_for_is_reset(
        self, registry: FakeRegistry
    ) -> None:
        """Its options go; no exposure value is sent, since the backup has
        none to send, and Core works out the default on the next read."""
        del registry.entries["light.desk"]["options"]["conversation"]
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.edit(
            "light.desk", options_domain="conversation", options={"should_expose": True}
        )

        await handler.restore(registry, "light.desk", snapshot)

        assert "conversation" not in registry.entries["light.desk"]["options"]
        assert "homeassistant/expose_entity" not in [
            m["type"] for m in _writes(registry)
        ]

    async def test_an_unreadable_area_registry_refuses_the_restore(
        self, registry: FakeRegistry
    ) -> None:
        """A deleted area could otherwise pass unchecked."""
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.ids["area"].discard("office")
        registry.unreadable.add("area")

        with pytest.raises(ToolError):
            await handler.restore(registry, "light.desk", snapshot)

        assert _writes(registry) == []

    async def test_an_unexpected_core_error_on_a_hider_fails_the_restore(
        self, ws_registry: FakeRegistry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only Core's refusals become warnings; anything else is reported."""
        registry = ws_registry
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.edit("light.desk", hidden_by="user")
        sent = registry.ws

        async def failing_hider(client: Any, msg: dict[str, Any]) -> Any:
            if "hidden_by" in msg:
                raise HomeAssistantCommandError("Command failed: boom", "unknown_error")
            return await sent(client, msg)

        monkeypatch.setattr(backup_manager, "_ws_send", failing_hider)

        with pytest.raises(HomeAssistantCommandError):
            await handler.restore(registry, "light.desk", snapshot)

    @pytest.mark.parametrize(
        ("kind", "deleted"),
        [("area", "office"), ("label", "work"), ("category", "cat-a")],
    )
    async def test_a_deleted_area_label_or_category_refuses_the_restore(
        self, registry: FakeRegistry, kind: str, deleted: str
    ) -> None:
        """Core would store the area or category ID as sent and drop the label
        without an error, and report success."""
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        registry.edit("light.desk", name="Desk")
        registry.ids[kind].remove(deleted)

        with pytest.raises(backup_manager.BackupRestoreError) as err:
            await handler.restore(registry, "light.desk", snapshot)

        assert err.value.outcome["reason"] == "missing_reference"
        assert err.value.outcome["apply_status"] == "not_applied"
        assert repr(deleted) in str(err.value)
        assert _writes(registry) == []
        assert registry.entries["light.desk"]["name"] == "Desk"

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

    async def test_a_removed_entity_points_to_restarting_home_assistant(
        self, ws_registry: FakeRegistry
    ) -> None:
        """A reload does not add a stored helper's entity again; a restart does."""
        registry = ws_registry
        handler = _handler("entity")
        snapshot = await handler.fetch(None, "light.desk")
        del registry.entries["light.desk"]

        with pytest.raises(backup_manager.BackupRestoreError) as err:
            await handler.restore(registry, "light.desk", snapshot)

        assert err.value.outcome["reason"] == "entry_deleted"
        assert err.value.outcome["suggestions"][0].startswith("Restart Home Assistant")
        assert _writes(registry) == []

    @pytest.mark.parametrize(
        "legacy",
        [{"entity_id": "light.desk", "state": "on", "attributes": {}}, "on"],
    )
    async def test_a_state_snapshot_from_an_earlier_version_is_refused(
        self, registry: FakeRegistry, legacy: Any
    ) -> None:
        """It holds no registry settings; re-posting its state would only
        overwrite what Home Assistant shows for the entity."""

        with pytest.raises(backup_manager.InvalidBackupSnapshotError):
            await _handler("entity").restore(registry, "light.desk", legacy)

        assert registry.sent == []


def _files(path: Path) -> list[Path]:
    return sorted(path.rglob("*"))


class TestStateSnapshotFromAnEarlierVersion:
    """Refused before the safety backup and in the diff: there is nothing to
    restore, so neither may report a change."""

    @pytest.fixture
    async def legacy(
        self, registry: FakeRegistry, tmp_path: Path
    ) -> tuple[backup_manager.BackupManager, str]:
        mgr = _manager(tmp_path, registry)

        async def state_snapshot(_client: Any, entity_id: str) -> Any:
            return {"entity_id": entity_id, "state": "on", "attributes": {}}

        mgr.register(
            backup_manager.DomainHandler("entity", state_snapshot, restore_entity)
        )
        path = await mgr.maybe_snapshot("entity", "light.desk", tool_name="t")
        assert path is not None
        backup_manager.register_default_handlers(mgr, None)
        return mgr, path.name

    async def test_restore_writes_no_safety_backup(
        self, legacy: tuple[backup_manager.BackupManager, str], tmp_path: Path
    ) -> None:
        mgr, name = legacy
        files = await asyncio.to_thread(_files, tmp_path)

        with pytest.raises(backup_manager.InvalidBackupSnapshotError):
            await mgr.restore_snapshot(name)

        assert await asyncio.to_thread(_files, tmp_path) == files

    async def test_diff_is_refused(
        self, legacy: tuple[backup_manager.BackupManager, str]
    ) -> None:
        mgr, name = legacy

        with pytest.raises(backup_manager.InvalidBackupSnapshotError):
            await mgr.snapshot_comparison(name)


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
            if msg["type"] in (
                "config/entity_registry/list",
                "ha_mcp_tools/entity_entry",
            ):
                registry.edit("light.desk", new_entity_id="light.lamp")
                if taken:
                    registry.entries["light.desk"] = {**DESK, "id": "reg-other"}
            return result

        monkeypatch.setattr(backup_manager, "_ws_send", rename_after_list)

        with pytest.raises(backup_manager.BackupRestoreError) as err:
            await handler.restore(registry, "light.desk", snapshot)

        assert err.value.outcome["reason"] == "entity_identity_mismatch"
        assert _writes(registry) == []

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

        await handler.restore(registry, "light.desk", snapshot)

        assert held == ["entity:light.desk", "entity:light.lamp"]


@pytest.mark.parametrize("path", ["websocket", "component"])
async def test_the_current_entity_id_follows_the_entry(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """A gone entry has none; a state snapshot has no entry to follow."""
    fake = _registry(monkeypatch, path)
    fake.edit("light.desk", new_entity_id="light.lamp")

    assert await current_entity_id(None, "light.desk", DESK) == "light.lamp"
    assert await current_entity_id(None, "light.desk", {**DESK, "id": "x"}) is None
    with pytest.raises(backup_manager.InvalidBackupSnapshotError):
        await current_entity_id(None, "light.desk", {"state": "on"})


class TestRenamedEntity:
    """The backup is keyed by the entity_id the entity had; after a rename the
    safety backup and the diff must still find it by its entry id."""

    async def test_restore_backs_up_the_renamed_entity_first(
        self, registry: FakeRegistry, tmp_path: Path
    ) -> None:
        mgr = _manager(tmp_path, registry)
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

    async def test_a_rename_during_the_safety_backup_stops_the_restore(
        self,
        registry: FakeRegistry,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The restore would find the entity by its entry id and overwrite it
        with no safety backup taken."""
        mgr = _manager(tmp_path, registry)
        path = await mgr.maybe_snapshot("entity", "light.desk", tool_name="t")
        assert path is not None
        registry.edit("light.desk", area_id="kitchen")
        sent = registry.ws

        async def rename_before_get(client: Any, msg: dict[str, Any]) -> Any:
            if msg["type"] == "config/entity_registry/get" and "light.desk" in (
                registry.entries
            ):
                registry.edit("light.desk", new_entity_id="light.lamp")
            return await sent(client, msg)

        monkeypatch.setattr(backup_manager, "_ws_send", rename_before_get)

        with pytest.raises(backup_manager.MandatoryBackupError):
            await mgr.restore_snapshot(path.name)

        assert _writes(registry) == []
        assert registry.entries["light.lamp"]["area_id"] == "kitchen"

    async def test_an_edit_queued_during_the_safety_backup_waits_for_the_write(
        self,
        registry: FakeRegistry,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The restore holds the entity lock from before its safety backup
        through its write, so an edit cannot land between the two."""
        mgr = _manager(tmp_path, registry)
        path = await mgr.maybe_snapshot("entity", "light.desk", tool_name="t")
        assert path is not None
        registry.edit("light.desk", area_id="kitchen")
        order: list[str] = []
        queued = asyncio.Event()
        edits: list[asyncio.Task[None]] = []

        async def edit() -> None:
            lock = backup_manager.registry_update_lock("entity", "light.desk")
            queued.set()
            async with lock:
                order.append("edit")

        capture = mgr.maybe_snapshot

        async def capture_with_an_edit_queued(*args: Any, **kwargs: Any) -> Any:
            edits.append(asyncio.ensure_future(edit()))
            await queued.wait()
            return await capture(*args, **kwargs)

        sent = registry.ws

        async def record(client: Any, msg: dict[str, Any]) -> Any:
            if msg["type"] in (
                "config/entity_registry/update",
                "ha_mcp_tools/entity_restore",
            ):
                order.append("write")
            return await sent(client, msg)

        monkeypatch.setattr(mgr, "maybe_snapshot", capture_with_an_edit_queued)
        monkeypatch.setattr(backup_manager, "_ws_send", record)

        await mgr.restore_snapshot(path.name)
        await asyncio.gather(*edits)

        assert order[-1] == "edit" and "write" in order, order

    async def test_a_rename_past_the_held_locks_stops_the_restore(
        self,
        registry: FakeRegistry,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Renamed to a third ID after the safety backup, by a writer the
        locks do not cover: the restore holds no lock on that ID and stops."""
        mgr = _manager(tmp_path, registry)
        path = await mgr.maybe_snapshot("entity", "light.desk", tool_name="t")
        assert path is not None
        registry.edit("light.desk", area_id="kitchen")
        captured = False
        capture = mgr.maybe_snapshot

        async def capture_then_flag(*args: Any, **kwargs: Any) -> Any:
            nonlocal captured
            result = await capture(*args, **kwargs)
            captured = True
            return result

        lookup = backup_entities._lookup

        async def rename_once_captured(client: Any, entry_id: str) -> Any:
            if captured and "light.desk" in registry.entries:
                registry.edit("light.desk", new_entity_id="light.third")
            return await lookup(client, entry_id)

        monkeypatch.setattr(mgr, "maybe_snapshot", capture_then_flag)
        monkeypatch.setattr(backup_entities, "_lookup", rename_once_captured)

        with pytest.raises(backup_manager.BackupRestoreError) as err:
            await mgr.restore_snapshot(path.name)

        assert err.value.outcome["reason"] == "entity_identity_mismatch"
        assert _writes(registry) == []

    async def test_a_rename_back_holds_both_ids_from_the_safety_backup(
        self,
        registry: FakeRegistry,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mgr = _manager(tmp_path, registry)
        path = await mgr.maybe_snapshot("entity", "light.desk", tool_name="t")
        assert path is not None
        registry.edit("light.desk", new_entity_id="light.lamp", area_id="kitchen")
        seen: list[tuple[str, list[str]]] = []

        def held() -> list[str]:
            return [
                entity_id
                for entity_id in ("light.desk", "light.lamp")
                if backup_manager.registry_update_lock("entity", entity_id).locked()
            ]

        capture = mgr.maybe_snapshot

        async def check(*args: Any, **kwargs: Any) -> Any:
            seen.append(("backup", held()))
            return await capture(*args, **kwargs)

        sent = registry.ws

        async def record(client: Any, msg: dict[str, Any]) -> Any:
            if msg["type"] in (
                "config/entity_registry/update",
                "ha_mcp_tools/entity_restore",
            ):
                seen.append(("write", held()))
            return await sent(client, msg)

        monkeypatch.setattr(mgr, "maybe_snapshot", check)
        monkeypatch.setattr(backup_manager, "_ws_send", record)

        await mgr.restore_snapshot(path.name)

        assert seen[0][0] == "backup", seen
        assert all(ids == ["light.desk", "light.lamp"] for _, ids in seen), seen

    async def test_a_failed_lock_lookup_keeps_the_restore_outcome(
        self,
        registry: FakeRegistry,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mgr = _manager(tmp_path, registry)
        path = await mgr.maybe_snapshot("entity", "light.desk", tool_name="t")
        assert path is not None

        async def failing(client: Any, entry_id: str) -> Any:
            raise ConnectionError("registry unavailable")

        monkeypatch.setattr(backup_entities, "_lookup", failing)

        with pytest.raises(ConnectionError) as err:
            await mgr.restore_snapshot(path.name)

        assert vars(err.value)["restore_outcome"]["restored_from"] == path.name
        assert _writes(registry) == []

    async def test_a_cancel_during_the_lock_lookup_writes_nothing(
        self,
        registry: FakeRegistry,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mgr = _manager(tmp_path, registry)
        path = await mgr.maybe_snapshot("entity", "light.desk", tool_name="t")
        assert path is not None
        registry.edit("light.desk", area_id="kitchen")
        lookup = backup_entities._lookup
        calls = 0

        async def cancelled_once(client: Any, entry_id: str) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise asyncio.CancelledError
            return await lookup(client, entry_id)

        monkeypatch.setattr(backup_entities, "_lookup", cancelled_once)

        with pytest.raises(asyncio.CancelledError):
            await mgr.restore_snapshot(path.name)

        assert _writes(registry) == []

    @pytest.mark.parametrize("first", ["fails", "finds_nothing"])
    async def test_an_entry_not_locked_before_the_safety_backup_is_not_written(
        self,
        registry: FakeRegistry,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        first: str,
    ) -> None:
        """The lookup before the safety backup failed or found no entry, and
        a later one finds it: the restore refuses instead of writing it
        without its locks."""
        mgr = _manager(tmp_path, registry)
        path = await mgr.maybe_snapshot("entity", "light.desk", tool_name="t")
        assert path is not None
        registry.edit("light.desk", area_id="kitchen")
        lookup = backup_entities._lookup
        calls = 0

        async def first_lookup_misses(client: Any, entry_id: str) -> Any:
            nonlocal calls
            calls += 1
            if calls > 1:
                return await lookup(client, entry_id)
            if first == "fails":
                raise ConnectionError("registry unavailable")
            return {"entry": None}

        monkeypatch.setattr(backup_entities, "_lookup", first_lookup_misses)

        with pytest.raises(backup_manager.BackupRestoreError) as err:
            await mgr.restore_snapshot(path.name)

        assert err.value.outcome["reason"] == "entity_identity_mismatch"
        assert err.value.outcome["verification_status"] == "mismatched"
        assert "could not be locked before its safety backup" in str(err.value)
        assert _writes(registry) == []
        assert registry.entries["light.desk"]["area_id"] == "kitchen"

    @pytest.mark.parametrize("missing", ["deleted", "none", "empty"])
    async def test_a_backup_without_an_entity_id_is_refused(
        self,
        registry: FakeRegistry,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        missing: str,
    ) -> None:
        """Refused before its locks and its safety backup; before, a missing
        key raised a KeyError in the lock lookup and a None one let the
        restore go ahead."""
        mgr = _manager(tmp_path, registry)
        path = await mgr.maybe_snapshot("entity", "light.desk", tool_name="t")
        assert path is not None
        read = mgr.read_snapshot

        def without_entity_id(name: str) -> dict[str, Any]:
            data = read(name)
            if missing == "deleted":
                del data["config"]["entity_id"]
            else:
                data["config"]["entity_id"] = None if missing == "none" else ""
            return data

        monkeypatch.setattr(mgr, "read_snapshot", without_entity_id)
        files = await asyncio.to_thread(_files, tmp_path)

        with pytest.raises(
            backup_manager.InvalidBackupSnapshotError, match="names no entity_id"
        ):
            await mgr.restore_snapshot(path.name)

        assert await asyncio.to_thread(_files, tmp_path) == files
        assert _writes(registry) == []

    async def test_diff_compares_with_the_renamed_entity(
        self, registry: FakeRegistry, tmp_path: Path
    ) -> None:
        mgr = _manager(tmp_path, registry)
        path = await mgr.maybe_snapshot("entity", "light.desk", tool_name="t")
        assert path is not None
        registry.edit("light.desk", new_entity_id="light.lamp")

        _, current = await mgr.snapshot_comparison(path.name)

        assert current is not None and current["entity_id"] == "light.lamp"

    async def test_a_removed_entity_is_named_by_its_entity_id(
        self, registry: FakeRegistry, tmp_path: Path
    ) -> None:
        """The diff has no current entry, and the restore's warning names the
        entity, not its registry entry id."""
        mgr = _manager(tmp_path, registry)
        path = await mgr.maybe_snapshot("entity", "light.desk", tool_name="t")
        assert path is not None
        del registry.entries["light.desk"]

        _, current = await mgr.snapshot_comparison(path.name)
        with pytest.raises(backup_manager.BackupRestoreError) as err:
            await mgr.restore_snapshot(path.name)

        assert current is None
        [warning] = err.value.outcome["warnings"]
        assert "light.desk" in warning and "reg-desk" not in warning

    async def test_an_entity_now_holding_the_old_id_stops_the_rename_back(
        self, registry: FakeRegistry, tmp_path: Path
    ) -> None:
        """Checked under the lock, so the error names that entity and neither
        entity changes."""
        mgr = _manager(tmp_path, registry)
        path = await mgr.maybe_snapshot("entity", "light.desk", tool_name="t")
        assert path is not None
        registry.edit("light.desk", new_entity_id="light.lamp", area_id="kitchen")
        registry.entries["light.desk"] = {**DESK, "id": "reg-other"}

        with pytest.raises(backup_manager.BackupRestoreError) as err:
            await mgr.restore_snapshot(path.name)

        assert err.value.outcome["reason"] == "entity_id_collision"
        assert err.value.outcome["conflicting_entity_id"] == "light.desk"
        assert "light.desk" in str(err.value)
        assert _writes(registry) == []


def test_a_bulk_call_backs_up_no_entity() -> None:
    """Snapshotting the first entity of a list would leave the rest without a
    backup while looking covered, so a list names no entity. A list of
    one is a single edit to ha_set_entity."""
    assert _single_entity_target({"entity_id": ["light.a", "light.b"]}) == ""
    assert _single_entity_target({"entity_id": "light.a"}) == "light.a"
    assert _single_entity_target({"entity_id": ["light.a"]}) == "light.a"
