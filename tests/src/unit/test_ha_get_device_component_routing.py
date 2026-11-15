"""Routing of ``ha_get_device`` over the ``ha_mcp_tools`` component.

A single lookup used to read the whole device registry and the whole entity
registry. When the component advertises ``device_get`` / ``device_list`` (with
child-device semantics), a lookup reads one ``device_get(include_entities)``
frame, an ``entity_id`` lookup first asks Core's ``config/entity_registry/get``
for that one entity, and a list read is one ``device_list`` frame. Every miss of
the component (no capability, a command it no longer knows, a timeout, a socket
that cannot be opened, an older build without the entities half) falls back to
the legacy registry lists for the same result.

The server's own client runs on a real ``hass`` whose registries hold the
devices; Core answers the component's commands and the legacy reads. A spy on
the pooled socket records which of them the tool sent.
"""

from __future__ import annotations

from typing import Any, NamedTuple, cast
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools.websocket_api import system
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import component_api, component_devices
from ha_mcp.tools.tools_registry import RegistryTools
from ha_mcp.utils.device_registry_semantics import EFFECTIVE_AREA_MARKER

from ._client_spies import sent_commands
from ._routing_core import (
    held_command,
    invalidations,
    pooled_socket,
    sent_frames,
    sent_types,
)
from ._tool_errors import error_of

DEVICE_GET = "ha_mcp_tools/device_get"
DEVICE_LIST = "ha_mcp_tools/device_list"
DEVICE_DUMP = "config/device_registry/list"
ENTITY_DUMP = "config/entity_registry/list"
ENTITY_GET = "config/entity_registry/get"


class Device(NamedTuple):
    device_id: str
    entity_id: str


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """A non-radio integration, so no zha / zwave / matter enrichment runs."""
    config_entry = MockConfigEntry(domain="hue")
    config_entry.add_to_hass(hass)
    return config_entry


def _device(
    hass: HomeAssistant, entry: MockConfigEntry, key: str, **update: Any
) -> Device:
    """A device of ``entry`` with one sensor entity."""
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("hue", key)},
        name=f"Device {key}",
        manufacturer="Acme",
        model="M1",
    )
    if update:
        devices.async_update_device(device.id, **update)
    sensor = er.async_get(hass).async_get_or_create(
        "sensor", "hue", f"{key}-sensor", config_entry=entry, device_id=device.id
    )
    return Device(device.id, sensor.entity_id)


@pytest.fixture
async def registries(hass: HomeAssistant, core_client: HomeAssistantClient) -> None:
    """Core's registry API (the legacy reads)."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
async def client(
    registries: None, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    return component_core_client


async def _get(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    return cast("dict[str, Any]", await RegistryTools(client).ha_get_device(**kwargs))


async def test_a_single_lookup_is_one_device_get_frame_and_no_registry_dump(
    hass: HomeAssistant, client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    kitchen = _device(hass, entry, "dev-1", name_by_user="Kitchen")
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent:
        resp = await _get(client, device_id=kitchen.device_id)

    assert resp["success"] is True
    assert resp["device"]["id"] == kitchen.device_id
    # Core's row passes through: the user's name next to the integration's.
    assert resp["device"]["name_by_user"] == "Kitchen"
    assert resp["device"]["name"] == "Device dev-1"
    assert resp["entity_count"] == 1
    assert resp["device"]["entities"][0]["entity_id"] == kitchen.entity_id
    types = sent_types(sent)
    assert DEVICE_DUMP not in types
    assert ENTITY_DUMP not in types
    (frame,) = sent_frames(sent, DEVICE_GET)
    assert frame == {"device_id": kitchen.device_id, "include_entities": True}


async def test_a_child_device_keeps_its_parents_area_as_its_effective_area(
    hass: HomeAssistant, client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    office = ar.async_get(hass).async_create("Office")
    parent = _device(hass, entry, "parent-1", area_id=office.id)
    devices = dr.async_get(hass)
    child = devices.async_get_or_create_child(
        config_entry_id=entry.entry_id,
        identifiers={("hue", "child-1")},
        parent_device_id=parent.device_id,
    )
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent:
        resp = await _get(client, device_id=child.id)

    device = resp["device"]
    assert device["effective_area_id"] == office.id
    assert device["area_id"] is None
    assert device["parent_device_id"] == parent.device_id
    assert EFFECTIVE_AREA_MARKER not in device
    assert DEVICE_DUMP not in sent_types(sent)


async def test_an_entity_lookup_reads_that_entity_then_one_device_get(
    hass: HomeAssistant, client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    lamp = _device(hass, entry, "dev-1")
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent:
        resp = await _get(client, entity_id=lamp.entity_id)

    assert resp["success"] is True
    assert resp["device"]["id"] == lamp.device_id
    assert resp["queried_by"] == "entity_id"
    types = sent_types(sent)
    assert types.count(ENTITY_GET) == 1
    assert types.count(DEVICE_GET) == 1
    assert DEVICE_DUMP not in types
    assert ENTITY_DUMP not in types


async def test_a_device_the_component_does_not_have_is_not_found(
    hass: HomeAssistant, client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    """The not-found error lists valid ids, read from the legacy device list."""
    other = _device(hass, entry, "other")
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent, pytest.raises(ToolError) as exc:
        await _get(client, device_id="ghost")

    error = error_of(exc)
    assert error["code"] == "RESOURCE_NOT_FOUND"
    assert other.device_id in str(exc.value)
    assert sent_types(sent).count(DEVICE_DUMP) == 1


async def test_a_summary_list_is_one_device_list_frame_without_entities(
    hass: HomeAssistant, client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    _device(hass, entry, "d1")
    _device(hass, entry, "d2")
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent:
        resp = await _get(client)

    assert resp["success"] is True
    assert resp["total_devices"] == 2
    types = sent_types(sent)
    assert types.count(DEVICE_LIST) == 1
    assert DEVICE_DUMP not in types
    assert ENTITY_DUMP not in types


async def test_a_component_without_child_semantics_is_not_trusted_for_a_list(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older component advertises ``device_list`` but may leave out child
    devices, so the server reads Core's device list until the component
    advertises the child-device capability."""
    monkeypatch.setattr(
        system,
        "CAPABILITIES",
        [
            cap
            for cap in system.CAPABILITIES
            if cap != "device_registry_child_semantics"
        ],
    )
    parent = _device(hass, entry, "parent-1")
    child = dr.async_get(hass).async_get_or_create_child(
        config_entry_id=entry.entry_id,
        identifiers={("hue", "child-1")},
        parent_device_id=parent.device_id,
    )
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent:
        resp = await _get(client)

    assert {row["id"] for row in resp["devices"]} == {parent.device_id, child.id}
    types = sent_types(sent)
    assert types.count(DEVICE_DUMP) == 1
    assert DEVICE_LIST not in types


async def test_the_area_filter_matches_a_child_through_its_parents_area(
    hass: HomeAssistant,
    registries: None,
    core_client: HomeAssistantClient,
    entry: MockConfigEntry,
) -> None:
    areas = ar.async_get(hass)
    office, garage = areas.async_create("Office"), areas.async_create("Garage")
    parent = _device(hass, entry, "parent-1", area_id=office.id)
    child = dr.async_get(hass).async_get_or_create_child(
        config_entry_id=entry.entry_id,
        identifiers={("hue", "child-1")},
        parent_device_id=parent.device_id,
    )
    _device(hass, entry, "other", area_id=garage.id)

    resp = await _get(core_client, area_id=office.id)

    assert {row["id"] for row in resp["devices"]} == {parent.device_id, child.id}


async def test_without_the_component_the_legacy_registries_serve_a_lookup(
    hass: HomeAssistant,
    registries: None,
    core_client: HomeAssistantClient,
    entry: MockConfigEntry,
) -> None:
    sensor = _device(hass, entry, "dev-1")
    socket = await pooled_socket(core_client)

    with sent_commands(socket) as sent:
        resp = await _get(core_client, device_id=sensor.device_id)

    assert resp["device"]["id"] == sensor.device_id
    types = sent_types(sent)
    assert types.count(DEVICE_DUMP) == 1
    assert DEVICE_GET not in types


# --- the entity_id -> device_id read ------------------------------------------------


async def test_an_entity_core_does_not_know_is_not_found_without_a_dump(
    client: HomeAssistantClient,
) -> None:
    """Core's ``not_found`` answer is authoritative (#1813 F3)."""
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent, pytest.raises(ToolError) as exc:
        await _get(client, entity_id="sensor.ghost")

    assert error_of(exc)["code"] == "ENTITY_NOT_FOUND"
    assert "sensor.ghost" in str(exc.value)
    assert ENTITY_DUMP not in sent_types(sent)


async def test_a_refused_entity_read_falls_back_to_the_registry_list(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refusal other than ``not_found`` is no verdict on the entity: the
    whole registry is read so a real entity is not reported missing (#1813 F3)."""
    lamp = _device(hass, entry, "dev-1")
    monkeypatch.delitem(hass.data["websocket_api"], ENTITY_GET)
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent:
        resp = await _get(client, entity_id=lamp.entity_id)

    assert resp["device"]["id"] == lamp.device_id
    assert sent_types(sent).count(ENTITY_DUMP) == 1


async def test_a_refused_entity_read_and_registry_list_is_a_service_error(
    core_client: HomeAssistantClient,
) -> None:
    """Without Core's registry API both reads are refused; a real entity must
    not be reported as ENTITY_NOT_FOUND (Codex P2)."""
    with pytest.raises(ToolError) as exc:
        await _get(core_client, entity_id="light.lr")

    assert error_of(exc)["code"] == "SERVICE_CALL_FAILED"


# --- component failures --------------------------------------------------------------


async def test_a_component_without_device_get_is_invalidated_and_falls_back(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensor = _device(hass, entry, "dev-1")
    assert await component_api.get_component_caps(client) is not None
    monkeypatch.delitem(hass.data["websocket_api"], DEVICE_GET)
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent, invalidations(component_devices) as invalidate:
        resp = await _get(client, device_id=sensor.device_id)

    assert resp["device"]["id"] == sensor.device_id
    assert sent_types(sent).count(DEVICE_DUMP) == 1
    invalidate.assert_called_once_with(client)


async def test_a_timed_out_device_get_falls_back_and_keeps_the_capability(
    hass: HomeAssistant, client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    """Only this frame failed; the capability is still advertised (#1813 T1)."""
    sensor = _device(hass, entry, "dev-1")
    socket = await pooled_socket(client)

    async with held_command(hass, client, DEVICE_GET):
        with (
            sent_commands(socket) as sent,
            invalidations(component_devices) as invalidate,
        ):
            resp = await _get(client, device_id=sensor.device_id)

    assert resp["device"]["id"] == sensor.device_id
    assert resp["entity_count"] == 1
    types = sent_types(sent)
    assert (types.count(DEVICE_DUMP), types.count(ENTITY_DUMP)) == (1, 1)
    invalidate.assert_not_called()


async def test_an_unreachable_component_socket_falls_back_and_keeps_the_capability(
    hass: HomeAssistant, client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    """The legacy reads ride the WebSocket bridge, not the component's socket."""
    sensor = _device(hass, entry, "dev-1")
    assert await component_api.get_component_caps(client) is not None
    socket = await pooled_socket(client)

    with (
        patch.object(
            component_devices,
            "get_websocket_client",
            AsyncMock(side_effect=HomeAssistantConnectionError("socket down")),
        ),
        sent_commands(socket) as sent,
        invalidations(component_devices) as invalidate,
    ):
        resp = await _get(client, device_id=sensor.device_id)

    assert resp["device"]["id"] == sensor.device_id
    assert resp["entity_count"] == 1
    types = sent_types(sent)
    assert (types.count(DEVICE_DUMP), types.count(ENTITY_DUMP)) == (1, 1)
    invalidate.assert_not_called()


async def test_a_component_without_device_list_is_invalidated_and_falls_back(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The list read follows device_get's downgrade branch (#1813 T2)."""
    _device(hass, entry, "d1")
    assert await component_api.get_component_caps(client) is not None
    monkeypatch.delitem(hass.data["websocket_api"], DEVICE_LIST)
    socket = await pooled_socket(client)

    with sent_commands(socket) as sent, invalidations(component_devices) as invalidate:
        resp = await _get(client)

    assert resp["total_devices"] == 1
    assert sent_types(sent).count(DEVICE_DUMP) == 1
    invalidate.assert_called_once_with(client)


async def test_a_timed_out_device_list_falls_back_and_keeps_the_capability(
    hass: HomeAssistant, client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    _device(hass, entry, "d1")
    socket = await pooled_socket(client)

    async with held_command(hass, client, DEVICE_LIST):
        with (
            sent_commands(socket) as sent,
            invalidations(component_devices) as invalidate,
        ):
            resp = await _get(client)

    assert resp["total_devices"] == 1
    assert sent_types(sent).count(DEVICE_DUMP) == 1
    invalidate.assert_not_called()
