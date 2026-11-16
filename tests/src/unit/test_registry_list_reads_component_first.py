"""Which whole registry a read takes from the component, and which from Core.

A read of the area, floor, label or category registry asks the component
first: with a component that serves the registry, Core's own list command for
it is not sent. The device registry is read the same way for the names a
search adds to its results (``result_fields``). A read whose rows go to the
visibility resolver or the bulk selector takes Core's own list:
``core_device_list_reply`` says why.

Each test runs a tool, a search or a helper a tool calls on the server's own
client against a real ``hass`` with the component. The routing tests read on
Core's side which commands arrived; the others hold a device in two copies
that disagree.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.bulk_selector import (
    BulkSelectorInfrastructureError,
    BulkSelectorResolution,
    resolve_bulk_selector,
)
from ha_mcp.tools.component_devices import WS_DEVICE_LIST
from ha_mcp.tools.component_registries import WS_REGISTRIES
from ha_mcp.tools.config_write_helpers import apply_entity_category
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_categories import CategoryTools
from ha_mcp.tools.tools_search import SearchTools
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig

from ._command_faults import received
from ._component_frames import older_component
from ._search_tool import _visibility

_AREAS = "config/area_registry/list"
_FLOORS = "config/floor_registry/list"
_LABELS = "config/label_registry/list"
_DEVICES = "config/device_registry/list"
_CATEGORIES = "config/category_registry/list"


@pytest.fixture
async def client(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
    label_registry: lr.LabelRegistry,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> HomeAssistantClient:
    """Core with the component and a lamp: ``light.lamp`` on a device in the
    Kitchen, which is on the Ground floor, the device labelled Bright."""
    assert await async_setup_component(hass, "config", {})
    ground = floor_registry.async_create("Ground")
    kitchen = area_registry.async_create("Kitchen", floor_id=ground.floor_id)
    bright = label_registry.async_create("Bright")
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "lamp")}, name="Lamp"
    )
    device_registry.async_update_device(
        device.id, area_id=kitchen.id, labels={bright.label_id}
    )
    light = entity_registry.async_get_or_create(
        "light", "test", "lamp", suggested_object_id="lamp", device_id=device.id
    )
    hass.states.async_set(light.entity_id, "on", {"friendly_name": "Lamp"})
    return component_core_client


@pytest.fixture
def unfiltered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _visibility(tmp_path, monkeypatch, VisibilityConfig(enabled=False))


@pytest.fixture
def by_area(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A visibility rule on an area, which the device registry is read for."""
    _visibility(
        tmp_path, monkeypatch, VisibilityConfig(enabled=True, exclude_areas=["garage"])
    )


@contextmanager
def _reads(hass: HomeAssistant) -> Iterator[dict[str, list[Any]]]:
    """The registry list reads Core receives meanwhile, its own and the component's."""
    with received(
        hass, _AREAS, _FLOORS, _LABELS, _DEVICES, WS_REGISTRIES, WS_DEVICE_LIST
    ) as seen:
        yield seen


def _asked(seen: dict[str, list[Any]]) -> list[str]:
    """The registries the component was asked for, sorted."""
    asked = [kind for msg in seen[WS_REGISTRIES] for kind in msg["registries"]]
    asked.extend("device" for _ in seen[WS_DEVICE_LIST])
    return sorted(asked)


def _core_lists(seen: dict[str, list[Any]]) -> list[str]:
    """Core's own list commands that arrived."""
    return [
        command
        for command in (_AREAS, _FLOORS, _LABELS, _DEVICES)
        for _ in seen[command]
    ]


async def _select_ground_lights(client: HomeAssistantClient) -> BulkSelectorResolution:
    """``ha_bulk_control``'s selector for the lights on the Ground floor."""
    return await resolve_bulk_selector(
        client,
        {"domain": "light", "floor_ids": ["ground"]},
        action="off",
        parameters=None,
        timeout_seconds=None,
        validate_first=False,
    )


async def test_a_bulk_selector_reads_areas_and_floors_from_the_component(
    hass: HomeAssistant, client: HomeAssistantClient, unfiltered: None
) -> None:
    with _reads(hass) as seen:
        resolution = await _select_ground_lights(client)

    assert resolution.resolved_entity_ids == ("light.lamp",)
    assert resolution.selected_area_ids == ("kitchen",)
    assert not (seen[_AREAS] or seen[_FLOORS])
    assert _asked(seen) == ["area", "floor"]


async def test_an_area_search_reads_areas_and_floors_from_the_component(
    hass: HomeAssistant, client: HomeAssistantClient, unfiltered: None
) -> None:
    with _reads(hass) as seen:
        found = await SmartSearchTools(client=client).get_entities_by_area("Ground")

    assert list(found["areas"]) == ["kitchen"]
    assert not (seen[_AREAS] or seen[_FLOORS])
    assert _asked(seen) == ["area", "floor"]


@pytest.fixture
async def without_overview(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A component from before its ``overview`` command: ``ha_get_overview``
    reads the states and registries itself, next to the notifications and
    repairs it reports."""
    older_component(monkeypatch, "overview")
    for domain in ("persistent_notification", "repairs"):
        assert await async_setup_component(hass, domain, {})


async def test_the_overview_of_a_component_without_its_overview_command_reads_areas_from_it(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    unfiltered: None,
    without_overview: None,
) -> None:
    tools = SearchTools(client, SmartSearchTools(client=client))

    with _reads(hass) as seen:
        overview = await tools.ha_get_overview()

    assert overview["success"] is True
    assert overview["area_analysis"] == {"kitchen": {"count": 1}}
    assert not seen[_AREAS]
    assert _asked(seen) == ["area"]


@pytest.fixture
def without_search(monkeypatch: pytest.MonkeyPatch) -> None:
    """A component from before its ``search`` command: ``ha_search`` reads the
    states and registries itself."""
    older_component(monkeypatch, "search")


@pytest.mark.parametrize(
    ("device_list", "core_lists", "asked"),
    [
        pytest.param(
            True, [], ["area", "device", "floor", "label"], id="all_from_component"
        ),
        pytest.param(
            False,
            [_DEVICES],
            ["area", "floor", "label"],
            id="devices_from_core_without_device_list",
        ),
    ],
)
async def test_a_search_for_names_reads_the_registries_from_the_component(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    unfiltered: None,
    without_search: None,
    monkeypatch: pytest.MonkeyPatch,
    device_list: bool,
    core_lists: list[str],
    asked: list[str],
) -> None:
    """The names come from the component; devices from Core only when the
    component does not offer its device list."""
    if not device_list:
        older_component(monkeypatch, "device_list")
    tools = SearchTools(client, SmartSearchTools(client=client))

    with _reads(hass) as seen:
        found = await tools.ha_search(
            query="lamp", result_fields=["area", "floor", "labels"]
        )

    (row,) = found["entities"]
    assert (row["area"], row["floor"], row["labels"]) == (
        "Kitchen",
        "Ground",
        ["Bright"],
    )
    assert _core_lists(seen) == core_lists
    assert _asked(seen) == asked


@pytest.fixture
def held_twice(
    client: HomeAssistantClient, device_registry: dr.DeviceRegistry
) -> Iterator[None]:
    """The registry hands out the lamp's device twice, the copies disagreeing
    on its area: Core's own entry before and after it left the Kitchen."""
    (before,) = device_registry.devices
    after = device_registry.async_update_device(before.id, area_id=None)
    with patch.object(
        dr.ActiveDeviceRegistryItems, "values", return_value=[before, after]
    ):
        yield


async def test_a_bulk_selector_refuses_a_device_held_twice(
    client: HomeAssistantClient, unfiltered: None, held_twice: None
) -> None:
    with pytest.raises(
        BulkSelectorInfrastructureError, match="conflicting device identity"
    ):
        await _select_ground_lights(client)


async def test_an_area_search_reports_a_device_held_twice(
    client: HomeAssistantClient, by_area: None, held_twice: None
) -> None:
    found = await SmartSearchTools(client=client).get_entities_by_area("Kitchen")

    assert found["warnings"] == [resolver.DEVICE_REGISTRY_CONFLICT_WARNING]


async def test_a_fuzzy_search_reports_a_device_held_twice(
    client: HomeAssistantClient, by_area: None, held_twice: None
) -> None:
    found = await SmartSearchTools(client=client).smart_entity_search("lamp")

    assert found["warnings"] == [resolver.DEVICE_REGISTRY_CONFLICT_WARNING]


async def test_the_overview_reports_a_device_held_twice(
    client: HomeAssistantClient,
    by_area: None,
    without_overview: None,
    held_twice: None,
) -> None:
    tools = SearchTools(client, SmartSearchTools(client=client))

    overview = await tools.ha_get_overview()

    assert overview["warnings"] == [resolver.DEVICE_REGISTRY_CONFLICT_WARNING]


async def test_an_exact_search_reports_a_device_held_twice(
    client: HomeAssistantClient,
    by_area: None,
    without_search: None,
    held_twice: None,
) -> None:
    tools = SearchTools(client, SmartSearchTools(client=client))

    found = await tools.ha_search(query="lamp", exact_match=True)

    assert resolver.DEVICE_REGISTRY_CONFLICT_WARNING in found["warnings"]


async def test_a_domain_listing_reports_a_device_held_twice(
    client: HomeAssistantClient,
    by_area: None,
    without_search: None,
    held_twice: None,
) -> None:
    tools = SearchTools(client, SmartSearchTools(client=client))

    found = await tools.ha_search(domain_filter="light")

    assert resolver.DEVICE_REGISTRY_CONFLICT_WARNING in found["warnings"]


@pytest.fixture
def lighting(category_registry: cr.CategoryRegistry) -> cr.CategoryEntry:
    return category_registry.async_create(scope="automation", name="Lighting")


async def test_categories_are_read_from_the_component(
    hass: HomeAssistant, client: HomeAssistantClient, lighting: cr.CategoryEntry
) -> None:
    with received(hass, _CATEGORIES, WS_REGISTRIES) as seen:
        listed = await CategoryTools(client).ha_config_get_category("automation")

    assert [row["category_id"] for row in listed["categories"]] == [
        lighting.category_id
    ]
    assert seen[_CATEGORIES] == []
    assert [msg["category_scopes"] for msg in seen[WS_REGISTRIES]] == [["automation"]]


async def test_the_category_check_before_an_assignment_asks_the_component(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    lighting: cr.CategoryEntry,
) -> None:
    result: dict[str, Any] = {}

    with received(hass, _CATEGORIES, WS_REGISTRIES) as seen:
        await apply_entity_category(
            client, "light.lamp", lighting.category_id, "automation", result
        )

    assert result == {"category": lighting.category_id}
    entry = entity_registry.async_get("light.lamp")
    assert entry is not None
    assert entry.categories == {"automation": lighting.category_id}
    assert seen[_CATEGORIES] == []
    assert [msg["category_scopes"] for msg in seen[WS_REGISTRIES]] == [["automation"]]
