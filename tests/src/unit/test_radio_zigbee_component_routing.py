"""Routing tests for ``_resolve_ieee`` over the ``ha_mcp_tools`` component gate.

``ha_manage_radio``'s ZHA actions resolve a ``device_id`` to its IEEE address, and
every one of those 7 call sites went through ``_resolve_ieee``, which dumped the
ENTIRE device registry to read one device's identifiers. When the component
advertises ``device_get`` that becomes a single in-process read. These tests pin
the shared ``_resolve_ieee`` seam: component-hit (no whole-registry dump), legacy
fallback on no-caps, and ``unknown_command`` → invalidate caps + legacy list.

The server's own client runs against a real ``hass`` with a ZHA device in Core's
device registry; the component's commands are registered only where a test
needs them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import websocket_manager
from ha_mcp.tools import component_devices
from ha_mcp.tools.component_api import ComponentCaps
from ha_mcp.tools.radio.zigbee import _resolve_ieee

_IEEE = "00:11:22:33:44:55:66:77"


@pytest.fixture
async def zha_device(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry
) -> dr.DeviceEntry:
    """A ZHA device in Core's registry, readable over ``config/*``."""
    assert await async_setup_component(hass, "config", {})
    entry = MockConfigEntry(domain="zha")
    entry.add_to_hass(hass)
    return device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("zha", _IEEE)},
        name="Zigbee Sensor",
    )


@contextmanager
def _routes(client: HomeAssistantClient, socket: Any) -> Iterator[tuple[Any, Any]]:
    """Spies on the legacy bridge and the component socket; both call through."""
    with (
        patch.object(
            client, "send_websocket_message", wraps=client.send_websocket_message
        ) as legacy,
        patch.object(socket, "send_command", wraps=socket.send_command) as component,
    ):
        yield legacy, component


async def _socket(client: HomeAssistantClient) -> Any:
    return await websocket_manager.get_client(
        url=client.base_url, token=client.token, verify_ssl=False
    )


def _device_list_calls(legacy: Any) -> list[Any]:
    return [
        c
        for c in legacy.call_args_list
        if c.args[0].get("type") == "config/device_registry/list"
    ]


def _device_get_calls(component: Any) -> list[Any]:
    return [
        c for c in component.call_args_list if c.args[0] == "ha_mcp_tools/device_get"
    ]


async def test_resolve_ieee_served_by_component(
    component_core_client: HomeAssistantClient, zha_device: dr.DeviceEntry
) -> None:
    socket = await _socket(component_core_client)
    with _routes(component_core_client, socket) as (legacy, component):
        ieee = await _resolve_ieee(component_core_client, zha_device.id)

    assert ieee == _IEEE
    assert len(_device_list_calls(legacy)) == 0
    assert len(_device_get_calls(component)) == 1
    assert _device_get_calls(component)[0].kwargs["device_id"] == zha_device.id


async def test_resolve_ieee_capsless_uses_legacy_list(
    core_client: HomeAssistantClient, zha_device: dr.DeviceEntry
) -> None:
    # Without the component, Core answers the capability probe with
    # unknown_command.
    socket = await _socket(core_client)
    with _routes(core_client, socket) as (legacy, component):
        ieee = await _resolve_ieee(core_client, zha_device.id)

    assert ieee == _IEEE
    assert len(_device_list_calls(legacy)) == 1
    assert not _device_get_calls(component)


async def test_resolve_ieee_unknown_command_invalidates_and_falls_back(
    core_client: HomeAssistantClient,
    zha_device: dr.DeviceEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cached capabilities say device_get; Core, without the component,
    # answers the command with unknown_command.
    caps = ComponentCaps(
        1,
        "1.1.0",
        frozenset({"device_get", "device_list", "device_registry_child_semantics"}),
        {},
    )
    monkeypatch.setattr(
        component_devices, "get_component_caps", AsyncMock(return_value=caps)
    )
    invalidated = MagicMock(wraps=component_devices.invalidate_caps)
    monkeypatch.setattr(component_devices, "invalidate_caps", invalidated)

    socket = await _socket(core_client)
    with _routes(core_client, socket) as (legacy, component):
        ieee = await _resolve_ieee(core_client, zha_device.id)

    assert ieee == _IEEE
    assert len(_device_get_calls(component)) == 1
    assert len(_device_list_calls(legacy)) == 1
    invalidated.assert_called_once_with(core_client)
