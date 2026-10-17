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
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantConnectionError,
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


@pytest.fixture(autouse=True)
def _no_component(removed: AsyncMock, registered: AsyncMock) -> Iterator[None]:
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
    async def test_core_not_found_on_delete_is_resource_not_found(self) -> None:
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
    async def test_unknown_zone_is_resource_not_found_before_any_write(
        self, call: str
    ) -> None:
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
    async def test_home_zone_is_refused_as_not_stored(
        self, call: str, zone_id: str
    ) -> None:
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

    async def test_an_empty_state_reply_is_a_read_failure(self) -> None:
        """A non-JSON reply comes back as {}: neither a zone nor its absence."""
        client = _client(states={"zone.ghost": {}})

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_remove_zone(zone_id="ghost")

        error = _error(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert "Could not read the state" in error["message"]

    async def test_a_failed_zone_list_is_a_read_failure_not_a_yaml_zone(self) -> None:
        client = _client({"zone/list": {"success": False, "error": "timeout"}})

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_remove_zone(zone_id="office")

        assert _error(exc_info)["code"] == "SERVICE_CALL_FAILED"
        assert _sent(client, "zone/delete") == []

    async def test_a_registry_entry_of_no_stored_zone_is_not_written(self) -> None:
        """zone.ghost's unique_id names no stored zone, so its backup holds
        nothing to restore."""
        ghost = {"entity_id": "zone.ghost", "platform": "zone", "unique_id": "ghost"}
        client = _client(
            {"config/entity_registry/list": {"success": True, "result": [ghost]}}
        )

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_remove_zone(zone_id="zone.ghost")

        assert _error(exc_info)["code"] == "RESOURCE_NOT_FOUND"
        assert _sent(client, "zone/delete") == []

    async def test_a_bare_id_no_zone_has_deletes_no_zone_by_its_entity(self) -> None:
        """After zone work's removal zone.work is still office's entity: a
        repeated removal of work would delete office."""
        renamed = [{**OFFICE_ENTRY, "entity_id": "zone.work"}]
        client = _client(
            {"config/entity_registry/list": {"success": True, "result": renamed}},
            states={"zone.work": {"entity_id": "zone.work", "attributes": {}}},
        )

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_remove_zone(zone_id="work")

        error = _error(exc_info)
        assert error["code"] == "RESOURCE_NOT_FOUND"
        assert "not a stored zone" not in error["message"]
        assert _sent(client, "zone/delete") == []

    async def test_a_bare_id_no_zone_has_points_to_the_entity_id(self) -> None:
        client = _client(
            {"config/entity_registry/list": {"success": True, "result": []}}
        )

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_remove_zone(zone_id="work")

        error = _error(exc_info)
        hints = [error.get("suggestion", ""), *error.get("suggestions", [])]
        assert any("full entity_id" in s for s in hints)

    async def test_blocked_registry_read_is_not_a_missing_zone(self) -> None:
        client = _client({"config/entity_registry/list": REGISTRY_BLOCKED})

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_remove_zone(zone_id="office")

        error = _error(exc_info)
        assert error["code"] != "RESOURCE_NOT_FOUND"
        assert "403" in error["message"]
        assert _sent(client, "zone/delete") == []

    @pytest.mark.parametrize("zone_id", ["office", "zone.office"])
    async def test_remove_by_zone_id_or_entity_id_deletes_the_storage_item(
        self, zone_id: str
    ) -> None:
        client = _client()

        result = await ZoneTools(client).ha_remove_zone(zone_id=zone_id)

        assert _sent(client, "zone/delete") == [
            {"type": "zone/delete", "zone_id": "office"}
        ]
        assert result["entity_id"] == "zone.office"

    async def test_a_storage_id_wins_over_a_renamed_entity_of_the_same_text(
        self,
    ) -> None:
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
    async def test_wait_false_skips_the_removal_check(self, removed: AsyncMock) -> None:
        await ZoneTools(_client()).ha_remove_zone(zone_id="office", wait=False)

        removed.assert_not_awaited()

    async def test_a_zone_still_present_after_the_wait_is_a_warning(
        self, removed: AsyncMock
    ) -> None:
        removed.return_value = False

        result = await ZoneTools(_client()).ha_remove_zone(zone_id="office")

        assert result["success"] is True
        assert "still present" in result["warnings"][0]

    async def test_a_failed_removal_check_is_a_warning(
        self, removed: AsyncMock
    ) -> None:
        removed.side_effect = HomeAssistantConnectionError("socket closed")

        result = await ZoneTools(_client()).ha_remove_zone(zone_id="office")

        assert "verification failed" in result["warnings"][0]

    async def test_a_zone_without_an_entity_is_removed_without_a_guessed_one(
        self, removed: AsyncMock
    ) -> None:
        """zone.office would be named and waited on, though no registry entry
        links it to the zone."""
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

    async def test_without_wait_a_zone_without_an_entity_says_only_why(
        self, removed: AsyncMock
    ) -> None:
        client = _client(
            {"config/entity_registry/list": {"success": True, "result": []}}
        )

        result = await ZoneTools(client).ha_remove_zone(zone_id="office", wait=False)

        assert result["warnings"] == ["Zone office had no entity in the registry."]

    @pytest.mark.parametrize("deleted", [True, False], ids=["removed", "failed"])
    async def test_a_zone_without_an_entity_is_named_by_its_storage_id(
        self, removed: AsyncMock, deleted: bool
    ) -> None:
        """Neither the reply nor the error says "zone None"."""
        replies: dict[str, Any] = {
            "config/entity_registry/list": {"success": True, "result": []}
        }
        if not deleted:
            replies["zone/delete"] = {"success": False, "error": "boom"}
        tools = ZoneTools(_client(replies))

        if deleted:
            message = (await tools.ha_remove_zone(zone_id="office"))["message"]
        else:
            with pytest.raises(ToolError) as exc_info:
                await tools.ha_remove_zone(zone_id="office")
            message = _error(exc_info)["message"]

        assert "office" in message and "None" not in message


class TestZoneWrites:
    async def test_new_icon_on_a_zone_without_stored_icon_goes_to_the_registry(
        self,
    ) -> None:
        """An icon written into the zone item could never be cleared again (#2643)."""
        client = _client()

        await ZoneTools(client).ha_set_zone(zone_id="office", icon="mdi:briefcase")

        assert all("icon" not in m for m in _sent(client, "zone/update"))
        assert any(
            m.get("icon") == "mdi:briefcase"
            for m in _sent(client, "config/entity_registry/update")
        )

    async def test_update_by_entity_id_writes_the_zone_item(self) -> None:
        client = _client()

        result = await ZoneTools(client).ha_set_zone(zone_id="zone.office", radius=250)

        (update,) = _sent(client, "zone/update")
        assert update["zone_id"] == "office"
        assert update["radius"] == 250
        assert result["zone_id"] == "office"
        assert result["entity_id"] == "zone.office"
        assert result["updated_fields"] == ["radius"]

    @pytest.mark.parametrize(
        ("stored", "registry", "icon", "shown"),
        [
            (None, "mdi:briefcase", None, "mdi:briefcase"),
            ("mdi:map", "mdi:briefcase", None, "mdi:briefcase"),
            ("mdi:map", None, None, "mdi:map"),
            (None, "mdi:briefcase", "", None),
        ],
        ids=["left-alone", "over-the-stored-icon", "stored-only", "cleared"],
    )
    async def test_an_update_reply_shows_the_icon_the_zone_entity_shows(
        self,
        stored: str | None,
        registry: str | None,
        icon: str | None,
        shown: str | None,
    ) -> None:
        """The zone entity shows its registry icon over the stored one, and
        #2643 keeps a zone's icon there. An update that leaves the icon alone
        reports the icon of this zone, as ha_get_zone does; one that clears it
        does not bring it back."""
        zone = {**OFFICE, "icon": stored} if stored else OFFICE
        other = {"entity_id": "zone.gym", "platform": "zone", "unique_id": "gym"}
        entries = [
            {**other, "icon": "mdi:dumbbell"},
            {**OFFICE_ENTRY, "icon": registry},
        ]
        client = _client(
            {
                "zone/list": {"success": True, "result": [zone]},
                "zone/update": {"success": True, "result": zone},
                "config/entity_registry/list": {"success": True, "result": entries},
            }
        )

        result = await ZoneTools(client).ha_set_zone(
            zone_id="office", radius=200, icon=icon
        )

        assert result["zone_data"].get("icon") == shown

    async def test_wait_false_skips_the_update_check(
        self, registered: AsyncMock
    ) -> None:
        await ZoneTools(_client()).ha_set_zone(zone_id="office", radius=250, wait=False)

        registered.assert_not_awaited()

    async def test_create_leaves_defaults_to_core(self) -> None:
        """Core applies radius and passive defaults; the tool must not pin its own."""
        client = _client()

        await ZoneTools(client).ha_set_zone(
            name="Office", latitude=52.5, longitude=13.4
        )

        (create,) = _sent(client, "zone/create")
        assert "radius" not in create
        assert "passive" not in create

    @pytest.mark.parametrize("zone_id", [None, "office"])
    async def test_blank_name_is_refused(self, zone_id: str) -> None:
        client = _client()

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_set_zone(
                name="  ", latitude=52.5, longitude=13.4, zone_id=zone_id
            )

        assert "name cannot be blank" in _error(exc_info)["message"]
        assert not _sent(client, "zone/create") and not _sent(client, "zone/update")


class TestListing:
    async def test_without_component_lists_the_home_zone_and_entity_ids(self) -> None:
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

    async def test_a_stored_zone_named_home_wins_over_the_home_zone(self) -> None:
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

    def test_a_stored_zone_wins_whatever_the_row_order(self) -> None:
        """The component lists zones in state-machine order, home zone first."""
        home_row = {"id": "home", "entity_id": "zone.home", "editable": False}
        stored = {"id": "home", "entity_id": "zone.home_2", "editable": True}

        result = _build_zone_result([home_row, stored], "home")

        assert result["zone"] is stored

    async def test_a_zone_can_be_fetched_by_its_entity_id(self) -> None:
        result = await ZoneTools(_client()).ha_get_zone(zone_id="zone.office")

        assert result["zone"]["id"] == "office"

    async def test_without_registry_stored_zones_are_not_listed_twice(self) -> None:
        """No zone state can be matched to its stored zone without the registry."""
        client = _client(
            {"config/entity_registry/list": REGISTRY_BLOCKED},
            states={"zone.home": HOME_STATE},
        )

        result = await ZoneTools(client).ha_get_zone()

        assert [z["id"] for z in result["zones"]] == ["office"]
        assert "'home'" in result["warnings"][0] and "403" in result["warnings"][0]

    async def test_without_registry_the_listing_names_the_missing_icons(self) -> None:
        client = _client({"config/entity_registry/list": REGISTRY_BLOCKED})

        result = await ZoneTools(client).ha_get_zone()

        assert "icons set in the entity registry" in result["warnings"][0]

    async def test_without_states_the_home_zone_is_named_as_missing(self) -> None:
        client = _client(states={"zone.home": HOME_STATE})
        client.get_states.side_effect = HomeAssistantConnectionError("socket closed")

        result = await ZoneTools(client).ha_get_zone()

        assert [z["id"] for z in result["zones"]] == ["office"]
        assert "'home'" in result["warnings"][0]

    async def test_a_zone_missing_from_an_incomplete_listing_is_not_absent(
        self,
    ) -> None:
        client = _client(
            {"config/entity_registry/list": REGISTRY_BLOCKED},
            states={"zone.home": HOME_STATE},
        )

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(client).ha_get_zone(zone_id="home")

        error = _error(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert "403" in error["message"]
