"""``ha_mcp_tools/device_get`` and ``device_list`` on Core's device registry.

Devices are Core's own, main and child; the commands run through Core's
WebSocket API (``component_ws``). A registry Core cannot be made to produce (one
that fails, holds one id twice or has the 2026.8 shape) has one Core accessor
replaced for the duration of the command. ``test_component_device_seam.py``
covers the rows the tools read from these commands.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.json import json_dumps
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools.websocket_api.constants import (
    WS_DEVICE_GET,
    WS_DEVICE_LIST,
    WS_ENTITY_ENRICH,
)

from ._component_core import ws_result
from ._core_setup import add_test_entry


def _as_sent(value: Any) -> Any:
    """``value`` as Core's JSON encoder puts it on the wire."""
    return json.loads(json_dumps(value))


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    return add_test_entry(hass)


def _device(
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
    key: str,
    *,
    area_id: str | None = None,
    **kwargs: Any,
) -> dr.DeviceEntry:
    """Register device ``key``, placed in ``area_id`` when one is given."""
    device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("test", key)}, **kwargs
    )
    if area_id is None:
        return device
    placed = device_registry.async_update_device(device.id, area_id=area_id)
    assert placed is not None
    return placed


def _conflict(
    device_registry: dr.DeviceRegistry, device: dr.DeviceEntry, area_id: str
) -> Any:
    """Make the registry hand out ``device`` twice, the copies disagreeing on
    the area: Core's own entry before and after a move to ``area_id``."""
    moved = device_registry.async_update_device(device.id, area_id=area_id)
    return patch.object(
        dr.ActiveDeviceRegistryItems, "values", return_value=[device, moved]
    )


async def _get(ws: MockHAClientWebSocket, device_id: str, **params: Any) -> Any:
    return await ws_result(ws, WS_DEVICE_GET, device_id=device_id, **params)


async def _list(ws: MockHAClientWebSocket) -> Any:
    return (await ws_result(ws, WS_DEVICE_LIST))["devices"]


@pytest.fixture
def areas(area_registry: ar.AreaRegistry) -> dict[str, str]:
    return {
        name: area_registry.async_create(name.title()).id
        for name in ("office", "garage")
    }


async def test_an_unknown_device_is_none(
    component_ws: MockHAClientWebSocket,
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
) -> None:
    _device(device_registry, config_entry, "known")

    assert await _get(component_ws, "ghost") == {"device": None}


async def test_an_empty_device_id_is_none(
    component_ws: MockHAClientWebSocket,
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
) -> None:
    _device(device_registry, config_entry, "known")

    assert await _get(component_ws, "") == {"device": None}


def _unserializable(*devices: dr.DeviceEntry, bad: str) -> Any:
    """Make Core's ``dict_repr`` of device ``bad`` fail, as a drifted Core would."""
    rows = {device.id: device.dict_repr for device in devices}

    def dict_repr(device: dr.DeviceEntry) -> dict[str, Any]:
        if device.id == bad:
            raise RuntimeError("core drift")
        return rows[device.id]

    return patch.object(dr.DeviceEntry, "dict_repr", property(dict_repr))


async def test_a_device_core_cannot_serialize_is_none(
    component_ws: MockHAClientWebSocket,
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
) -> None:
    device = _device(device_registry, config_entry, "x")

    with _unserializable(device, bad=device.id):
        result = await _get(component_ws, device.id)

    assert result == {"device": None}


async def test_a_list_skips_and_logs_a_device_core_cannot_serialize(
    component_ws: MockHAClientWebSocket,
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The skip names the device instead of passing silently (#1813 F5)."""
    bad = _device(device_registry, config_entry, "bad")
    good = _device(device_registry, config_entry, "good", name="Good")

    with _unserializable(bad, good, bad=bad.id), caplog.at_level(logging.WARNING):
        devices = await _list(component_ws)

    assert [row["id"] for row in devices] == [good.id]
    assert any(
        "skipping device" in r.getMessage() and bad.id in r.getMessage()
        for r in caplog.records
    )


async def test_list_sends_cores_device_rows(
    component_ws: MockHAClientWebSocket,
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
    areas: dict[str, str],
) -> None:
    one = _device(device_registry, config_entry, "d1", name="One")
    two = _device(
        device_registry, config_entry, "d2", name="Two", area_id=areas["garage"]
    )

    devices = await _list(component_ws)

    assert sorted(devices, key=lambda row: row["name"]) == [
        _as_sent(one.dict_repr),
        _as_sent(two.dict_repr),
    ]


async def test_an_empty_registry_lists_no_devices(
    component_ws: MockHAClientWebSocket,
) -> None:
    assert await _list(component_ws) == []


async def test_main_and_child_devices_list_in_a_stable_order(
    component_ws: MockHAClientWebSocket,
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
    areas: dict[str, str],
) -> None:
    """Core 2026.9 keeps child devices in a collection of their own."""
    parent = _device(device_registry, config_entry, "parent", area_id=areas["office"])
    ordinary = _device(
        device_registry, config_entry, "ordinary", area_id=areas["garage"]
    )
    child = device_registry.async_get_or_create_child(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "child")},
        parent_device_id=parent.id,
    )

    first = await _list(component_ws)
    second = await _list(component_ws)

    assert [row["id"] for row in first] == [parent.id, ordinary.id, child.id]
    assert second == first
    assert first[-1] == _as_sent(child.dict_repr)


async def test_a_device_held_twice_is_left_out_of_the_list(
    component_ws: MockHAClientWebSocket,
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
    areas: dict[str, str],
) -> None:
    """Neither copy is chosen arbitrarily."""
    device = _device(device_registry, config_entry, "dup", area_id=areas["office"])

    with _conflict(device_registry, device, areas["garage"]):
        devices = await _list(component_ws)

    assert devices == []


async def test_a_device_held_twice_is_not_returned_by_id(
    component_ws: MockHAClientWebSocket,
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
    areas: dict[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    device = _device(device_registry, config_entry, "dup", area_id=areas["office"])

    with (
        _conflict(device_registry, device, areas["garage"]),
        caplog.at_level(logging.WARNING),
    ):
        result = await _get(component_ws, device.id)

    assert result == {"device": None}
    assert any("conflicting device identity" in r.message for r in caplog.records)


async def test_a_device_held_twice_gives_its_entities_no_area(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    config_entry: MockConfigEntry,
    areas: dict[str, str],
) -> None:
    device = _device(device_registry, config_entry, "dup", area_id=areas["office"])
    entity_registry.async_get_or_create(
        "sensor",
        "test",
        "ambiguous",
        suggested_object_id="ambiguous",
        device_id=device.id,
    )

    with _conflict(device_registry, device, areas["garage"]):
        result = await ws_result(
            component_ws, WS_ENTITY_ENRICH, entity_ids=["sensor.ambiguous"]
        )

    assert result["entities"]["sensor.ambiguous"]["area"] is None


async def test_a_parent_held_twice_gives_its_child_no_area(
    component_ws: MockHAClientWebSocket,
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
    areas: dict[str, str],
) -> None:
    parent = _device(device_registry, config_entry, "parent", area_id=areas["office"])
    child = device_registry.async_get_or_create_child(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "child")},
        parent_device_id=parent.id,
    )

    with _conflict(device_registry, parent, areas["garage"]):
        result = await _get(component_ws, child.id)

    assert result["device"]["id"] == child.id
    assert result["effective_area_id"] is None


async def test_a_childs_empty_area_is_not_replaced_by_its_parents(
    component_ws: MockHAClientWebSocket,
    device_registry: dr.DeviceRegistry,
    config_entry: MockConfigEntry,
    areas: dict[str, str],
) -> None:
    """An empty direct area is invalid placement, not "no area of its own"."""
    parent = _device(device_registry, config_entry, "parent", area_id=areas["office"])
    child = device_registry.async_get_or_create_child(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "child")},
        parent_device_id=parent.id,
    )
    device_registry.async_update_child_device(child.id, area_id="")

    result = await _get(component_ws, child.id)

    assert result["effective_area_id"] is None


async def test_device_entities_are_sent_only_when_asked_for(
    component_ws: MockHAClientWebSocket,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    config_entry: MockConfigEntry,
) -> None:
    device = _device(device_registry, config_entry, "d1")
    entity_registry.async_get_or_create("sensor", "test", "a", device_id=device.id)

    result = await _get(component_ws, device.id)

    assert result["device"]["id"] == device.id
    assert "entities" not in result


async def test_an_unknown_device_has_no_entities(
    component_ws: MockHAClientWebSocket,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    config_entry: MockConfigEntry,
) -> None:
    device = _device(device_registry, config_entry, "d1")
    entity_registry.async_get_or_create("sensor", "test", "a", device_id=device.id)

    result = await _get(component_ws, "ghost", include_entities=True)

    assert result == {"device": None, "entities": []}
