"""Zone backups snapshot and restore the zone the zone tools change (#2632).

The zone tools resolve a zone by its storage id, else by its registry
entity_id, so a backup must resolve it the same way; a create has no zone to
back up. Core allows identical zones, so a restore cannot tell a zone an
earlier restore created from one the user made.
"""

import copy
from typing import Any
from unittest.mock import MagicMock

import pytest

from ha_mcp import backup_manager
from ha_mcp._vendor.websockets.exceptions import ConnectionClosedError
from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_zones import ZoneTools

from .test_zone_tools_core_contract import (
    OFFICE,
    OFFICE_ENTRY,
    _client,
    _no_component,  # noqa: F401 - pytest fixture
    registered,  # noqa: F401 - pytest fixture
    removed,  # noqa: F401 - pytest fixture
)


def _backup_ws(
    zone_list: list[dict[str, Any]],
    registry: list[dict[str, Any]],
    update_error: str | None = None,
    registry_update_error: str | None = None,
) -> tuple[list[dict[str, Any]], Any]:
    """A fake ``backup_manager._ws_send`` and the messages it received.

    zone/update and zone/create change ``zone_list`` and ``registry`` the way
    Core does: a created zone's id is its slugified name.
    """
    sent: list[dict[str, Any]] = []

    def update(msg: dict[str, Any]) -> dict[str, Any]:
        current = next((z for z in zone_list if z["id"] == msg["zone_id"]), None)
        if update_error or current is None:
            code = update_error or "not_found"
            raise HomeAssistantCommandError(f"Command failed: {code}", code)
        current.update({k: v for k, v in msg.items() if k not in ("type", "zone_id")})
        return dict(current)

    def create(msg: dict[str, Any]) -> dict[str, Any]:
        created = {k: v for k, v in msg.items() if k != "type"}
        taken = {z["id"] for z in zone_list}
        slug = created["name"].lower()
        created["id"] = next(
            i for i in (slug, *(f"{slug}_{n}" for n in range(2, 99))) if i not in taken
        )
        zone_list.append(created)
        registry.append(
            {
                "entity_id": f"zone.{created['id']}_2",
                "platform": "zone",
                "unique_id": created["id"],
            }
        )
        return dict(created)

    def registry_get(msg: dict[str, Any]) -> dict[str, Any]:
        entry = next((e for e in registry if e["entity_id"] == msg["entity_id"]), None)
        if entry is None:
            raise HomeAssistantCommandError(
                "Command failed: Entity not found", "not_found"
            )
        return entry

    def registry_update(_msg: dict[str, Any]) -> dict[str, Any]:
        if registry_update_error:
            raise HomeAssistantCommandError(
                f"Command failed: {registry_update_error}", registry_update_error
            )
        return {"entity_entry": {}}

    handlers = {
        "zone/list": lambda _msg: zone_list,
        "config/entity_registry/list": lambda _msg: registry,
        "config/entity_registry/get": registry_get,
        "config/entity_registry/update": registry_update,
        "zone/update": update,
        "zone/create": create,
    }

    async def fake_ws(_client: Any, msg: dict[str, Any]) -> Any:
        sent.append(dict(msg))
        return copy.deepcopy(handlers[msg["type"]](msg))

    return sent, fake_ws


def _office_entity(icon: str | None = "mdi:briefcase") -> dict[str, Any]:
    return {**OFFICE_ENTRY, "icon": icon}


def _handler(domain: str) -> Any:
    mgr = MagicMock()
    handlers: dict[str, Any] = {}
    mgr.register = lambda h: handlers.__setitem__(h.domain, h)
    backup_manager.register_default_handlers(mgr, None)
    return handlers[domain]


class TestZoneBackup:
    @pytest.mark.parametrize("zone_id", ["office", "zone.office"])
    @pytest.mark.parametrize("domain", ["zone", "helper_zone"])
    async def test_snapshot_records_the_registry_icon(
        self, monkeypatch: pytest.MonkeyPatch, domain: str, zone_id: str
    ) -> None:
        """The zone tools take a zone_id or the zone's entity_id."""
        _, fake_ws = _backup_ws([dict(OFFICE)], [_office_entity()])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        snapshot = await _handler(domain).fetch(None, zone_id)

        assert snapshot == {**OFFICE, "registry_icon": "mdi:briefcase"}

    @pytest.mark.parametrize(
        ("zone_id", "found"), [("zone.work", True), ("work", False)]
    )
    async def test_snapshot_names_a_renamed_zone_only_by_its_entity_id(
        self, monkeypatch: pytest.MonkeyPatch, zone_id: str, found: bool
    ) -> None:
        """The zone tools resolve ``zone.work`` to office and a bare ``work``
        to no zone, so the backup must too."""
        renamed = {**_office_entity(), "entity_id": "zone.work"}
        _, fake_ws = _backup_ws([dict(OFFICE)], [renamed])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        snapshot = await _handler("zone").fetch(None, zone_id)

        assert snapshot == (
            {**OFFICE, "registry_icon": "mdi:briefcase"} if found else None
        )

    @pytest.mark.parametrize(
        ("zone_id", "changed"),
        [
            # A stored zone "Home" is zone.home_2: the home zone holds zone.home.
            ("zone.home_2", "home"),
            # A zone named "home_2" does not shadow the zone with that id.
            ("home_2", "home_2"),
        ],
    )
    async def test_snapshot_is_of_the_zone_the_tool_changes(
        self, monkeypatch: pytest.MonkeyPatch, zone_id: str, changed: str
    ) -> None:
        """A wrong snapshot would leave the changed zone without a rollback."""
        zones = [
            {**OFFICE, "id": "home", "name": "home_2"},
            {**OFFICE, "id": "home_2", "name": "Home 2"},
        ]
        registry = [
            {"entity_id": "zone.home_2", "platform": "zone", "unique_id": "home"},
            {"entity_id": "zone.home_2_2", "platform": "zone", "unique_id": "home_2"},
        ]
        _, fake_ws = _backup_ws(zones, registry)
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)
        tools = ZoneTools(
            _client(
                {
                    "config/entity_registry/list": {
                        "success": True,
                        "result": registry,
                    },
                    "zone/list": {"success": True, "result": zones},
                }
            )
        )

        _, storage_id, _ = await tools._resolve_zone(zone_id)
        snapshot = await _handler("zone").fetch(None, zone_id)

        assert storage_id == snapshot["id"] == changed

    async def test_a_zone_without_a_registry_entry_is_the_zone_backed_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zone "work" lost its registry entry and zone "office" was renamed to
        zone.work: the tools change "work", as the backup holds it."""
        zones = [dict(OFFICE), {**OFFICE, "id": "work", "name": "Work"}]
        registry = [
            {"entity_id": "zone.work", "platform": "zone", "unique_id": "office"}
        ]
        _, fake_ws = _backup_ws(zones, registry)
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)
        tools = ZoneTools(
            _client(
                {
                    "config/entity_registry/list": {
                        "success": True,
                        "result": registry,
                    },
                    "zone/list": {"success": True, "result": zones},
                }
            )
        )

        entity_id, storage_id, _ = await tools._resolve_zone("work")
        snapshot = await _handler("zone").fetch(None, "work")

        assert storage_id == snapshot["id"] == "work"
        assert entity_id is None

    @pytest.mark.parametrize(
        ("kwargs", "captured"),
        [
            ({"name": "Office", "latitude": 52.5, "longitude": 13.4}, []),
            ({"zone_id": "office", "radius": 50}, [("zone", "office")]),
        ],
    )
    async def test_creating_a_zone_backs_up_no_other_zone(
        self,
        monkeypatch: pytest.MonkeyPatch,
        kwargs: dict[str, Any],
        captured: list[tuple[str, str]],
    ) -> None:
        """A new zone has nothing to back up; a zone of the same name is not it."""
        snapshots: list[tuple[str, str]] = []

        class FakeManager:
            async def maybe_snapshot(
                self, domain: str, entity_id: str, **_kwargs: Any
            ) -> None:
                snapshots.append((domain, entity_id))

        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_global_settings",
            lambda: MagicMock(enable_auto_backup=True),
        )
        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_backup_manager",
            lambda *_a, **_k: FakeManager(),
        )
        client = _client({"zone/create": {"success": True, "result": dict(OFFICE)}})

        await ZoneTools(client).ha_set_zone(**kwargs, wait=False)

        assert snapshots == captured

    async def test_a_failed_registry_read_fails_the_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Saved without the icon key, it would read as an older snapshot."""

        async def fake_ws(_client: Any, msg: dict[str, Any]) -> Any:
            if msg["type"] == "zone/list":
                return [OFFICE]
            raise HomeAssistantConnectionError("registry read failed")

        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        with pytest.raises(HomeAssistantConnectionError):
            await _handler("zone").fetch(None, "office")

    @pytest.mark.parametrize("domain", ["zone", "helper_zone"])
    async def test_restore_of_a_removed_zone_creates_it_again(
        self, monkeypatch: pytest.MonkeyPatch, domain: str
    ) -> None:
        """Core's zone/update cannot restore a zone that no longer exists."""
        sent, fake_ws = _backup_ws([], [])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        result = await _handler(domain).restore(
            None, "office", {**OFFICE, "registry_icon": "mdi:briefcase"}
        )

        (create,) = [m for m in sent if m["type"] == "zone/create"]
        assert {k: create[k] for k in ("name", "latitude", "longitude")} == {
            "name": "Office",
            "latitude": 52.5,
            "longitude": 13.4,
        }
        assert "registry_icon" not in create
        assert result["restore_mode"] == "recreated"
        assert {
            "type": "config/entity_registry/update",
            "entity_id": "zone.office_2",
            "icon": "mdi:briefcase",
        } in sent

    async def test_a_repeated_restore_does_not_add_another_zone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zone renamed after the snapshot comes back under a new id."""
        zones: list[dict[str, Any]] = []
        snapshot = {**OFFICE, "id": "workplace", "registry_icon": None}
        sent, fake_ws = _backup_ws(zones, [])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        await _handler("zone").restore(None, "workplace", dict(snapshot))
        again = await _handler("zone").restore(None, "workplace", dict(snapshot))

        assert len([m for m in sent if m["type"] == "zone/create"]) == 1
        assert [z["id"] for z in zones] == ["office"]
        assert again["restore_mode"] == "already_recreated"

    async def test_a_different_zone_with_the_same_name_is_not_overwritten(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Core allows two zones with one name: the user's own zone stays as it is."""
        other = {**OFFICE, "latitude": 1.0, "longitude": 2.0}
        zones = [dict(other)]
        sent, fake_ws = _backup_ws(zones, [])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        await _handler("zone").restore(
            None, "workplace", {**OFFICE, "id": "workplace", "registry_icon": None}
        )

        assert not [
            m for m in sent if m["type"] == "zone/update" and m["zone_id"] == "office"
        ]
        assert zones[0] == other
        assert [z["id"] for z in zones] == ["office", "office_2"]

    async def test_an_identical_zone_is_named_and_its_icon_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Core allows identical zones, so the match may be the user's own zone.

        The warning names the recorded icon, so it can still be set by hand
        when an earlier restore failed to write it.
        """
        twin = {**OFFICE, "id": "office_2"}
        twin_entry = {
            **OFFICE_ENTRY,
            "entity_id": "zone.office_2",
            "unique_id": "office_2",
        }
        sent, fake_ws = _backup_ws([dict(twin)], [twin_entry])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        result = await _handler("zone").restore(
            None, "office", {**OFFICE, "registry_icon": "mdi:briefcase"}
        )

        assert result["restore_mode"] == "already_recreated"
        assert "office_2" in result["warnings"][0]
        assert "mdi:briefcase" in result["warnings"][0]
        assert not [m for m in sent if m["type"] == "config/entity_registry/update"]

    async def test_a_snapshot_without_a_registry_icon_offers_none_to_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Telling the user to set the icon None would be a wrong next step."""
        _, fake_ws = _backup_ws([{**OFFICE, "id": "office_2"}], [])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        result = await _handler("zone").restore(
            None, "office", {**OFFICE, "registry_icon": None}
        )

        assert "ha_set_entity" not in result["warnings"][0]

    async def test_a_zone_with_a_field_the_snapshot_lacks_is_not_the_restored_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A zone recreated from an icon-less snapshot has no stored icon."""
        zones = [{**OFFICE, "id": "office_2", "icon": "mdi:home"}]
        sent, fake_ws = _backup_ws(zones, [])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        result = await _handler("zone").restore(None, "office", dict(OFFICE))

        assert result["restore_mode"] == "recreated"
        assert zones[0]["icon"] == "mdi:home"

    async def test_a_dropped_connection_during_the_icon_write_is_a_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The zone is already restored, so the restore must not fail over it."""
        _, backup_ws = _backup_ws([dict(OFFICE)], [_office_entity()])

        async def fake_ws(client: Any, msg: dict[str, Any]) -> Any:
            if msg["type"] == "config/entity_registry/update":
                raise ConnectionClosedError(None, None)
            return await backup_ws(client, msg)

        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        result = await _handler("zone").restore(
            None, "office", {**OFFICE, "registry_icon": "mdi:star"}
        )

        assert "registry icon was not" in result["warnings"][0]

    @pytest.mark.parametrize("helper_type", ["person", "tag", "input_boolean"])
    async def test_a_snapshot_records_and_restores_the_registry_icon(
        self, monkeypatch: pytest.MonkeyPatch, helper_type: str
    ) -> None:
        """The entity shows its registry icon, which an update writes for every
        type and the stored item can miss (person and tag have no icon field)."""
        item = {"id": "anna", "name": "Anna"}
        entry = {
            "entity_id": f"{helper_type}.anna",
            "platform": helper_type,
            "unique_id": "anna",
            "icon": "mdi:star",
            "name": "Anna",
        }
        sent: list[dict[str, Any]] = []

        async def fake_ws(_client: Any, msg: dict[str, Any]) -> Any:
            sent.append(dict(msg))
            if msg["type"] == f"{helper_type}/list":
                return {"storage": [item]} if helper_type == "person" else [item]
            if msg["type"] == "config/entity_registry/list":
                return [dict(entry)]
            if msg["type"] == "config/entity_registry/get":
                if msg["entity_id"] != entry["entity_id"]:
                    raise HomeAssistantCommandError("Command failed", "not_found")
                return dict(entry)
            return {**item}

        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)
        handler = _handler(f"helper_{helper_type}")

        # Snapshots are keyed by entity_id; the restore writes the stored item's id.
        snapshot = await handler.fetch(None, f"{helper_type}.anna")
        await handler.restore(
            None, f"{helper_type}.anna", {**snapshot, "registry_icon": "mdi:account"}
        )

        assert snapshot["registry_icon"] == "mdi:star"
        (update,) = [m for m in sent if m["type"] == f"{helper_type}/update"]
        assert "registry_icon" not in update
        assert {
            "type": "config/entity_registry/update",
            "entity_id": f"{helper_type}.anna",
            "icon": "mdi:account",
        } in sent

    async def test_a_failed_icon_write_after_a_recreate_is_a_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent, fake_ws = _backup_ws([], [], registry_update_error="unauthorized")
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        result = await _handler("zone").restore(
            None, "office", {**OFFICE, "registry_icon": "mdi:briefcase"}
        )

        assert result["restore_mode"] == "recreated"
        assert "registry icon was not" in result["warnings"][0]

    async def test_a_recorded_icon_without_an_entity_is_a_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, fake_ws = _backup_ws([dict(OFFICE)], [])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        result = await _handler("zone").restore(
            None, "office", {**OFFICE, "registry_icon": "mdi:briefcase"}
        )

        assert "has no entity" in result["warnings"][0]

    async def test_no_recorded_icon_and_no_entity_is_no_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, fake_ws = _backup_ws([dict(OFFICE)], [])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        result = await _handler("zone").restore(
            None, "office", {**OFFICE, "registry_icon": None}
        )

        assert "warnings" not in result

    async def test_restore_resets_a_registry_icon_set_after_the_snapshot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent, fake_ws = _backup_ws([dict(OFFICE)], [_office_entity()])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        await _handler("zone").restore(
            None, "office", {**OFFICE, "registry_icon": None}
        )

        (update,) = [m for m in sent if m["type"] == "zone/update"]
        assert "registry_icon" not in update
        assert {
            "type": "config/entity_registry/update",
            "entity_id": "zone.office",
            "icon": None,
        } in sent

    async def test_an_icon_stored_after_the_snapshot_is_a_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Core's zone/update merges, so it cannot remove the newer stored icon."""
        _, fake_ws = _backup_ws([{**OFFICE, "icon": "mdi:school"}], [_office_entity()])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        result = await _handler("zone").restore(None, "office", dict(OFFICE))

        assert "mdi:school" in result["warnings"][0]

    async def test_restore_of_an_older_snapshot_leaves_the_registry_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent, fake_ws = _backup_ws([dict(OFFICE)], [_office_entity()])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        await _handler("zone").restore(None, "office", dict(OFFICE))

        assert [m["type"] for m in sent] == ["zone/update"]

    async def test_other_update_failures_are_not_turned_into_a_create(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent, fake_ws = _backup_ws([dict(OFFICE)], [], update_error="invalid_format")
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        with pytest.raises(HomeAssistantCommandError):
            await _handler("zone").restore(None, "office", dict(OFFICE))
        assert not [m for m in sent if m["type"] == "zone/create"]
