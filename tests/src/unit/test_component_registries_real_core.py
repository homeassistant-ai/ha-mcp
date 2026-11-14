"""Registry reads served by the component's ``registries`` command on a real ``hass``.

``ha_list_floors_areas`` and the auto-backup captures of a label, category,
area or floor read their rows through ``ha_mcp_tools/registries`` when the
component is there. Every field Core stores (icon, aliases, picture, labels,
the climate sensors, timestamps) has to survive that read: each row the tool
or capture returns is compared with the row Core's own
``config/<x>_registry/list`` answers.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from ha_mcp.backup_manager import get_backup_manager
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    get_websocket_client,
)
from ha_mcp.config import get_global_settings
from ha_mcp.tools.component_registries import WS_REGISTRIES
from ha_mcp.tools.tools_areas import AreaTools

from ._client_spies import sent_commands


@pytest.fixture
async def socket(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantWebSocketClient:
    """The pooled socket the component reads use, on a ``hass`` whose
    ``config`` integration answers Core's registry lists."""
    assert await async_setup_component(hass, "config", {})
    return await get_websocket_client(
        url=component_core_client.base_url,
        token=component_core_client.token,
        verify_ssl=component_core_client.verify_ssl,
    )


async def _core_rows(
    client: HomeAssistantClient, command: str, **params: Any
) -> list[dict[str, Any]]:
    reply = await client.send_websocket_message({"type": command, **params})
    assert reply["success"], reply
    rows: list[dict[str, Any]] = reply["result"]
    return rows


def _ground_floor_office_and_garage(hass: HomeAssistant) -> tuple[str, str, str]:
    """A Ground floor holding an Office with every optional field set, and a
    Garage on no floor; returns (floor_id, office_id, garage_id)."""
    hass.states.async_set("sensor.office_temp", "21", {"device_class": "temperature"})
    hass.states.async_set("sensor.office_humidity", "40", {"device_class": "humidity"})
    label = lr.async_get(hass).async_create("Important")
    floor = fr.async_get(hass).async_create(
        "Ground", level=0, icon="mdi:floor-plan", aliases={"downstairs"}
    )
    areas = ar.async_get(hass)
    office = areas.async_create(
        "Office",
        floor_id=floor.floor_id,
        aliases={"studio"},
        icon="mdi:office",
        picture="/local/office.png",
        labels={label.label_id},
        humidity_entity_id="sensor.office_humidity",
        temperature_entity_id="sensor.office_temp",
    )
    garage = areas.async_create("Garage")
    return floor.floor_id, office.id, garage.id


def _sent_types(spy: Any) -> list[str]:
    return [str(call.args[0]) for call in spy.call_args_list]


async def test_listed_floors_and_areas_keep_every_field_core_stores(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
) -> None:
    floor_id, office_id, garage_id = _ground_floor_office_and_garage(hass)
    areas = {
        row["area_id"]: row
        for row in await _core_rows(component_core_client, "config/area_registry/list")
    }
    (core_floor,) = await _core_rows(
        component_core_client, "config/floor_registry/list"
    )

    with sent_commands(socket) as sent:
        listed = await AreaTools(component_core_client).ha_list_floors_areas()

    assert WS_REGISTRIES in _sent_types(sent)
    assert "config/area_registry/list" not in _sent_types(sent)
    (floor,) = listed["floors"]
    floor_areas = floor.pop("areas")
    assert floor_areas == [areas[office_id]]
    assert floor == core_floor
    assert floor["floor_id"] == floor_id
    assert listed["unassigned_areas"] == [areas[garage_id]]
    assert (
        listed["area_count"],
        listed["unassigned_count"],
        listed["orphaned_count"],
    ) == (2, 1, 0)


async def _captured(
    client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
    domain: str,
    target: str,
) -> tuple[Any, list[str]]:
    """What the auto-backup captures for ``target``, and the commands it sent."""
    handler = get_backup_manager(client, get_global_settings()).handler_for(domain)
    assert handler is not None
    with sent_commands(socket) as sent:
        snapshot = await handler.fetch(client, target)
    return snapshot, _sent_types(sent)


async def test_a_label_capture_keeps_every_field_core_stores(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
) -> None:
    label = lr.async_get(hass).async_create(
        "Favorites", color="red", description="fav", icon="mdi:star"
    )
    rows = await _core_rows(component_core_client, "config/label_registry/list")

    snapshot, sent = await _captured(
        component_core_client, socket, "label", label.label_id
    )

    assert WS_REGISTRIES in sent
    assert snapshot == next(r for r in rows if r["label_id"] == label.label_id)


async def test_a_category_capture_keeps_every_field_core_stores(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
) -> None:
    category = cr.async_get(hass).async_create(
        name="Lights", scope="automation", icon="mdi:lightbulb"
    )
    (row,) = await _core_rows(
        component_core_client, "config/category_registry/list", scope="automation"
    )

    snapshot, sent = await _captured(
        component_core_client,
        socket,
        "category",
        f"automation:{category.category_id}",
    )

    assert WS_REGISTRIES in sent
    assert snapshot == {"scope": "automation", **row}


@pytest.mark.parametrize(
    ("kind", "list_command", "id_key"),
    [
        ("area", "config/area_registry/list", "area_id"),
        ("floor", "config/floor_registry/list", "floor_id"),
    ],
)
async def test_an_area_or_floor_capture_keeps_every_field_core_stores(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
    kind: str,
    list_command: str,
    id_key: str,
) -> None:
    floor_id, office_id, _ = _ground_floor_office_and_garage(hass)
    wanted = office_id if kind == "area" else floor_id
    rows = await _core_rows(component_core_client, list_command)

    snapshot, sent = await _captured(
        component_core_client, socket, "area_or_floor", f"{kind}:{wanted}"
    )

    assert WS_REGISTRIES in sent
    assert snapshot == {
        "kind": kind,
        **next(r for r in rows if r[id_key] == wanted),
    }
