"""The ZHA device lookup behind ``ha_manage_radio`` over the ``ha_mcp_tools`` gate.

Every ZHA action of ``ha_manage_radio`` resolves its ``device_id`` to the
device's IEEE address first. When the component advertises ``device_get`` that
is one in-process read; otherwise the whole device registry is listed. Core's
real ``zha`` integration answers the diagnostics read that follows, on a zigpy
radio whose serial I/O does nothing (``_zha_core.py``).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.tools import component_devices
from ha_mcp.tools.component_api import DEVICE_REGISTRY_CHILD_SEMANTICS, ComponentCaps
from ha_mcp.tools.tools_radio import RadioTools

from ._client_spies import routing_spies
from ._zha_core import IEEE, set_up_zha, zigbee_network


@pytest.fixture
async def device_id(hass: HomeAssistant, device_registry: dr.DeviceRegistry) -> str:
    """The end device of a ZHA network that Core's ``zha`` integration runs."""
    assert await async_setup_component(hass, "config", {})
    entry = await set_up_zha(hass, zigbee_network())
    device = device_registry.async_get_device_by_identifier(
        ("zha", IEEE), entry.entry_id
    )
    assert device is not None
    return device.id


async def _component_socket(
    client: HomeAssistantClient,
) -> HomeAssistantWebSocketClient:
    """The pooled socket the component route sends on."""
    return await websocket_manager.get_client(
        url=client.base_url, token=client.token, verify_ssl=False
    )


def _sent(legacy: AsyncMock, msg_type: str) -> list[dict[str, Any]]:
    return [
        c.args[0] for c in legacy.call_args_list if c.args[0].get("type") == msg_type
    ]


def _device_get_calls(component: AsyncMock) -> list[Any]:
    return [
        c
        for c in component.call_args_list
        if c.args[0] == component_devices.WS_DEVICE_GET
    ]


async def _diagnostics(client: HomeAssistantClient, device_id: str) -> dict[str, Any]:
    result: dict[str, Any] = await RadioTools(client).ha_manage_radio(
        radio="zigbee", action="diagnostics", device_id=device_id
    )
    return result


async def test_ieee_served_by_component(
    component_core_client: HomeAssistantClient, device_id: str
) -> None:
    """The device comes from one device_get read; the whole registry is never
    dumped, and ZHA answers for the resolved device."""
    socket = await _component_socket(component_core_client)
    with routing_spies(component_core_client, socket) as (legacy, component):
        result = await _diagnostics(component_core_client, device_id)

    assert not _sent(legacy, "config/device_registry/list")
    assert len(_device_get_calls(component)) == 1
    assert _device_get_calls(component)[0].kwargs["device_id"] == device_id
    assert _sent(legacy, "zha/device")[0]["ieee"] == IEEE
    assert result["ieee"] == IEEE
    assert result["diagnostics"]["ieee"] == IEEE
    assert result["diagnostics"]["lqi"] == 200


async def test_ieee_without_component_uses_device_list(
    core_client: HomeAssistantClient, device_id: str
) -> None:
    """Without the component Core answers the capability probe unknown_command,
    so the IEEE comes from the whole device registry list."""
    socket = await _component_socket(core_client)
    with routing_spies(core_client, socket) as (legacy, component):
        result = await _diagnostics(core_client, device_id)

    assert len(_sent(legacy, "config/device_registry/list")) == 1
    assert not _device_get_calls(component)
    assert _sent(legacy, "zha/device")[0]["ieee"] == IEEE
    assert result["diagnostics"]["ieee"] == IEEE


async def test_ieee_unknown_command_invalidates_and_falls_back(
    core_client: HomeAssistantClient,
    device_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cached capabilities say device_get, but Core, without the component,
    answers it unknown_command: the caps are dropped and the lookup falls back."""
    invalidated = MagicMock(wraps=component_devices.invalidate_caps)
    monkeypatch.setattr(component_devices, "invalidate_caps", invalidated)
    served = ComponentCaps(
        1,
        "1.1.0",
        frozenset({"device_get", "device_list", DEVICE_REGISTRY_CHILD_SEMANTICS}),
        {},
    )
    monkeypatch.setattr(
        component_devices, "get_component_caps", AsyncMock(return_value=served)
    )
    with routing_spies(core_client, await _component_socket(core_client)) as (
        legacy,
        _component,
    ):
        result = await _diagnostics(core_client, device_id)

    assert len(_sent(legacy, "config/device_registry/list")) == 1
    assert _sent(legacy, "zha/device")[0]["ieee"] == IEEE
    assert result["diagnostics"]["ieee"] == IEEE
    invalidated.assert_called_once_with(core_client)
