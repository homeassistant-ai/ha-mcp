"""Area-filtered entity search on real Core registries (issue #504).

``ha_search(area_filter=...)`` resolves areas from the area, floor, device and
entity registries instead of fuzzy-matching entity friendly names.

Bug: ha_search(area_filter="salon") returned entities from unrelated areas
because the old implementation fuzzy-matched entity friendly names instead of
consulting the registries.

The tests use ``core_client`` without the component, so the search runs on the
server's own registry resolution (``SmartSearchTools.get_entities_by_area``).
"""

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError

from ._search_tool import _build_ha_search


@pytest.fixture(autouse=True)
async def registry_commands(hass: HomeAssistant) -> None:
    """``config`` answers the registry list commands the search reads."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """The config entry the test devices belong to."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    return entry


def _add_entity(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entity_id: str,
    *,
    friendly_name: str,
    area_id: str | None = None,
    device_id: str | None = None,
) -> None:
    """Register ``entity_id`` (optionally on a device / in an area) with a state."""
    domain, object_id = entity_id.split(".")
    entry = entity_registry.async_get_or_create(
        domain,
        "test",
        object_id,
        suggested_object_id=object_id,
        device_id=device_id,
        # Core lets a device's entity hold an area of its own only when it
        # has a name of its own.
        original_name=friendly_name,
    )
    assert entry.entity_id == entity_id
    if area_id is not None:
        entity_registry.async_update_entity(entity_id, area_id=area_id)
    hass.states.async_set(entity_id, "on", {"friendly_name": friendly_name})


def _add_device(
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
    identifier: str,
    *,
    area_id: str | None = None,
) -> str:
    """Register a device, optionally placed in ``area_id``; return its id."""
    device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("test", identifier)}
    )
    if area_id is not None:
        device_registry.async_update_device(device.id, area_id=area_id)
    return device.id


async def _search(
    client: HomeAssistantClient, area_filter: str, **kwargs: Any
) -> dict[str, Any]:
    result: dict[str, Any] = await _build_ha_search(client)(
        area_filter=area_filter, **kwargs
    )
    return result


def _ids(result: dict[str, Any]) -> set[str]:
    return {row["entity_id"] for row in result["entities"]}


def _warnings(result: dict[str, Any]) -> list[str]:
    warnings: list[str] = result.get("warnings", [])
    return warnings


@pytest.fixture
def salon_and_abc(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    config_entry: MockConfigEntry,
) -> None:
    """The issue #504 home.

    ``light.salon_lamp`` is in Salon directly, ``climate.abc`` is in ABC
    through its thermostat device, and ``sensor.outdoor_temp`` has no area but
    a friendly name that mentions Salon, so a friendly-name match would return
    it for "salon".
    """
    salon = area_registry.async_create("Salon")
    abc = area_registry.async_create("ABC")
    _add_entity(
        hass,
        entity_registry,
        "light.salon_lamp",
        friendly_name="Salon Lamp",
        area_id=salon.id,
    )
    thermostat = _add_device(
        device_registry, config_entry, "thermostat", area_id=abc.id
    )
    _add_entity(
        hass,
        entity_registry,
        "climate.abc",
        friendly_name="Climatisation ABC",
        device_id=thermostat,
    )
    _add_entity(
        hass,
        entity_registry,
        "sensor.outdoor_temp",
        friendly_name="Salon Window Outdoor Temperature",
    )


@pytest.mark.usefixtures("salon_and_abc")
async def test_area_filter_returns_only_entities_placed_in_that_area(
    core_client: HomeAssistantClient,
) -> None:
    """Issue #504: "salon" must not return another area's entity, nor an
    unplaced entity whose friendly name merely mentions Salon."""
    result = await _search(core_client, "salon")

    assert result["area_names"] == ["Salon"]
    assert _ids(result) == {"light.salon_lamp"}


@pytest.mark.usefixtures("salon_and_abc")
async def test_entity_without_own_area_is_found_through_its_device_area(
    core_client: HomeAssistantClient,
) -> None:
    """An entity placed only through its device is listed under the device's area."""
    result = await _search(core_client, "abc")

    assert result["area_names"] == ["ABC"]
    assert _ids(result) == {"climate.abc"}


def _child_device_sensor(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    config_entry: MockConfigEntry,
    *,
    entity_area_id: str | None,
) -> None:
    """``sensor.child`` on a child device whose parent is in Office."""
    office = area_registry.async_create("Office")
    parent = _add_device(device_registry, config_entry, "parent", area_id=office.id)
    child = device_registry.async_get_or_create_child(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "child")},
        parent_device_id=parent,
    )
    _add_entity(
        hass,
        entity_registry,
        "sensor.child",
        friendly_name="Child Sensor",
        area_id=entity_area_id,
        device_id=child.id,
    )


async def test_entity_on_child_device_is_found_through_parent_device_area(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    config_entry: MockConfigEntry,
) -> None:
    """A child device with no area of its own sits in its parent's area, so
    its entities are found by that area."""
    _child_device_sensor(
        hass,
        area_registry,
        device_registry,
        entity_registry,
        config_entry,
        entity_area_id=None,
    )

    result = await _search(core_client, "office")

    assert _ids(result) == {"sensor.child"}


async def test_empty_entity_area_blocks_fallback_to_device_area(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    config_entry: MockConfigEntry,
) -> None:
    """An entity whose own area_id is present but empty (Core's registry and
    its update command both store ``""``) is not silently placed in its
    device's area."""
    _child_device_sensor(
        hass,
        area_registry,
        device_registry,
        entity_registry,
        config_entry,
        entity_area_id="",
    )

    result = await _search(core_client, "office")

    assert result["area_names"] == ["Office"]
    assert _ids(result) == set()


async def test_entity_own_area_overrides_its_device_area(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    config_entry: MockConfigEntry,
) -> None:
    """An entity moved to another room than its device is found in its own room
    only."""
    salon = area_registry.async_create("Salon")
    bedroom = area_registry.async_create("Bedroom")
    device = _add_device(device_registry, config_entry, "device_1", area_id=bedroom.id)
    _add_entity(
        hass,
        entity_registry,
        "sensor.device_temp",
        friendly_name="Device Temp",
        area_id=salon.id,
        device_id=device,
    )

    salon_result = await _search(core_client, "salon")
    bedroom_result = await _search(core_client, "bedroom")

    assert _ids(salon_result) == {"sensor.device_temp"}
    assert bedroom_result["area_names"] == ["Bedroom"]
    assert _ids(bedroom_result) == set()


@pytest.mark.parametrize(
    "area_query",
    [
        pytest.param("MASTER BEDROOM", id="name-in-other-case"),
        # The id ha_list_floors_areas reports.
        pytest.param("master_bedroom", id="area-id"),
        pytest.param("Chambre", id="area-alias"),
    ],
)
async def test_exact_area_reference_does_not_pull_in_similar_areas(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    area_query: str,
) -> None:
    """An area's exact name (in whatever case), id or alias selects that area
    alone, not every area whose name, id or alias contains it."""
    bedroom = area_registry.async_create("Master Bedroom", aliases={"Chambre"})
    closet = area_registry.async_create(
        "Master Bedroom Closet", aliases={"Chambre placard"}
    )
    assert bedroom.id == "master_bedroom"
    _add_entity(
        hass,
        entity_registry,
        "light.bedroom",
        friendly_name="Bedroom Light",
        area_id=bedroom.id,
    )
    _add_entity(
        hass,
        entity_registry,
        "light.closet",
        friendly_name="Closet Light",
        area_id=closet.id,
    )

    result = await _search(core_client, area_query)

    assert result["area_names"] == ["Master Bedroom"]
    assert _ids(result) == {"light.bedroom"}


async def test_unknown_area_returns_no_entities(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """An area that does not exist matches nothing rather than a nearby area."""
    for name in ("Living Room", "Kitchen"):
        area = area_registry.async_create(name)
        _add_entity(
            hass,
            entity_registry,
            f"light.{area.id}",
            friendly_name=f"{name} Light",
            area_id=area.id,
        )

    result = await _search(core_client, "garage")

    assert result["area_names"] == []
    assert _ids(result) == set()


@pytest.fixture
def basement(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    floor_registry: fr.FloorRegistry,
) -> None:
    """Floor Sous-sol (alias "Cave level") holds Couloir Sous-Sol and Cave.

    Garage on the Ground floor shows an expansion stays on its floor; Couloir
    Sous-Sol's name contains the floor's name, the shape that made a floor
    query resolve to that single area.
    """
    sous_sol = floor_registry.async_create("Sous-sol", aliases={"Cave level"})
    ground = floor_registry.async_create("Ground")
    for name, floor_id, entity_id in (
        ("Couloir Sous-Sol", sous_sol.floor_id, "light.basement_hall"),
        ("Cave", sous_sol.floor_id, "light.cave_lamp"),
        ("Garage", ground.floor_id, "light.garage_lamp"),
    ):
        area = area_registry.async_create(name, floor_id=floor_id)
        _add_entity(
            hass, entity_registry, entity_id, friendly_name=name, area_id=area.id
        )


_WHOLE_BASEMENT = {"light.basement_hall", "light.cave_lamp"}


@pytest.mark.usefixtures("basement")
@pytest.mark.parametrize("floor_query", ["sous-sol", "sous_sol"])
async def test_floor_name_or_id_expands_to_every_area_on_the_floor(
    core_client: HomeAssistantClient, floor_query: str
) -> None:
    """A floor's name or id lists every area on that floor, not just the one
    area whose name contains the floor's name, and is not reported as a guess."""
    result = await _search(core_client, floor_query)

    assert set(result["area_names"]) == {"Couloir Sous-Sol", "Cave"}
    assert _ids(result) == _WHOLE_BASEMENT
    assert any(
        "is a floor, not an area — expanded to 2 area(s)" in warning
        for warning in _warnings(result)
    )
    assert not any("close spelling" in w for w in _warnings(result))


@pytest.mark.usefixtures("basement")
async def test_floor_alias_shared_by_two_floors_expands_both(
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    floor_registry: fr.FloorRegistry,
    hass: HomeAssistant,
) -> None:
    """An exact floor alias expands like the floor's name, and when two floors
    share it, both are expanded and the warning names each floor."""
    lower = floor_registry.async_create("Lower level", aliases={"Cave level"})
    lower_room = area_registry.async_create("Lower room", floor_id=lower.floor_id)
    _add_entity(
        hass,
        entity_registry,
        "light.lower_lamp",
        friendly_name="Lower Lamp",
        area_id=lower_room.id,
    )

    result = await _search(core_client, "Cave level")

    assert _ids(result) == {*_WHOLE_BASEMENT, "light.lower_lamp"}
    warning = next(w for w in _warnings(result) if "expanded" in w)
    assert "Sous-sol" in warning
    assert "Lower level" in warning


@pytest.mark.usefixtures("basement")
async def test_area_alias_equal_to_floor_name_selects_the_area_with_warning(
    core_client: HomeAssistantClient, area_registry: ar.AreaRegistry
) -> None:
    """A name that is both an area alias and a floor name resolves to the area,
    and says the floor was passed over."""
    area_registry.async_update("couloir_sous_sol", aliases={"Sous-sol"})

    result = await _search(core_client, "Sous-sol")

    assert _ids(result) == {"light.basement_hall"}
    assert any("matches both an area and a floor" in w for w in _warnings(result))


@pytest.mark.usefixtures("basement")
async def test_area_id_equal_to_floor_id_selects_the_area_with_warning(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """An area whose id is also a floor's id wins over that floor."""
    stairs = area_registry.async_create("Sous sol")
    assert stairs.id == "sous_sol"  # the floor "Sous-sol" has this id too
    _add_entity(
        hass, entity_registry, "light.stairs", friendly_name="Stairs", area_id=stairs.id
    )

    result = await _search(core_client, "sous_sol")

    assert _ids(result) == {"light.stairs"}
    assert any("matches both an area and a floor" in w for w in _warnings(result))


@pytest.mark.usefixtures("basement")
async def test_misspelled_floor_expands_to_every_area_on_the_floor(
    core_client: HomeAssistantClient,
) -> None:
    """A close spelling of a whole floor name expands that floor instead of
    returning the one area whose name contains it."""
    result = await _search(core_client, "sous-sl")

    assert _ids(result) == _WHOLE_BASEMENT
    assert any("close spelling" in w for w in _warnings(result))


@pytest.mark.usefixtures("basement")
async def test_misspelled_area_name_stays_one_area_despite_containing_floor(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """A misspelling close to the floor's name but closer to an area's name
    selects that area, not the whole floor."""
    east = area_registry.async_create("Sous-sol Est", floor_id="sous_sol")
    _add_entity(
        hass,
        entity_registry,
        "light.cellar_east",
        friendly_name="Cellar East",
        area_id=east.id,
    )

    result = await _search(core_client, "Sous-sol Es")

    assert _ids(result) == {"light.cellar_east"}
    assert not any("is a floor" in w for w in _warnings(result))


async def test_misspelling_close_to_two_floors_expands_neither(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    floor_registry: fr.FloorRegistry,
) -> None:
    """A spelling equally close to two floors returns nothing and names both
    floors, instead of expanding the two of them."""
    for wing in ("East", "West"):
        floor = floor_registry.async_create(f"{wing} Wing")
        room = area_registry.async_create(f"{wing} room", floor_id=floor.floor_id)
        _add_entity(
            hass,
            entity_registry,
            f"light.{room.id}",
            friendly_name=f"{wing} Lamp",
            area_id=room.id,
        )

    result = await _search(core_client, "st Wing")

    assert result["area_names"] == []
    assert _ids(result) == set()
    warning = next(w for w in _warnings(result) if "ambiguously" in w)
    assert "East Wing" in warning
    assert "West Wing" in warning


async def test_floor_with_an_empty_name_is_named_by_its_id(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    floor_registry: fr.FloorRegistry,
) -> None:
    """Core lets a floor be renamed to an empty name, keeping its id; the
    expansion warning still says which floor was expanded, by that id."""
    floor = floor_registry.async_create("Attic")
    floor_registry.async_update(floor.floor_id, name="")
    room = area_registry.async_create("Attic room", floor_id=floor.floor_id)
    _add_entity(
        hass,
        entity_registry,
        "light.attic_lamp",
        friendly_name="Attic Lamp",
        area_id=room.id,
    )

    result = await _search(core_client, floor.floor_id)

    assert _ids(result) == {"light.attic_lamp"}
    warning = next(w for w in _warnings(result) if "expanded" in w)
    assert f"({floor.floor_id})" in warning


async def test_area_listing_is_grouped_by_domain_only_when_asked(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """``group_by_domain=True`` buckets an area's entities by domain; without it
    they stay one flat list."""
    salon = area_registry.async_create("Salon")
    salon_ids = {"light.salon_light", "switch.salon_switch", "sensor.salon_temp"}
    for entity_id in salon_ids:
        _add_entity(
            hass, entity_registry, entity_id, friendly_name=entity_id, area_id=salon.id
        )

    grouped = await _search(core_client, "salon", group_by_domain=True)
    flat = await _search(core_client, "salon", group_by_domain=False)

    assert {
        domain: {row["entity_id"] for row in rows}
        for domain, rows in grouped["by_domain"].items()
    } == {
        "light": {"light.salon_light"},
        "switch": {"switch.salon_switch"},
        "sensor": {"sensor.salon_temp"},
    }
    assert "by_domain" not in flat
    assert _ids(flat) == salon_ids


async def test_unreachable_registries_return_no_entities_and_say_why(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the WebSocket gone, an area search answers with no entities instead
    of failing, and warns that the registries were not read, so the empty
    answer is not taken for an empty area."""
    salon = area_registry.async_create("Salon")
    _add_entity(
        hass,
        entity_registry,
        "light.salon_lamp",
        friendly_name="Salon Lamp",
        area_id=salon.id,
    )
    monkeypatch.setattr(
        core_client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantConnectionError("WebSocket unavailable")),
    )

    result = await _search(core_client, "salon")

    assert _ids(result) == set()
    assert any("area registry" in w for w in _warnings(result))
