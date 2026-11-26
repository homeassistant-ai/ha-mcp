"""Zone tools on a real ``hass``, through the server's own client (#2632).

Core reports a missing zone as ``not_found`` with the text "Unable to find
zone_id <id>" (``helpers/collection.py``). A stored zone's id is its slugified
name and its registry ``unique_id``; its entity_id also follows the name but is
picked separately, so the two can differ (``zone.home_2`` for a zone named
"Home"). The home zone has no ``unique_id`` and so no registry entry. Without
Core's ``config`` integration the entity registry commands are unknown, and
without ``zone`` so is ``zone/list``: those are Core's own failed reads.
"""

from __future__ import annotations

import json
from contextlib import AbstractContextManager
from functools import partial
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.zone.const import DATA_ZONE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools import websocket_api as component_ws
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import tools_zones
from ha_mcp.tools.config_helpers import update as helper_update
from ha_mcp.tools.tools_zones import ZoneTools

from ._backup_target_identity_core import create_named
from ._client_spies import sent_messages
from ._core_setup import with_config_component
from ._receipt_faults import at_entity_wait, lose_state_reads, on_receipt
from ._tool_errors import error_of

WAITS = pytest.mark.parametrize("wait", [True, False], ids=["wait", "no-wait"])
CALLS = pytest.mark.parametrize("call", ["remove", "update"])
# What Core answers for a command of an integration that is not set up.
UNKNOWN_COMMAND = "Unknown command"


async def _create(client: HomeAssistantClient, name: str = "Office") -> str:
    """A stored zone called ``name``; its storage id."""
    return await create_named(client, "zone", name, latitude=52.5, longitude=13.4)


@pytest.fixture
async def unregistered_client(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """``core_client`` on a Core holding zone "Office", without the ``config``
    integration that serves the entity registry commands."""
    assert await async_setup_component(hass, "zone", {})
    await _create(core_client)
    return core_client


@pytest.fixture
async def office_client(
    hass: HomeAssistant, unregistered_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The same Core with the entity registry commands."""
    return await with_config_component(hass, unregistered_client)


def _stored(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """Core's stored zones by id."""
    zones = hass.data[DATA_ZONE].storage_collection.async_items()
    return {zone["id"]: zone for zone in zones}


def _deleted_at_receipt(
    hass: HomeAssistant, zone_id: str
) -> AbstractContextManager[None]:
    """Core removes ``zone_id`` as the tool's ``zone/delete`` reaches it, so
    its own handler answers ``not_found``."""
    collection = hass.data[DATA_ZONE].storage_collection
    return on_receipt(
        hass,
        "zone/delete",
        lambda msg: hass.async_create_task(collection.async_delete_item(zone_id)),
    )


def _sent(spy: AsyncMock, kind: str) -> list[dict[str, Any]]:
    return [c.args[0] for c in spy.call_args_list if c.args[0]["type"] == kind]


def _writes(spy: AsyncMock) -> list[dict[str, Any]]:
    return _sent(spy, "zone/delete") + _sent(spy, "zone/update")


async def _change(tools: ZoneTools, call: str, zone_id: str) -> None:
    if call == "remove":
        await tools.ha_remove_zone(zone_id=zone_id)
    else:
        await tools.ha_set_zone(zone_id=zone_id, radius=50)


class TestZoneLookup:
    async def test_a_zone_core_has_already_removed_is_resource_not_found(
        self, hass: HomeAssistant, office_client: HomeAssistantClient
    ) -> None:
        """Core listed the zone, then removed it before the delete arrived."""
        with (
            _deleted_at_receipt(hass, "office"),
            pytest.raises(ToolError) as exc_info,
        ):
            await ZoneTools(office_client).ha_remove_zone(zone_id="office")
        await hass.async_block_till_done()

        assert error_of(exc_info)["code"] == "RESOURCE_NOT_FOUND"

    @CALLS
    async def test_unknown_zone_is_resource_not_found_before_any_write(
        self, office_client: HomeAssistantClient, call: str
    ) -> None:
        with (
            sent_messages(office_client) as spy,
            pytest.raises(ToolError) as exc_info,
        ):
            await _change(ZoneTools(office_client), call, "nope")

        error = error_of(exc_info)
        assert error["code"] == "RESOURCE_NOT_FOUND"
        assert "ha_get_zone" in json.dumps(error)
        assert _writes(spy) == []

    @pytest.mark.parametrize("zone_id", ["home", "zone.home"])
    async def test_home_zone_removal_is_refused_as_not_stored(
        self, office_client: HomeAssistantClient, zone_id: str
    ) -> None:
        """Core's home zone has a state but no registry entry: nothing is sent."""
        with (
            sent_messages(office_client) as spy,
            pytest.raises(ToolError) as exc_info,
        ):
            await _change(ZoneTools(office_client), "remove", zone_id)

        assert "not a stored zone" in error_of(exc_info)["message"]
        assert _writes(spy) == []

    async def test_a_zone_list_core_refuses_is_a_read_failure_not_a_missing_zone(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """Core cannot list the stored zones: whether zone office exists is
        unknown, not answered."""
        assert await async_setup_component(hass, "config", {})

        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(core_client).ha_remove_zone(zone_id="office")

        error = error_of(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert UNKNOWN_COMMAND in error["message"]

    async def test_a_registry_entry_of_no_stored_zone_is_not_deleted(
        self, office_client: HomeAssistantClient, entity_registry: er.EntityRegistry
    ) -> None:
        """The entry's unique_id names no stored zone: there is no zone to delete."""
        ghost = entity_registry.async_get_or_create("zone", "zone", "ghost")

        with (
            sent_messages(office_client) as spy,
            pytest.raises(ToolError) as exc_info,
        ):
            await ZoneTools(office_client).ha_remove_zone(zone_id=ghost.entity_id)

        assert error_of(exc_info)["code"] == "RESOURCE_NOT_FOUND"
        assert _sent(spy, "zone/delete") == []

    async def test_a_bare_id_no_zone_has_deletes_no_zone_by_its_entity(
        self,
        hass: HomeAssistant,
        office_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
    ) -> None:
        """Zone office's entity was renamed zone.work: a repeated removal of
        work must not delete office."""
        entity_registry.async_update_entity("zone.office", new_entity_id="zone.work")
        await hass.async_block_till_done()

        with (
            sent_messages(office_client) as spy,
            pytest.raises(ToolError) as exc_info,
        ):
            await ZoneTools(office_client).ha_remove_zone(zone_id="work")

        error = error_of(exc_info)
        assert error["code"] == "RESOURCE_NOT_FOUND"
        assert "not a stored zone" not in error["message"]
        assert _sent(spy, "zone/delete") == []

    async def test_a_bare_id_no_zone_has_points_to_the_entity_id(
        self, office_client: HomeAssistantClient
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(office_client).ha_remove_zone(zone_id="work")

        error = error_of(exc_info)
        hints = [error.get("suggestion", ""), *error.get("suggestions", [])]
        assert any("full entity_id" in s for s in hints)

    async def test_an_unreadable_registry_is_not_a_missing_zone(
        self, hass: HomeAssistant, unregistered_client: HomeAssistantClient
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(unregistered_client).ha_remove_zone(zone_id="office")

        error = error_of(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert UNKNOWN_COMMAND in error["message"]
        assert "office" in _stored(hass)

    @pytest.mark.parametrize("zone_id", ["office", "zone.office"])
    async def test_remove_by_zone_id_or_entity_id_deletes_the_stored_zone(
        self, hass: HomeAssistant, office_client: HomeAssistantClient, zone_id: str
    ) -> None:
        result = await ZoneTools(office_client).ha_remove_zone(zone_id=zone_id)

        assert "office" not in _stored(hass)
        assert result["entity_id"] == "zone.office"

    async def test_a_storage_id_wins_over_a_renamed_entity_of_the_same_text(
        self,
        hass: HomeAssistant,
        office_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
    ) -> None:
        """Zone office was renamed to zone.work; zone work is then zone.work_2."""
        entity_registry.async_update_entity("zone.office", new_entity_id="zone.work")
        await _create(office_client, "Work")

        await ZoneTools(office_client).ha_remove_zone(zone_id="work")

        assert set(_stored(hass)) == {"office"}


class TestRemoveWait:
    @WAITS
    async def test_the_removal_is_waited_on_only_when_asked(
        self, office_client: HomeAssistantClient, wait: bool
    ) -> None:
        real = tools_zones.wait_for_entity_removed
        with patch.object(tools_zones, "wait_for_entity_removed", wraps=real) as waiter:
            result = await ZoneTools(office_client).ha_remove_zone(
                zone_id="office", wait=wait
            )

        assert "warnings" not in result
        if wait:
            waiter.assert_awaited_once_with(office_client, "zone.office")
        else:
            waiter.assert_not_awaited()

    async def test_a_zone_still_present_after_the_wait_is_a_warning(
        self, hass: HomeAssistant, office_client: HomeAssistantClient
    ) -> None:
        with (
            at_entity_wait(hass, lambda: hass.states.async_set("zone.office", "0")),
            patch.object(
                tools_zones,
                "wait_for_entity_removed",
                partial(tools_zones.wait_for_entity_removed, timeout=0.05),
            ),
        ):
            result = await ZoneTools(office_client).ha_remove_zone(zone_id="office")

        assert result["success"] is True
        assert "still present" in result["warnings"][0]

    async def test_a_removal_check_that_gets_no_answer_is_a_warning(
        self,
        hass: HomeAssistant,
        office_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        lose_state_reads(office_client, monkeypatch)

        result = await ZoneTools(office_client).ha_remove_zone(zone_id="office")

        assert "office" not in _stored(hass)
        assert "verification failed" in result["warnings"][0]

    @WAITS
    async def test_a_zone_without_an_entity_is_removed_without_a_guessed_one(
        self,
        hass: HomeAssistant,
        office_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
        wait: bool,
    ) -> None:
        """zone.office would be named and waited on, though no registry entry
        links it to the zone."""
        entity_registry.async_remove("zone.office")
        real = tools_zones.wait_for_entity_removed

        with patch.object(tools_zones, "wait_for_entity_removed", wraps=real) as waiter:
            result = await ZoneTools(office_client).ha_remove_zone(
                zone_id="office", wait=wait
            )

        assert "office" not in _stored(hass)
        assert result["entity_id"] is None
        (warning,) = result["warnings"]
        assert ("not waited on" in warning) is wait
        waiter.assert_not_awaited()

    @pytest.mark.parametrize("deleted", [True, False], ids=["removed", "failed"])
    async def test_a_zone_without_an_entity_is_named_by_its_storage_id(
        self,
        hass: HomeAssistant,
        office_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
        deleted: bool,
    ) -> None:
        """Neither the reply nor the error says "zone None"."""
        entity_registry.async_remove("zone.office")
        tools = ZoneTools(office_client)

        if deleted:
            message = (await tools.ha_remove_zone(zone_id="office"))["message"]
        else:
            with (
                _deleted_at_receipt(hass, "office"),
                pytest.raises(ToolError) as exc_info,
            ):
                await tools.ha_remove_zone(zone_id="office")
            await hass.async_block_till_done()
            message = error_of(exc_info)["message"]

        assert "office" in message and "None" not in message


class TestZoneWrites:
    async def test_update_by_entity_id_writes_the_stored_zone(
        self, hass: HomeAssistant, office_client: HomeAssistantClient
    ) -> None:
        result = await ZoneTools(office_client).ha_set_zone(
            zone_id="zone.office", radius=250
        )

        assert _stored(hass)["office"]["radius"] == 250
        assert result["zone_id"] == "office"
        assert result["entity_id"] == "zone.office"
        assert result["updated_fields"] == ["radius"]

    @WAITS
    async def test_the_update_is_waited_on_only_when_asked(
        self, office_client: HomeAssistantClient, wait: bool
    ) -> None:
        real = helper_update.wait_for_entity_registered
        with patch.object(
            helper_update, "wait_for_entity_registered", wraps=real
        ) as waiter:
            await ZoneTools(office_client).ha_set_zone(
                zone_id="office", radius=250, wait=wait
            )

        assert waiter.await_count == (1 if wait else 0)

    async def test_create_leaves_defaults_to_core(
        self, office_client: HomeAssistantClient
    ) -> None:
        """Core applies radius and passive defaults; the tool must not pin its own."""
        with sent_messages(office_client) as spy:
            await ZoneTools(office_client).ha_set_zone(
                name="School", latitude=52.5, longitude=13.4
            )

        (create,) = _sent(spy, "zone/create")
        assert "radius" not in create
        assert "passive" not in create

    @pytest.mark.parametrize("zone_id", [None, "office"])
    async def test_blank_name_is_refused(
        self, office_client: HomeAssistantClient, zone_id: str | None
    ) -> None:
        with (
            sent_messages(office_client) as spy,
            pytest.raises(ToolError) as exc_info,
        ):
            await ZoneTools(office_client).ha_set_zone(
                name="  ", latitude=52.5, longitude=13.4, zone_id=zone_id
            )

        assert "name cannot be blank" in error_of(exc_info)["message"]
        assert _sent(spy, "zone/create") == [] and _sent(spy, "zone/update") == []


class TestHomeZone:
    """The home zone's location and radius are Core's general settings,
    written with ``config/core/update`` as the Zones panel writes them."""

    @pytest.mark.parametrize("zone_id", ["home", "zone.home"])
    async def test_the_home_zone_moves_with_the_general_settings(
        self, hass: HomeAssistant, office_client: HomeAssistantClient, zone_id: str
    ) -> None:
        with sent_messages(office_client) as spy:
            result = await ZoneTools(office_client).ha_set_zone(
                zone_id=zone_id, latitude=48.1, longitude=11.6, radius=250
            )

        assert (hass.config.latitude, hass.config.longitude, hass.config.radius) == (
            48.1,
            11.6,
            250,
        )
        (update,) = _sent(spy, "config/core/update")
        assert set(update) == {"type", "latitude", "longitude", "radius"}
        assert _writes(spy) == []
        assert result["entity_id"] == "zone.home"
        assert result["zone_data"]["radius"] == 250
        assert "warnings" not in result

    async def test_only_the_given_fields_are_sent(
        self, hass: HomeAssistant, office_client: HomeAssistantClient
    ) -> None:
        latitude = hass.config.latitude
        with sent_messages(office_client) as spy:
            await ZoneTools(office_client).ha_set_zone(zone_id="zone.home", radius=80)

        (update,) = _sent(spy, "config/core/update")
        assert set(update) == {"type", "radius"}
        assert (hass.config.radius, hass.config.latitude) == (80, latitude)

    @pytest.mark.parametrize(
        "other", [{"name": "Castle"}, {"icon": "mdi:castle"}, {"passive": True}]
    )
    async def test_another_home_zone_field_is_refused_before_the_write(
        self,
        hass: HomeAssistant,
        office_client: HomeAssistantClient,
        other: dict[str, Any],
    ) -> None:
        radius = hass.config.radius
        with (
            sent_messages(office_client) as spy,
            pytest.raises(ToolError) as exc_info,
        ):
            await ZoneTools(office_client).ha_set_zone(
                zone_id="zone.home", radius=250, **other
            )

        assert error_of(exc_info)["code"] == "VALIDATION_FAILED"
        assert _sent(spy, "config/core/update") == []
        assert hass.config.radius == radius

    async def test_core_refuses_a_home_zone_value_it_does_not_take(
        self, office_client: HomeAssistantClient
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(office_client).ha_set_zone(
                zone_id="zone.home", latitude=123.0
            )

        assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"


class TestListing:
    async def test_without_component_lists_the_home_zone_and_entity_ids(
        self, hass: HomeAssistant, office_client: HomeAssistantClient
    ) -> None:
        """Core's zone/list serves stored zones only; the home zone is in the
        states, whose ``editable`` attribute says True."""
        result = await ZoneTools(office_client).ha_get_zone()

        by_id = {z["id"]: z for z in result["zones"]}
        assert by_id["office"]["entity_id"] == "zone.office"
        assert by_id["office"]["source"] == "storage"
        home = by_id["home"]
        assert (home["entity_id"], home["source"], home["editable"]) == (
            "zone.home",
            "yaml",
            False,
        )
        assert home["name"] == hass.config.location_name
        assert "warnings" not in result

    @pytest.mark.parametrize("path", ["core", "component"])
    async def test_a_stored_zone_named_home_wins_over_the_home_zone(
        self, hass: HomeAssistant, office_client: HomeAssistantClient, path: str
    ) -> None:
        """The home zone and a stored zone named Home both have the id "home"."""
        await _create(office_client, "Home")
        if path == "component":
            component_ws.async_register_commands(hass)

        result = await ZoneTools(office_client).ha_get_zone(zone_id="home")

        assert result["zone"]["entity_id"] == "zone.home_2"

    async def test_a_zone_can_be_fetched_by_its_entity_id(
        self, office_client: HomeAssistantClient
    ) -> None:
        result = await ZoneTools(office_client).ha_get_zone(zone_id="zone.office")

        assert result["zone"]["id"] == "office"

    async def test_without_registry_stored_zones_are_not_listed_twice(
        self, unregistered_client: HomeAssistantClient
    ) -> None:
        """No zone state can be matched to its stored zone without the
        registry; the listing names what it leaves out, and why."""
        result = await ZoneTools(unregistered_client).ha_get_zone()

        assert [z["id"] for z in result["zones"]] == ["office"]
        (warning,) = result["warnings"]
        assert "'home'" in warning
        assert "icons set in the entity registry" in warning
        assert UNKNOWN_COMMAND in warning

    async def test_without_states_the_home_zone_is_named_as_missing(
        self, office_client: HomeAssistantClient
    ) -> None:
        with patch.object(
            office_client,
            "get_states",
            AsyncMock(side_effect=HomeAssistantConnectionError("states down")),
        ):
            result = await ZoneTools(office_client).ha_get_zone()

        assert [z["id"] for z in result["zones"]] == ["office"]
        assert "'home'" in result["warnings"][0]

    async def test_a_zone_missing_from_an_incomplete_listing_is_not_absent(
        self, unregistered_client: HomeAssistantClient
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await ZoneTools(unregistered_client).ha_get_zone(zone_id="home")

        error = error_of(exc_info)
        assert error["code"] == "SERVICE_CALL_FAILED"
        assert UNKNOWN_COMMAND in error["message"]
