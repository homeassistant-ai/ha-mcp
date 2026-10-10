"""Zone tools against the answers Home Assistant actually sends (#2632).

Core's zone collection reports a missing item as ``not_found`` with the text
"Unable to find zone_id <id>" (``helpers/collection.py``). The zone tools used
to look for "not found" in that text, so a missing zone came back as a generic
service failure. A storage zone's registry ``unique_id`` is its zone_id; the
home zone and YAML zones have none, and the home zone's state says
``editable: True`` (``components/zone/__init__.py``).
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp import backup_manager
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError
from ha_mcp.tools.tools_zones import ZoneTools

OFFICE_ID = "8e1f0c2b6d5a4f3e9c7b1a2d3e4f5a6b"
OFFICE = {
    "id": OFFICE_ID,
    "name": "Office",
    "latitude": 52.5,
    "longitude": 13.4,
    "radius": 100.0,
    "passive": False,
}
REGISTRY = [
    {"entity_id": "zone.office", "platform": "zone", "unique_id": OFFICE_ID},
    {"entity_id": "light.desk", "platform": "hue", "unique_id": OFFICE_ID},
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


def _not_found(zone_id: str) -> dict[str, Any]:
    """``send_websocket_message``'s envelope for Core's ERR_NOT_FOUND."""
    return {
        "success": False,
        "error": f"Command failed: Unable to find zone_id {zone_id}",
        "error_code": "not_found",
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
        "config/entity_registry/get": {
            "success": True,
            "result": {**REGISTRY[0], "icon": None},
        },
        "config/entity_registry/update": {
            "success": True,
            "result": {"entity_entry": REGISTRY[0]},
        },
    }
    answers.update(replies or {})
    known = {"zone.office": {"entity_id": "zone.office", "attributes": {}}}
    known.update(states or {})

    async def send(message: dict[str, Any]) -> Any:
        return answers[message["type"]]

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


@pytest.fixture(autouse=True)
def _no_component_no_wait():
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
        patch(
            "ha_mcp.tools.tools_zones.wait_for_entity_removed",
            create=True,
            new=AsyncMock(return_value=True),
        ),
    ):
        yield


class TestMissingZoneIsReportedAsNotFound:
    async def test_remove_of_a_deleted_zone_is_resource_not_found(self):
        """A zone Core no longer holds must not read as a failed service call."""
        client = _client({"zone/delete": _not_found(OFFICE_ID)})

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_remove_zone(zone_id=OFFICE_ID)

        assert _error(exc_info)["code"] == "RESOURCE_NOT_FOUND"

    async def test_remove_of_an_unknown_zone_id_is_resource_not_found(self):
        client = _client({"zone/delete": _not_found("nope")})

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_remove_zone(zone_id="nope")

        error = _error(exc_info)
        assert error["code"] == "RESOURCE_NOT_FOUND"
        assert "ha_get_zone" in json.dumps(error)

    async def test_update_of_an_unknown_zone_id_is_resource_not_found(self):
        client = _client({"zone/update": _not_found("nope")})

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_set_zone(zone_id="nope", radius=50)

        assert _error(exc_info)["code"] == "RESOURCE_NOT_FOUND"


class TestYamlZoneIsNamed:
    @pytest.mark.parametrize("zone_id", ["home", "zone.home"])
    async def test_remove_of_the_home_zone_says_it_is_not_editable(self, zone_id):
        """The home zone is not a stored zone, so no delete is sent for it."""
        client = _client(states={"zone.home": HOME_STATE})

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_remove_zone(zone_id=zone_id)

        error = _error(exc_info)
        assert error["code"] == "RESOURCE_NOT_FOUND"
        assert "YAML" in error["message"]
        assert _sent(client, "zone/delete") == []

    async def test_update_of_the_home_zone_says_it_is_not_editable(self):
        client = _client(states={"zone.home": HOME_STATE})

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_set_zone(zone_id="home", radius=50)

        assert "YAML" in _error(exc_info)["message"]
        assert _sent(client, "zone/update") == []


class TestRegistryReadFailureIsNotAbsence:
    async def test_blocked_registry_read_is_not_reported_as_missing_zone(self):
        client = _client(
            {
                "config/entity_registry/list": {
                    "success": False,
                    "error": "WebSocket request blocked (403 Forbidden)",
                    "error_code": None,
                }
            }
        )

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_remove_zone(zone_id=OFFICE_ID)

        error = _error(exc_info)
        assert error["code"] != "RESOURCE_NOT_FOUND"
        assert "403" in error["message"]
        assert _sent(client, "zone/delete") == []


class TestRemoveTargetsTheZoneItself:
    @pytest.mark.parametrize("zone_id", [OFFICE_ID, "zone.office"])
    async def test_remove_by_zone_id_or_entity_id_deletes_the_storage_item(
        self, zone_id
    ):
        client = _client()

        result = await ZoneTools(client).ha_remove_zone(zone_id=zone_id)

        assert _sent(client, "zone/delete") == [
            {"type": "zone/delete", "zone_id": OFFICE_ID}
        ]
        assert result["entity_id"] == "zone.office"


class TestUpdateUsesTheHelperWritePath:
    async def test_new_icon_on_a_zone_without_stored_icon_goes_to_the_registry(self):
        """An icon written into the zone item could never be cleared again (#2643)."""
        client = _client()

        await ZoneTools(client).ha_set_zone(zone_id=OFFICE_ID, icon="mdi:briefcase")

        assert all("icon" not in m for m in _sent(client, "zone/update"))
        assert any(
            m.get("icon") == "mdi:briefcase"
            for m in _sent(client, "config/entity_registry/update")
        )

    async def test_update_by_entity_id_writes_the_zone_item(self):
        client = _client()

        result = await ZoneTools(client).ha_set_zone(zone_id="zone.office", radius=250)

        (update,) = _sent(client, "zone/update")
        assert update["zone_id"] == OFFICE_ID
        assert update["radius"] == 250
        assert result["zone_id"] == OFFICE_ID
        assert result["entity_id"] == "zone.office"
        assert result["updated_fields"] == ["radius"]


class TestCoreValidatesZoneFields:
    async def test_create_leaves_defaults_to_core(self):
        """Core applies radius and passive defaults; the tool must not pin its own."""
        client = _client()

        await ZoneTools(client).ha_set_zone(
            name="Office", latitude=52.5, longitude=13.4
        )

        (create,) = _sent(client, "zone/create")
        assert "radius" not in create
        assert "passive" not in create

    async def test_core_rejection_of_a_field_is_a_validation_error(self):
        client = _client(
            {
                "zone/create": {
                    "success": False,
                    "error": "Command failed: invalid latitude for dictionary "
                    "value @ data['latitude']",
                    "error_code": "invalid_format",
                }
            }
        )

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_set_zone(name="X", latitude=95, longitude=0)

        error = _error(exc_info)
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "latitude" in error["message"]


class TestLegacyListing:
    async def test_without_component_lists_yaml_zones_and_entity_ids(self):
        """Core's zone/list serves storage zones only; the home zone is in the states."""
        client = _client(states={"zone.home": HOME_STATE})

        result = await ZoneTools(client).ha_get_zone()

        by_id = {z["id"]: z for z in result["zones"]}
        assert by_id[OFFICE_ID]["entity_id"] == "zone.office"
        assert by_id[OFFICE_ID]["source"] == "storage"
        assert by_id["home"]["entity_id"] == "zone.home"
        assert by_id["home"]["source"] == "yaml"
        assert by_id["home"]["persons"] == ["person.anna"]


async def test_zone_backup_finds_a_zone_named_by_its_entity_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The zone tools take zone.<name>; without the registry hop no backup was taken."""

    async def fake_ws(_client: Any, msg: dict[str, Any]) -> Any:
        if msg["type"] == "zone/list":
            return [OFFICE]
        if msg["type"] == "config/entity_registry/list":
            return []
        if msg["type"] == "config/entity_registry/get":
            assert msg["entity_id"] == "zone.office"
            return {"unique_id": OFFICE_ID}
        raise AssertionError(f"unexpected ws message: {msg}")

    monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)
    assert await backup_manager._fetch_zone(None, "zone.office") == {
        **OFFICE,
        "registry_icon": None,
    }


async def test_listing_without_registry_does_not_list_stored_zones_twice() -> None:
    """Without the registry no zone state can be matched to its stored zone."""
    client = _client(
        {
            "config/entity_registry/list": {
                "success": False,
                "error": "WebSocket request blocked (403 Forbidden)",
                "error_code": None,
            }
        },
        states={"zone.home": HOME_STATE},
    )

    result = await ZoneTools(client).ha_get_zone()

    assert [z["id"] for z in result["zones"]] == [OFFICE_ID]
    assert any("403" in w for w in result["warnings"])


class TestCreatedZoneIconStaysClearable:
    """A zone keeps an icon in its stored item forever (#2643), so a new
    zone's icon goes to the entity registry, where it can be cleared."""

    async def test_create_writes_the_icon_to_the_registry_only(self):
        client = _client()

        await ZoneTools(client).ha_set_zone(
            name="Office", latitude=52.5, longitude=13.4, icon="mdi:briefcase"
        )

        (create,) = _sent(client, "zone/create")
        assert "icon" not in create
        assert any(
            m.get("icon") == "mdi:briefcase"
            for m in _sent(client, "config/entity_registry/update")
        )

    async def test_other_helpers_keep_the_icon_in_their_stored_item(self):
        """Only a zone shows its stored icon over a cleared registry one."""
        from ha_mcp.tools.config_helpers.create import _execute_create_simple_helper

        client = _client(
            {"input_boolean/create": {"success": True, "result": {"id": "lamp"}}}
        )

        await _execute_create_simple_helper(
            client,
            "input_boolean",
            "Lamp",
            "mdi:lamp",
            None,
            None,
            None,
            False,
            False,
            {},
        )

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


def _backup_ws(
    zone_list: list[dict[str, Any]],
    registry: list[dict[str, Any]],
    update_error: str | None = None,
) -> tuple[list[dict[str, Any]], Any]:
    """A fake ``backup_manager._ws_send`` and the messages it received."""
    sent: list[dict[str, Any]] = []

    async def fake_ws(_client: Any, msg: dict[str, Any]) -> Any:
        sent.append(dict(msg))
        kind = msg["type"]
        if kind == "zone/list":
            return zone_list
        if kind == "config/entity_registry/list":
            return registry
        if kind == "zone/update":
            if update_error:
                from ha_mcp.client.rest_client import HomeAssistantCommandError

                raise HomeAssistantCommandError(
                    f"Command failed: {update_error}", update_error
                )
            return {**msg, "id": msg["zone_id"]}
        if kind == "zone/create":
            created = {k: v for k, v in msg.items() if k != "type"}
            registry.append(
                {
                    "entity_id": "zone.office_2",
                    "platform": "zone",
                    "unique_id": "office",
                }
            )
            return {**created, "id": "office"}
        if kind == "config/entity_registry/update":
            return {"entity_entry": {}}
        raise AssertionError(f"unexpected ws message: {msg}")

    return sent, fake_ws


ZONE_ENTITY = {
    "entity_id": "zone.office",
    "platform": "zone",
    "unique_id": OFFICE_ID,
    "icon": "mdi:briefcase",
}


class TestZoneBackup:
    @pytest.mark.parametrize("domain", ["zone", "helper_zone"])
    async def test_snapshot_records_the_registry_icon(self, monkeypatch, domain):
        sent, fake_ws = _backup_ws([OFFICE], [ZONE_ENTITY])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)
        handler = _handler(domain)

        snapshot = await handler.fetch(None, OFFICE_ID)

        assert snapshot == {**OFFICE, "registry_icon": "mdi:briefcase"}

    @pytest.mark.parametrize("domain", ["zone", "helper_zone"])
    async def test_restore_of_a_removed_zone_creates_it_again(
        self, monkeypatch, domain
    ):
        """Core's zone/update cannot restore a zone that no longer exists."""
        registry: list[dict[str, Any]] = []
        sent, fake_ws = _backup_ws([], registry, update_error="not_found")
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        result = await _handler(domain).restore(
            None, OFFICE_ID, {**OFFICE, "registry_icon": "mdi:briefcase"}
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

    async def test_restore_resets_a_registry_icon_set_after_the_snapshot(
        self, monkeypatch
    ):
        sent, fake_ws = _backup_ws([OFFICE], [ZONE_ENTITY])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        await _handler("zone").restore(
            None, OFFICE_ID, {**OFFICE, "registry_icon": None}
        )

        (update,) = [m for m in sent if m["type"] == "zone/update"]
        assert "registry_icon" not in update
        assert {
            "type": "config/entity_registry/update",
            "entity_id": "zone.office",
            "icon": None,
        } in sent

    async def test_restore_of_an_older_snapshot_leaves_the_registry_alone(
        self, monkeypatch
    ):
        sent, fake_ws = _backup_ws([OFFICE], [ZONE_ENTITY])
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        await _handler("zone").restore(None, OFFICE_ID, dict(OFFICE))

        assert [m["type"] for m in sent] == ["zone/update"]

    async def test_other_update_failures_are_not_turned_into_a_create(
        self, monkeypatch
    ):
        from ha_mcp.client.rest_client import HomeAssistantCommandError

        sent, fake_ws = _backup_ws([], [], update_error="invalid_format")
        monkeypatch.setattr(backup_manager, "_ws_send", fake_ws)

        with pytest.raises(HomeAssistantCommandError):
            await _handler("zone").restore(None, OFFICE_ID, dict(OFFICE))
        assert not [m for m in sent if m["type"] == "zone/create"]


def _handler(domain: str) -> Any:
    mgr = MagicMock()
    handlers: dict[str, Any] = {}
    mgr.register = lambda h: handlers.__setitem__(h.domain, h)
    backup_manager.register_default_handlers(mgr, None)
    return handlers[domain]
