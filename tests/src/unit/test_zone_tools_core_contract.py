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

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.config_helpers.create import _execute_create_simple_helper
from ha_mcp.tools.config_helpers.update import (
    _execute_update_simple_helper,
    _update_via_component,
)
from ha_mcp.tools.tools_zones import ZoneTools, _build_zone_result

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
LAMP = {
    "entity_id": "input_boolean.lamp",
    "platform": "input_boolean",
    "unique_id": "lamp",
}
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
        answer = answers[message["type"]]
        if isinstance(answer, Exception):
            raise answer
        # A fresh copy each time, like a real reply: the tools write into results.
        return copy.deepcopy(answer)

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


@pytest.fixture
def registered() -> AsyncMock:
    return AsyncMock(return_value=True)


_FETCH_SCHEMAS = "ha_mcp.tools.config_helpers.icon_route.fetch_helper_schemas"
# Core's person and tag fields, as the component serves them: no icon among them.
_PERSON_SCHEMAS = {
    "person": {
        action: [{"name": f} for f in ("name", "user_id", "device_trackers", "picture")]
        for action in ("create", "update")
    }
}
_TAG_SCHEMAS = {
    "tag": {
        "create": [{"name": f} for f in ("tag_id", "name", "description")],
        "update": [{"name": f} for f in ("name", "description")],
    }
}


@pytest.fixture(autouse=True)
def _no_component(removed: AsyncMock, registered: AsyncMock):
    """Serve every call through Core's own WebSocket commands, without waiting."""
    with (
        patch(
            "ha_mcp.tools.tools_zones.get_component_caps",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "ha_mcp.tools.config_helpers.update.read_stored_helper_item",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "ha_mcp.tools.config_helpers.create.write_helper_item",
            new=AsyncMock(return_value=None),
        ),
        patch(_FETCH_SCHEMAS, new=AsyncMock(return_value=None)),
        patch(
            "ha_mcp.tools.config_helpers.update.wait_for_entity_registered",
            new=registered,
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

    async def test_an_empty_state_reply_is_a_read_failure(self):
        """A non-JSON reply comes back as {}: neither a zone nor its absence."""
        client = _client(states={"zone.ghost": {}})

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_remove_zone(zone_id="ghost")

        error = _error(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert "Could not read the state" in error["message"]

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
        work = {**OFFICE, "id": "work", "name": "Work"}
        client = _client(
            {
                "config/entity_registry/list": {"success": True, "result": renamed},
                "zone/list": {"success": True, "result": [OFFICE, work]},
            }
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

    async def test_a_zone_without_an_entity_is_removed_without_a_guessed_one(
        self, removed
    ):
        """zone.office would be named and waited on, though no entity holds it."""
        client = _client(
            {"config/entity_registry/list": {"success": True, "result": []}}
        )

        result = await ZoneTools(client).ha_remove_zone(zone_id="office")

        assert _sent(client, "zone/delete") == [
            {"type": "zone/delete", "zone_id": "office"}
        ]
        assert result["entity_id"] is None
        assert "not waited on" in result["warnings"][0]
        removed.assert_not_awaited()


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

    async def test_wait_false_skips_the_update_check(self, registered):
        await ZoneTools(_client()).ha_set_zone(zone_id="office", radius=250, wait=False)

        registered.assert_not_awaited()

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
    """A new zone's icon goes to the entity registry, where it stays clearable.

    Core's zone/update merges into the stored item and rejects an empty icon, so
    an icon stored in a zone can never be removed (#2643). Person and tag have no
    icon field in Core's schemas, so their icon goes to the registry too. A
    created person's, tag's or zone's entity is looked up in the registry.
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

    @pytest.mark.parametrize(
        "failure",
        [REGISTRY_BLOCKED, HomeAssistantConnectionError("403 socket closed")],
        ids=["failure reply", "dropped connection"],
    )
    async def test_a_failed_registry_read_after_create_is_a_warning(self, failure):
        """The helper exists: an error would make a retrying caller create it
        twice."""
        client = _client(
            {
                "input_boolean/create": {"success": True, "result": {"id": "lamp"}},
                "config/entity_registry/list": failure,
            }
        )

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=None)):
            result = await _execute_create_simple_helper(
                client, "input_boolean", "Lamp", "mdi:lamp", None, None, None,
                False, False, {},
            )  # fmt: skip

        assert result["success"] is True
        assert "could not be read" in result["warnings"][0]
        assert "403" in result["warnings"][0]

    async def test_a_category_without_its_entity_is_named_as_not_applied(self):
        client = _client(
            {
                "person/create": {"success": True, "result": {"id": "anna"}},
                "config/entity_registry/list": {"success": True, "result": []},
            }
        )

        result = await _execute_create_simple_helper(
            client, "person", "Anna", None, None, None, "family", False, False, {}
        )  # fmt: skip

        assert "the category given were not applied" in result["warnings"][0]

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

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=_PERSON_SCHEMAS)):
            await _execute_create_simple_helper(
                client, "person", "Anna", "mdi:account", None, None, None,
                False, False, {},
            )  # fmt: skip

        (create,) = _sent(client, "person/create")
        assert "icon" not in create
        assert (
            _sent(client, "config/entity_registry/update")[0]["icon"] == "mdi:account"
        )

    async def test_tag_create_writes_the_icon_to_the_registry(self):
        client = _client(
            {
                "tag/create": {"success": True, "result": {"id": "abc-1"}},
                "config/entity_registry/list": {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "tag.front_door",
                            "platform": "tag",
                            "unique_id": "abc-1",
                        }
                    ],
                },
            }
        )

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=_TAG_SCHEMAS)):
            await _execute_create_simple_helper(
                client, "tag", "Front door", "mdi:nfc", None, None, None,
                False, False, {},
            )  # fmt: skip

        (create,) = _sent(client, "tag/create")
        assert "icon" not in create
        assert _sent(client, "config/entity_registry/update")[0]["icon"] == "mdi:nfc"

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

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=_PERSON_SCHEMAS)):
            await _execute_update_simple_helper(
                client, "person", "person.anna", "person.anna", None, "mdi:account",
                None, None, None, False, False, {},
            )  # fmt: skip

        (update,) = _sent(client, "person/update")
        assert "icon" not in update
        assert (
            _sent(client, "config/entity_registry/update")[0]["icon"] == "mdi:account"
        )

    async def test_component_person_create_sends_the_icon_as_a_registry_field(self):
        write = AsyncMock(return_value=None)
        with (
            patch("ha_mcp.tools.config_helpers.create.write_helper_item", write),
            patch(_FETCH_SCHEMAS, AsyncMock(return_value=_PERSON_SCHEMAS)),
        ):
            await _execute_create_simple_helper(
                _client(
                    {
                        "person/create": {"success": True, "result": {"id": "anna"}},
                        "config/entity_registry/list": {"success": True, "result": []},
                    }
                ), "person", "Anna", "mdi:account", None, None, None,
                False, False, {},
            )  # fmt: skip

        assert "icon" not in write.call_args.args[3]
        assert write.call_args.kwargs["registry"] == {"icon": "mdi:account"}

    @pytest.mark.parametrize(
        ("helper_type", "schemas", "item", "in_item"),
        [
            ("person", _PERSON_SCHEMAS, {"id": "anna", "name": "Anna"}, False),
            (
                "input_boolean",
                {"input_boolean": {"update": [{"name": "name"}, {"name": "icon"}]}},
                {"id": "anna", "name": "Anna"},
                True,
            ),
        ],
        ids=["person", "input_boolean"],
    )
    async def test_component_update_follows_the_schema_and_sets_the_registry(
        self, helper_type, schemas, item, in_item
    ):
        """The registry always takes the icon; the item only where Core has one."""
        write = AsyncMock(return_value=None)
        read = AsyncMock(return_value={"item": item, "item_id": "anna"})
        with (
            patch("ha_mcp.tools.config_helpers.update.read_stored_helper_item", read),
            patch("ha_mcp.tools.config_helpers.update.write_helper_item", write),
            patch(_FETCH_SCHEMAS, AsyncMock(return_value=schemas)),
        ):
            await _update_via_component(
                _client(), helper_type, f"{helper_type}.anna", f"{helper_type}.anna",
                None, "mdi:account", None, None, None, {},
            )  # fmt: skip

        assert ("icon" in write.call_args.args[3]) is in_item
        assert write.call_args.kwargs["registry"] == {"icon": "mdi:account"}

    async def test_an_icon_the_schema_declares_lands_in_item_and_registry(self):
        stored = {"id": "lamp", "name": "Lamp", "icon": "mdi:lamp"}
        entry = {
            "entity_id": "input_boolean.lamp",
            "platform": "input_boolean",
            "unique_id": "lamp",
        }
        client = _client(
            {
                "input_boolean/list": {"success": True, "result": [stored]},
                "input_boolean/update": {"success": True, "result": stored},
                "config/entity_registry/get": {"success": True, "result": entry},
            }
        )
        schemas = {"input_boolean": {"update": [{"name": "name"}, {"name": "icon"}]}}

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=schemas)):
            await _execute_update_simple_helper(
                client, "input_boolean", "input_boolean.lamp", "input_boolean.lamp",
                None, "mdi:lightbulb", None, None, None, False, False, {},
            )  # fmt: skip

        (update,) = _sent(client, "input_boolean/update")
        assert update["icon"] == "mdi:lightbulb"
        assert (
            _sent(client, "config/entity_registry/update")[0]["icon"] == "mdi:lightbulb"
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
            client, "tag", "abc-1", "abc-1", None, None,
            "hall", None, None, True, False, {},
        )  # fmt: skip

        assert "the area given were not applied" in result["warnings"][0]
        assert "not waited on" in result["warnings"][0]

    async def test_a_failed_registry_read_on_tag_update_is_a_warning(self):
        """Even with no registry field given, the missing entity_id is named."""
        tag = {"id": "abc-1", "name": "Front door"}
        client = _client(
            {
                "tag/list": {"success": True, "result": [tag]},
                "tag/update": {"success": True, "result": tag},
                "config/entity_registry/list": REGISTRY_BLOCKED,
            }
        )

        result = await _execute_update_simple_helper(
            client, "tag", "tag.abc-1", "abc-1", "Front", None,
            None, None, None, False, False, {},
        )  # fmt: skip

        assert "could not be read" in result["warnings"][0]

    async def test_other_helpers_keep_the_icon_in_their_stored_item(self):
        """Core's schema declares the icon: it stays in the stored item."""
        client = _client(
            {
                "input_boolean/create": {"success": True, "result": {"id": "lamp"}},
                "config/entity_registry/list": {"success": True, "result": [LAMP]},
            }
        )
        schemas = {"input_boolean": {"create": [{"name": "name"}, {"name": "icon"}]}}

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=schemas)):
            await _execute_create_simple_helper(
                client, "input_boolean", "Lamp", "mdi:lamp", None, None, None,
                False, False, {},
            )  # fmt: skip

        (create,) = _sent(client, "input_boolean/create")
        assert create["icon"] == "mdi:lamp"
        assert _sent(client, "config/entity_registry/update") == []

    async def test_an_icon_without_a_known_field_goes_to_the_registry(self):
        """The create sends the icon where ``collection_icon`` routes it; its
        cases are pinned in test_helper_icon_route."""
        client = _client(
            {
                "input_boolean/create": {"success": True, "result": {"id": "lamp"}},
                "config/entity_registry/list": {"success": True, "result": [LAMP]},
            }
        )

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=None)):
            await _execute_create_simple_helper(
                client, "input_boolean", "Lamp", "mdi:lamp", None, None, None,
                False, False, {},
            )  # fmt: skip

        (create,) = _sent(client, "input_boolean/create")
        assert "icon" not in create
        assert _sent(client, "config/entity_registry/update")[0]["icon"] == "mdi:lamp"

    async def test_the_registry_icon_goes_to_the_new_helper_s_own_entity(self):
        """A renamed helper holds input_boolean.lamp, so Core names the new one
        input_boolean.lamp_2: <type>.<id> would give the icon to the other."""
        renamed = {**LAMP, "unique_id": "other"}
        created = {**LAMP, "entity_id": "input_boolean.lamp_2"}
        client = _client(
            {
                "input_boolean/create": {"success": True, "result": {"id": "lamp"}},
                "config/entity_registry/list": {
                    "success": True,
                    "result": [renamed, created],
                },
            }
        )

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=None)):
            result = await _execute_create_simple_helper(
                client, "input_boolean", "Lamp", "mdi:lamp", None, None, None,
                False, False, {},
            )  # fmt: skip

        (update,) = _sent(client, "config/entity_registry/update")
        assert update["entity_id"] == "input_boolean.lamp_2"
        assert result["entity_id"] == "input_boolean.lamp_2"

    async def test_an_icon_for_a_helper_without_a_registry_entry_is_not_applied(
        self,
    ):
        """No entry proves which entity is the new helper's: nothing is written."""
        renamed = {**LAMP, "unique_id": "other"}
        client = _client(
            {
                "input_boolean/create": {"success": True, "result": {"id": "lamp"}},
                "config/entity_registry/list": {"success": True, "result": [renamed]},
            }
        )

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=None)):
            result = await _execute_create_simple_helper(
                client, "input_boolean", "Lamp", "mdi:lamp", None, None, None,
                False, False, {},
            )  # fmt: skip

        assert _sent(client, "config/entity_registry/update") == []
        assert any("icon given were not applied" in w for w in result["warnings"])

    async def test_a_cleared_icon_also_leaves_the_stored_item(self):
        """Clearing only the registry would bring an older stored icon back."""
        stored = {"id": "lamp", "name": "Lamp", "icon": "mdi:lamp"}
        entry = {
            "entity_id": "input_boolean.lamp",
            "platform": "input_boolean",
            "unique_id": "lamp",
        }
        client = _client(
            {
                "input_boolean/list": {"success": True, "result": [stored]},
                "input_boolean/update": {"success": True, "result": stored},
                "config/entity_registry/get": {"success": True, "result": entry},
            }
        )

        with patch(_FETCH_SCHEMAS, AsyncMock(return_value=None)):
            await _execute_update_simple_helper(
                client, "input_boolean", "input_boolean.lamp", "input_boolean.lamp",
                None, "", None, None, None, False, False, {},
            )  # fmt: skip

        (update,) = _sent(client, "input_boolean/update")
        assert "icon" not in update
        assert _sent(client, "config/entity_registry/update")[0]["icon"] is None

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

    async def test_a_stored_zone_named_home_wins_over_the_home_zone(self):
        stored_home = {**OFFICE, "id": "home", "name": "Home"}
        client = _client(
            {
                "zone/list": {"success": True, "result": [stored_home]},
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

        result = await ZoneTools(client).ha_get_zone(zone_id="home")

        assert result["zone"]["entity_id"] == "zone.home_2"

    def test_a_stored_zone_wins_whatever_the_row_order(self):
        """The component lists zones in state-machine order, home zone first."""
        home_row = {"id": "home", "entity_id": "zone.home", "editable": False}
        stored = {"id": "home", "entity_id": "zone.home_2", "editable": True}

        result = _build_zone_result([home_row, stored], "home")

        assert result["zone"] is stored

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
