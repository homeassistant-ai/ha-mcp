"""Zone tools against the answers Home Assistant actually sends (#2632).

Core reports a missing zone as ``not_found`` with the text "Unable to find
zone_id <id>" (``helpers/collection.py``). A storage zone's id is its slugified
name and its registry ``unique_id``; its entity_id also follows the name but is
picked separately, so the two can differ (``zone.home_2`` for a zone named
"Home"). The home zone and YAML zones have no ``unique_id`` and so no registry
entry, and the home zone's state says ``editable: True``
(``components/zone/__init__.py``).
"""

import copy
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp import backup_manager
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.config_helpers.create import _execute_create_simple_helper
from ha_mcp.tools.config_helpers.update import _execute_update_simple_helper
from ha_mcp.tools.tools_zones import ZoneTools

OFFICE = {
    "id": "office",
    "name": "Office",
    "latitude": 52.5,
    "longitude": 13.4,
    "radius": 100.0,
    "passive": False,
}
OFFICE_ENTRY = {"entity_id": "zone.office", "platform": "zone", "unique_id": "office"}
REGISTRY = [
    OFFICE_ENTRY,
    {"entity_id": "light.desk", "platform": "hue", "unique_id": "office"},
]
HOME_STATE = {
    "entity_id": "zone.home",
    "state": "1",
    "attributes": {
        "latitude": 52.0,
        "longitude": 13.0,
        "radius": 100,
        "passive": False,
        "persons": ["person.anna"],
        "device_trackers": [],
        "editable": True,
        "icon": "mdi:home",
        "friendly_name": "Home",
    },
}
REGISTRY_BLOCKED = {
    "success": False,
    "error": "WebSocket request blocked (403 Forbidden)",
    "error_code": None,
}


def _client(
    replies: dict[str, Any] | None = None,
    states: dict[str, dict[str, Any]] | None = None,
) -> MagicMock:
    """A client answering by WebSocket command type and entity_id."""
    answers: dict[str, Any] = {
        "config/entity_registry/list": {"success": True, "result": REGISTRY},
        "zone/list": {"success": True, "result": [OFFICE]},
        "zone/create": {"success": True, "result": OFFICE},
        "zone/update": {"success": True, "result": OFFICE},
        "zone/delete": {"success": True, "result": None},
        "config/entity_registry/get": {"success": True, "result": OFFICE_ENTRY},
        "config/entity_registry/update": {
            "success": True,
            "result": {"entity_entry": OFFICE_ENTRY},
        },
    }
    answers.update(replies or {})
    known = {"zone.office": {"entity_id": "zone.office", "attributes": {}}}
    known.update(states or {})

    async def send(message: dict[str, Any]) -> Any:
        # A fresh copy each time, like a real reply: the tools write into results.
        return copy.deepcopy(answers[message["type"]])

    async def state(entity_id: str) -> dict[str, Any]:
        if entity_id not in known:
            raise HomeAssistantAPIError("Not found", status_code=404)
        return known[entity_id]

    client = MagicMock()
    client.send_websocket_message = AsyncMock(side_effect=send)
    client.get_entity_state = AsyncMock(side_effect=state)
    client.get_states = AsyncMock(return_value=list(known.values()))
    return client


def _sent(client: MagicMock, kind: str) -> list[dict[str, Any]]:
    return [
        c.args[0]
        for c in client.send_websocket_message.call_args_list
        if c.args[0]["type"] == kind
    ]


def _error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    return json.loads(str(exc_info.value))["error"]  # type: ignore[no-any-return]


@pytest.fixture
def removed() -> AsyncMock:
    return AsyncMock(return_value=True)


@pytest.fixture(autouse=True)
def _no_component(removed: AsyncMock):
    """Serve every call through Core's own WebSocket commands, without waiting."""
    with (
        patch(
            "ha_mcp.tools.tools_zones.get_component_caps",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "ha_mcp.tools.config_helpers.update.read_helper_item",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "ha_mcp.tools.config_helpers.create.write_helper_item",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "ha_mcp.tools.config_helpers.update.wait_for_entity_registered",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "ha_mcp.tools.config_helpers.create.wait_for_entity_registered",
            new=AsyncMock(return_value=True),
        ),
        patch("ha_mcp.tools.tools_zones.wait_for_entity_removed", new=removed),
    ):
        yield


class TestZoneLookup:
    async def test_core_not_found_on_delete_is_resource_not_found(self):
        """The registry still lists the zone, but Core has already removed it."""
        client = _client(
            {
                "zone/delete": {
                    "success": False,
                    "error": "Command failed: Unable to find zone_id office",
                    "error_code": "not_found",
                }
            }
        )

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_remove_zone(zone_id="office")

        assert _error(exc_info)["code"] == "RESOURCE_NOT_FOUND"

    @pytest.mark.parametrize("call", ["remove", "update"])
    async def test_unknown_zone_is_resource_not_found_before_any_write(self, call):
        client = _client()
        tools = ZoneTools(client)

        with pytest.raises(ToolError) as exc_info:
            if call == "remove":
                await tools.ha_remove_zone(zone_id="nope")
            else:
                await tools.ha_set_zone(zone_id="nope", radius=50)

        error = _error(exc_info)
        assert error["code"] == "RESOURCE_NOT_FOUND"
        assert "ha_get_zone" in json.dumps(error)
        assert not _sent(client, "zone/delete") and not _sent(client, "zone/update")

    @pytest.mark.parametrize("call", ["remove", "update"])
    @pytest.mark.parametrize("zone_id", ["home", "zone.home"])
    async def test_home_zone_is_refused_as_not_stored(self, call, zone_id):
        """The home zone has a state but no registry entry: nothing is sent."""
        client = _client(states={"zone.home": HOME_STATE})
        tools = ZoneTools(client)

        with pytest.raises(ToolError) as exc_info:
            if call == "remove":
                await tools.ha_remove_zone(zone_id=zone_id)
            else:
                await tools.ha_set_zone(zone_id=zone_id, radius=50)

        assert "not a stored zone" in _error(exc_info)["message"]
        assert not _sent(client, "zone/delete") and not _sent(client, "zone/update")

    async def test_an_empty_state_reply_is_no_zone(self):
        """A non-JSON reply comes back as {}; it is not a zone outside the storage."""
        client = _client(states={"zone.ghost": {}})

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_remove_zone(zone_id="ghost")

        assert "Zone not found" in _error(exc_info)["message"]

    async def test_blocked_registry_read_is_not_a_missing_zone(self):
        client = _client({"config/entity_registry/list": REGISTRY_BLOCKED})

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_remove_zone(zone_id="office")

        error = _error(exc_info)
        assert error["code"] != "RESOURCE_NOT_FOUND"
        assert "403" in error["message"]
        assert _sent(client, "zone/delete") == []

    @pytest.mark.parametrize("zone_id", ["office", "zone.office"])
    async def test_remove_by_zone_id_or_entity_id_deletes_the_storage_item(
        self, zone_id
    ):
        client = _client()

        result = await ZoneTools(client).ha_remove_zone(zone_id=zone_id)

        assert _sent(client, "zone/delete") == [
            {"type": "zone/delete", "zone_id": "office"}
        ]
        assert result["entity_id"] == "zone.office"

    async def test_a_storage_id_wins_over_a_renamed_entity_of_the_same_text(self):
        """Zone "office" was renamed to zone.work; zone "work" is zone.work_2."""
        renamed = [
            {"entity_id": "zone.work", "platform": "zone", "unique_id": "office"},
            {"entity_id": "zone.work_2", "platform": "zone", "unique_id": "work"},
        ]
        client = _client(
            {"config/entity_registry/list": {"success": True, "result": renamed}}
        )

        await ZoneTools(client).ha_remove_zone(zone_id="work")

        assert _sent(client, "zone/delete") == [
            {"type": "zone/delete", "zone_id": "work"}
        ]


class TestRemoveWait:
    async def test_wait_false_skips_the_removal_check(self, removed):
        await ZoneTools(_client()).ha_remove_zone(zone_id="office", wait=False)

        removed.assert_not_awaited()

    async def test_a_zone_still_present_after_the_wait_is_a_warning(self, removed):
        removed.return_value = False

        result = await ZoneTools(_client()).ha_remove_zone(zone_id="office")

        assert result["success"] is True
        assert "still present" in result["warnings"][0]

    async def test_a_failed_removal_check_is_a_warning(self, removed):
        removed.side_effect = HomeAssistantConnectionError("socket closed")

        result = await ZoneTools(_client()).ha_remove_zone(zone_id="office")

        assert "verification failed" in result["warnings"][0]


class TestZoneWrites:
    async def test_new_icon_on_a_zone_without_stored_icon_goes_to_the_registry(self):
        """An icon written into the zone item could never be cleared again (#2643)."""
        client = _client()

        await ZoneTools(client).ha_set_zone(zone_id="office", icon="mdi:briefcase")

        assert all("icon" not in m for m in _sent(client, "zone/update"))
        assert any(
            m.get("icon") == "mdi:briefcase"
            for m in _sent(client, "config/entity_registry/update")
        )

    async def test_update_by_entity_id_writes_the_zone_item(self):
        client = _client()

        result = await ZoneTools(client).ha_set_zone(zone_id="zone.office", radius=250)

        (update,) = _sent(client, "zone/update")
        assert update["zone_id"] == "office"
        assert update["radius"] == 250
        assert result["zone_id"] == "office"
        assert result["entity_id"] == "zone.office"
        assert result["updated_fields"] == ["radius"]

    async def test_create_leaves_defaults_to_core(self):
        """Core applies radius and passive defaults; the tool must not pin its own."""
        client = _client()

        await ZoneTools(client).ha_set_zone(
            name="Office", latitude=52.5, longitude=13.4
        )

        (create,) = _sent(client, "zone/create")
        assert "radius" not in create
        assert "passive" not in create

    @pytest.mark.parametrize("zone_id", [None, "office"])
    async def test_blank_name_is_refused(self, zone_id):
        client = _client()

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_set_zone(
                name="  ", latitude=52.5, longitude=13.4, zone_id=zone_id
            )

        assert "name cannot be blank" in _error(exc_info)["message"]
        assert not _sent(client, "zone/create") and not _sent(client, "zone/update")


class TestCreatedIconStaysClearable:
    """A new zone's, person's or tag's icon goes to the entity registry.

    Core's zone/update merges into the stored item and rejects an empty icon, so
    an icon stored in a zone can never be removed; person and tag have no icon
    field, so Core rejects one there (#2643).
    """

    async def test_zone_create_writes_the_icon_to_its_registry_entity(self):
        """The new zone "Home" is zone.home_2: the built-in home zone holds zone.home."""
        home = {**OFFICE, "id": "home", "name": "Home"}
        client = _client(
            {
                "zone/create": {"success": True, "result": home},
                "config/entity_registry/list": {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "zone.home_2",
                            "platform": "zone",
                            "unique_id": "home",
                        }
                    ],
                },
            },
            states={"zone.home": HOME_STATE},
        )

        result = await ZoneTools(client).ha_set_zone(
            name="Home", latitude=52.5, longitude=13.4, icon="mdi:house"
        )

        (create,) = _sent(client, "zone/create")
        assert "icon" not in create
        assert _sent(client, "config/entity_registry/update")[0]["entity_id"] == (
            "zone.home_2"
        )
        assert result["entity_id"] == "zone.home_2"

    async def test_an_icon_that_found_no_entity_is_a_warning(self):
        client = _client(
            {"config/entity_registry/list": {"success": True, "result": []}}
        )

        result = await ZoneTools(client).ha_set_zone(
            name="Office", latitude=52.5, longitude=13.4, icon="mdi:briefcase"
        )

        assert _sent(client, "config/entity_registry/update") == []
        assert "were not applied" in result["warnings"][0]

    async def test_person_create_writes_the_icon_to_the_registry(self):
        client = _client(
            {
                "person/create": {"success": True, "result": {"id": "anna"}},
                "config/entity_registry/list": {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "person.anna",
                            "platform": "person",
                            "unique_id": "anna",
                        }
                    ],
                },
            }
        )

        await _execute_create_simple_helper(
            client, "person", "Anna", "mdi:account", None, None, None, False, False, {}
        )  # fmt: skip

        (create,) = _sent(client, "person/create")
        assert "icon" not in create
        assert (
            _sent(client, "config/entity_registry/update")[0]["icon"] == "mdi:account"
        )

    async def test_person_update_leaves_the_icon_out_of_the_stored_item(self):
        person = {"id": "anna", "name": "Anna", "device_trackers": []}
        entry = {"entity_id": "person.anna", "platform": "person", "unique_id": "anna"}
        client = _client(
            {
                "person/list": {
                    "success": True,
                    "result": {"storage": [person], "config": []},
                },
                "person/update": {"success": True, "result": person},
                "config/entity_registry/get": {"success": True, "result": entry},
            }
        )

        await _execute_update_simple_helper(
            client, "person", "person.anna", "person.anna", None, "mdi:account",
            None, None, None, False, False, {},
        )  # fmt: skip

        (update,) = _sent(client, "person/update")
        assert "icon" not in update
        assert (
            _sent(client, "config/entity_registry/update")[0]["icon"] == "mdi:account"
        )

    async def test_tag_update_without_its_entity_is_a_warning(self):
        tag = {"id": "abc-1", "name": "Front door"}
        client = _client(
            {
                "tag/list": {"success": True, "result": [tag]},
                "tag/update": {"success": True, "result": tag},
                "config/entity_registry/list": {"success": True, "result": []},
            }
        )

        result = await _execute_update_simple_helper(
            client, "tag", "abc-1", "abc-1", None, "mdi:nfc",
            None, None, None, False, False, {},
        )  # fmt: skip

        assert "were not applied" in result["warnings"][0]

    async def test_other_helpers_keep_the_icon_in_their_stored_item(self):
        """Their update replaces the stored item, so a stored icon can be cleared."""
        client = _client(
            {"input_boolean/create": {"success": True, "result": {"id": "lamp"}}}
        )

        await _execute_create_simple_helper(
            client, "input_boolean", "Lamp", "mdi:lamp", None, None, None, False, False, {}
        )  # fmt: skip

        (create,) = _sent(client, "input_boolean/create")
        assert create["icon"] == "mdi:lamp"
        assert _sent(client, "config/entity_registry/update") == []

    async def test_component_create_sends_the_icon_as_a_registry_field(self):
        write = AsyncMock(return_value=None)
        with patch("ha_mcp.tools.config_helpers.create.write_helper_item", write):
            await ZoneTools(_client()).ha_set_zone(
                name="Office", latitude=52.5, longitude=13.4, icon="mdi:briefcase"
            )

        payload = write.call_args.args[3]
        assert "icon" not in payload
        assert write.call_args.kwargs["registry"] == {"icon": "mdi:briefcase"}


class TestListing:
    async def test_without_component_lists_the_home_zone_and_entity_ids(self):
        """Core's zone/list serves stored zones only; the home zone is in the states."""
        client = _client(states={"zone.home": HOME_STATE})

        result = await ZoneTools(client).ha_get_zone()

        by_id = {z["id"]: z for z in result["zones"]}
        assert by_id["office"]["entity_id"] == "zone.office"
        assert by_id["office"]["source"] == "storage"
        assert by_id["home"]["entity_id"] == "zone.home"
        assert by_id["home"]["source"] == "yaml"
        assert by_id["home"]["editable"] is False
        assert by_id["home"]["name"] == "Home"
        assert "warnings" not in result

    async def test_a_zone_can_be_fetched_by_its_entity_id(self):
        result = await ZoneTools(_client()).ha_get_zone(zone_id="zone.office")

        assert result["zone"]["id"] == "office"

    async def test_without_registry_stored_zones_are_not_listed_twice(self):
        """No zone state can be matched to its stored zone without the registry."""
        client = _client(
            {"config/entity_registry/list": REGISTRY_BLOCKED},
            states={"zone.home": HOME_STATE},
        )

        result = await ZoneTools(client).ha_get_zone()

        assert [z["id"] for z in result["zones"]] == ["office"]
        assert "'home'" in result["warnings"][0] and "403" in result["warnings"][0]

    async def test_without_states_the_home_zone_is_named_as_missing(self):
        client = _client(states={"zone.home": HOME_STATE})
        client.get_states.side_effect = HomeAssistantConnectionError("socket closed")

        result = await ZoneTools(client).ha_get_zone()

        assert [z["id"] for z in result["zones"]] == ["office"]
        assert "'home'" in result["warnings"][0]

    async def test_a_zone_missing_from_an_incomplete_listing_is_not_absent(self):
        client = _client(
            {"config/entity_registry/list": REGISTRY_BLOCKED},
            states={"zone.home": HOME_STATE},
        )

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_get_zone(zone_id="home")

        error = _error(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert "403" in error["message"]


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
        self, monkeypatch, domain, zone_id
    ):
        """The zone tools take a zone_id or the zone's entity_id."""
        _, fake_ws = _backup_ws([dict(OFFICE)], [_office_entity()])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        snapshot = await _handler(domain).fetch(None, zone_id)

        assert snapshot == {**OFFICE, "registry_icon": "mdi:briefcase"}

    async def test_snapshot_without_registry_keeps_the_stored_zone(self, monkeypatch):
        async def fake_ws(_client: Any, msg: dict[str, Any]) -> Any:
            if msg["type"] == "zone/list":
                return [OFFICE]
            raise HomeAssistantConnectionError("registry read failed")

        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        assert await _handler("zone").fetch(None, "office") == OFFICE

    @pytest.mark.parametrize("domain", ["zone", "helper_zone"])
    async def test_restore_of_a_removed_zone_creates_it_again(
        self, monkeypatch, domain
    ):
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

    async def test_a_repeated_restore_does_not_add_another_zone(self, monkeypatch):
        """A zone renamed after the snapshot comes back under a new id."""
        zones: list[dict[str, Any]] = []
        snapshot = {**OFFICE, "id": "workplace", "registry_icon": None}
        sent, fake_ws = _backup_ws(zones, [])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        for _ in range(2):
            await _handler("zone").restore(None, "workplace", dict(snapshot))

        assert len([m for m in sent if m["type"] == "zone/create"]) == 1
        assert [z["id"] for z in zones] == ["office"]

    async def test_a_different_zone_with_the_same_name_is_not_overwritten(
        self, monkeypatch
    ):
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

    async def test_a_failed_icon_write_after_a_recreate_is_a_warning(self, monkeypatch):
        sent, fake_ws = _backup_ws([], [], registry_update_error="unauthorized")
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        result = await _handler("zone").restore(
            None, "office", {**OFFICE, "registry_icon": "mdi:briefcase"}
        )

        assert result["restore_mode"] == "recreated"
        assert "registry icon was not" in result["warnings"][0]

    async def test_a_recorded_icon_without_an_entity_is_a_warning(self, monkeypatch):
        _, fake_ws = _backup_ws([dict(OFFICE)], [])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        result = await _handler("zone").restore(
            None, "office", {**OFFICE, "registry_icon": "mdi:briefcase"}
        )

        assert "has no entity" in result["warnings"][0]

    async def test_no_recorded_icon_and_no_entity_is_no_warning(self, monkeypatch):
        _, fake_ws = _backup_ws([dict(OFFICE)], [])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        result = await _handler("zone").restore(
            None, "office", {**OFFICE, "registry_icon": None}
        )

        assert "warnings" not in result

    async def test_restore_resets_a_registry_icon_set_after_the_snapshot(
        self, monkeypatch
    ):
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

    async def test_an_icon_stored_after_the_snapshot_is_a_warning(self, monkeypatch):
        """Core's zone/update merges, so it cannot remove the newer stored icon."""
        _, fake_ws = _backup_ws([{**OFFICE, "icon": "mdi:school"}], [_office_entity()])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        result = await _handler("zone").restore(None, "office", dict(OFFICE))

        assert "mdi:school" in result["warnings"][0]

    async def test_restore_of_an_older_snapshot_leaves_the_registry_alone(
        self, monkeypatch
    ):
        sent, fake_ws = _backup_ws([dict(OFFICE)], [_office_entity()])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        await _handler("zone").restore(None, "office", dict(OFFICE))

        assert [m["type"] for m in sent] == ["zone/update"]

    async def test_other_update_failures_are_not_turned_into_a_create(
        self, monkeypatch
    ):
        sent, fake_ws = _backup_ws([dict(OFFICE)], [], update_error="invalid_format")
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        with pytest.raises(HomeAssistantCommandError):
            await _handler("zone").restore(None, "office", dict(OFFICE))
        assert not [m for m in sent if m["type"] == "zone/create"]
