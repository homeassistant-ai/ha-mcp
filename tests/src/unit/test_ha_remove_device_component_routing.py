"""Routing tests for ``ha_remove_device`` over the ``ha_mcp_tools`` component gate, on real Core.

``ha_remove_device`` reads one device, then removes it, and its auto-backup
capture reads the same device before a write. Both reads route through the
component's ``device_get`` when it answers and fall back to Core's device list
otherwise. The devices, the registries, the removal and every refusal are
Core's: the tests only watch which read was sent, and where a route needs an
answer Core does not give (no component, an unknown command), they remove that
command from Core and let Core answer the rest.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
)

from ha_mcp import backup_manager
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.config import get_global_settings
from ha_mcp.tools.tools_registry import RegistryTools

from ._client_spies import sent_commands, sent_messages
from ._removable_device import removable_device
from ._tool_errors import rejected_call_body

DEVICE_LIST = "config/device_registry/list"
DEVICE_GET = "ha_mcp_tools/device_get"
COMPONENT_INFO = "ha_mcp_tools/info"


@pytest.fixture
async def config_commands(hass: HomeAssistant) -> None:
    """Core's ``config`` commands: the device list, ``remove`` and the rest."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
async def tools(
    config_commands: None, component_core_client: HomeAssistantClient
) -> RegistryTools:
    """The registry tools on Core, with its ``config`` commands set up."""
    return RegistryTools(component_core_client)


@pytest.fixture
async def socket(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantWebSocketClient:
    """The pooled WebSocket client the component's reads go through."""
    return await websocket_manager.get_client(
        url=component_core_client.base_url,
        token=component_core_client.token,
        verify_ssl=False,
    )


def _device(hass: HomeAssistant, domain: str, identifier: str) -> dr.DeviceEntry:
    """A device in Core's registry, owned by a new config entry of ``domain``,
    identified by ``identifier`` within that domain."""
    entry = MockConfigEntry(domain=domain)
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    return registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(domain, identifier)}
    )


async def _set_up_orphan(hass: HomeAssistant) -> dr.DeviceEntry:
    return await removable_device(hass, name_by_user="Orphan")


async def _capture(client: HomeAssistantClient, device_id: str) -> Any:
    """The auto-backup capture read for a device, through the backup manager."""
    mgr = backup_manager.BackupManager(get_global_settings(), client)
    backup_manager.register_default_handlers(mgr, client)
    handler = mgr.handler_for("device")
    assert handler is not None
    return await handler.fetch(client, device_id)


def _bridge_types(spy: AsyncMock) -> list[str]:
    """The message types sent over the client's legacy WebSocket bridge."""
    return [call.args[0]["type"] for call in spy.call_args_list]


def _command_types(spy: AsyncMock) -> list[str]:
    """The commands sent over the component's pooled WebSocket client."""
    return [call.args[0] for call in spy.call_args_list]


# --- body routing ---------------------------------------------------------------
async def test_body_served_by_component(
    hass: HomeAssistant,
    tools: RegistryTools,
    component_core_client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """The body reads the device via device_get and removes it with Core's
    ``remove``; the whole device registry is never dumped."""
    dev = await _set_up_orphan(hass)

    with (
        sent_messages(component_core_client) as legacy,
        sent_commands(socket) as component,
    ):
        resp = await tools.ha_remove_device(device_id=dev.id)

    assert resp["success"] is True
    assert resp["device_name"] == "Orphan"
    assert dr.async_get(hass).async_get(dev.id) is None
    assert _command_types(component).count(DEVICE_GET) == 1
    assert DEVICE_LIST not in _bridge_types(legacy)


async def test_body_capsless_uses_legacy_list(
    hass: HomeAssistant,
    tools: RegistryTools,
    component_core_client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """Old component → legacy config/device_registry/list for the body."""
    dev = await _set_up_orphan(hass)
    hass.data[websocket_api.DOMAIN].pop(COMPONENT_INFO)

    with (
        sent_messages(component_core_client) as legacy,
        sent_commands(socket) as component,
    ):
        resp = await tools.ha_remove_device(device_id=dev.id)

    assert resp["success"] is True
    assert _bridge_types(legacy).count(DEVICE_LIST) == 1
    assert DEVICE_GET not in _command_types(component)


async def test_body_unknown_command_invalidates_and_falls_back(
    hass: HomeAssistant,
    tools: RegistryTools,
    component_core_client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """unknown_command on device_get → invalidate caps + legacy list."""
    dev = await _set_up_orphan(hass)
    hass.data[websocket_api.DOMAIN].pop(DEVICE_GET)

    with sent_messages(component_core_client) as legacy:
        resp = await tools.ha_remove_device(device_id=dev.id)

    assert resp["success"] is True
    assert _bridge_types(legacy).count(DEVICE_LIST) == 1
    # The dropped caps are probed again by the next call.
    with sent_commands(socket) as component, pytest.raises(ToolError):
        await tools.ha_remove_device(device_id="ghost")
    assert COMPONENT_INFO in _command_types(component)


async def test_body_not_found_falls_back_to_list_for_valid_ids(
    hass: HomeAssistant,
    tools: RegistryTools,
    component_core_client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """A component-reported missing device falls back to the LEGACY list so the
    not-found error can list valid ids (device_get authoritatively said None)."""
    other = _device(hass, "hue", "other")

    with (
        sent_messages(component_core_client) as legacy,
        sent_commands(socket) as component,
    ):
        error = await rejected_call_body(tools.ha_remove_device(device_id="ghost"))

    assert DEVICE_GET in _command_types(component)
    assert error["error"]["message"] == "Device not found: ghost"
    # The legacy device list served the not-found error's valid ids.
    assert _bridge_types(legacy).count(DEVICE_LIST) == 1
    assert error["available_device_ids"] == [other.id]


# --- auto-backup capture read routing ----------------------------
async def test_capture_read_served_by_component(
    hass: HomeAssistant,
    config_commands: None,
    component_core_client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """The pre-write snapshot capture (the device handler, shared by ha_set_device /
    ha_remove_device) reads the device via device_get and never dumps the whole
    registry through the legacy bridge."""
    dev = _device(hass, "hue", "0xAABB")

    with (
        sent_messages(component_core_client) as legacy,
        sent_commands(socket) as component,
    ):
        result = await _capture(component_core_client, dev.id)

    assert result["id"] == dev.id
    assert _command_types(component).count(DEVICE_GET) == 1
    assert DEVICE_LIST not in _bridge_types(legacy)


async def test_capture_read_capsless_uses_legacy_ws_send(
    hass: HomeAssistant,
    config_commands: None,
    component_core_client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """No device_get capability → the capture falls back to the legacy list read."""
    dev = _device(hass, "hue", "0xAABB")
    hass.data[websocket_api.DOMAIN].pop(COMPONENT_INFO)

    with sent_commands(socket) as component:
        result = await _capture(component_core_client, dev.id)

    assert result["id"] == dev.id
    assert DEVICE_GET not in _command_types(component)


async def test_capture_read_unknown_command_invalidates(
    hass: HomeAssistant,
    config_commands: None,
    component_core_client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """unknown_command on the capture's device_get → invalidate caps + legacy read."""
    dev = _device(hass, "hue", "0xAABB")
    hass.data[websocket_api.DOMAIN].pop(DEVICE_GET)

    with sent_commands(socket) as component:
        result = await _capture(component_core_client, dev.id)

    assert result["id"] == dev.id
    assert DEVICE_GET in _command_types(component)
    # The dropped caps are probed again by the next capture.
    with sent_commands(socket) as component:
        await _capture(component_core_client, dev.id)
    assert COMPONENT_INFO in _command_types(component)
