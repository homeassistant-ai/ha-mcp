"""Routing tests for ``resolve_update_entity`` over the ``ha_mcp_tools`` gate.

``ha_manage_radio``'s firmware actions (zha / zwave_js / matter update installs)
resolve a device's ``update.*`` entity through ``resolve_update_entity``, which
dumped the ENTIRE entity registry (``config/entity_registry/list``) to filter one
device's rows. When the component advertises ``device_get`` those per-device rows
ride one ``device_get(include_entities=True)`` frame instead — the ``update.*`` /
platform filtering stays client-side. These tests pin that seam: component-hit (no
whole-registry dump), legacy fallback on no-caps, ``unknown_command`` → invalidate
caps + legacy list, and an additive-param-blind component (device served, entities
half omitted) degrading to the legacy list rather than reporting no update entity.

The server's own client runs on a real ``hass`` whose registries hold the device;
Core answers the capability probe, the component's ``device_get`` and the legacy
registry list.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.tools import component_devices
from ha_mcp.tools.component_api import DEVICE_REGISTRY_CHILD_SEMANTICS, ComponentCaps
from ha_mcp.tools.radio.base import resolve_update_entity


@pytest.fixture
async def device_id(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> str:
    """A zha device with a sensor and a firmware ``update`` entity."""
    assert await async_setup_component(hass, "config", {})
    entry = MockConfigEntry(domain="zha")
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("zha", "dev-1")}
    )
    for domain, object_id in (("sensor", "temp"), ("update", "dev_firmware")):
        entity_registry.async_get_or_create(
            domain,
            "zha",
            object_id,
            suggested_object_id=object_id,
            config_entry=entry,
            device_id=device.id,
        )
    return device.id


async def _component_socket(
    client: HomeAssistantClient,
) -> HomeAssistantWebSocketClient:
    """The pooled socket the component route sends on."""
    return await websocket_manager.get_client(
        url=client.base_url, token=client.token, verify_ssl=False
    )


def _entity_list_calls(legacy: AsyncMock) -> list[Any]:
    return [
        c
        for c in legacy.call_args_list
        if c.args[0].get("type") == "config/entity_registry/list"
    ]


def _device_get_calls(component: AsyncMock) -> list[Any]:
    return [
        c
        for c in component.call_args_list
        if c.args[0] == component_devices.WS_DEVICE_GET
    ]


@pytest.fixture
def invalidated(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """A spy on the server's capability invalidation; the real one still runs."""
    spy = MagicMock(wraps=component_devices.invalidate_caps)
    monkeypatch.setattr(component_devices, "invalidate_caps", spy)
    return spy


@pytest.mark.asyncio
async def test_update_entity_served_by_component(
    component_core_client: HomeAssistantClient, device_id: str
) -> None:
    """The device's rows ride one device_get(include_entities) frame; the whole
    entity registry is never dumped, and the update.* filter still runs."""
    socket = await _component_socket(component_core_client)
    with (
        patch.object(
            component_core_client,
            "send_websocket_message",
            wraps=component_core_client.send_websocket_message,
        ) as legacy,
        patch.object(socket, "send_command", wraps=socket.send_command) as component,
    ):
        eid = await resolve_update_entity(
            component_core_client, device_id, platform="zha"
        )

    assert eid == "update.dev_firmware"
    assert not _entity_list_calls(legacy)
    assert len(_device_get_calls(component)) == 1
    assert _device_get_calls(component)[0].kwargs["device_id"] == device_id
    assert _device_get_calls(component)[0].kwargs["include_entities"] is True


@pytest.mark.asyncio
async def test_update_entity_capsless_uses_legacy_list(
    core_client: HomeAssistantClient, device_id: str
) -> None:
    # Without the component Core answers the capability probe unknown_command.
    socket = await _component_socket(core_client)
    with (
        patch.object(
            core_client,
            "send_websocket_message",
            wraps=core_client.send_websocket_message,
        ) as legacy,
        patch.object(socket, "send_command", wraps=socket.send_command) as component,
    ):
        eid = await resolve_update_entity(core_client, device_id, platform="zha")

    assert eid == "update.dev_firmware"
    assert len(_entity_list_calls(legacy)) == 1
    assert not _device_get_calls(component)


@pytest.mark.asyncio
async def test_update_entity_unknown_command_invalidates_and_falls_back(
    core_client: HomeAssistantClient,
    device_id: str,
    invalidated: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cached capabilities say device_get; Core, without the component,
    # answers that command unknown_command.
    served = ComponentCaps(
        1,
        "1.1.0",
        frozenset({"device_get", "device_list", DEVICE_REGISTRY_CHILD_SEMANTICS}),
        {},
    )
    monkeypatch.setattr(
        component_devices, "get_component_caps", AsyncMock(return_value=served)
    )
    with patch.object(
        core_client,
        "send_websocket_message",
        wraps=core_client.send_websocket_message,
    ) as legacy:
        eid = await resolve_update_entity(core_client, device_id, platform="zha")

    assert eid == "update.dev_firmware"
    assert len(_entity_list_calls(legacy)) == 1
    invalidated.assert_called_once_with(core_client)


@pytest.mark.asyncio
async def test_update_entity_entities_half_absent_falls_back_to_legacy(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    device_registry: dr.DeviceRegistry,
    device_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device_get-capable component that omits the additive entities half (an
    older build predating include_entities) degrades to the legacy entity list
    rather than reporting no update entity."""
    from custom_components.ha_mcp_tools import websocket_api as component_ws_api

    def _device_get_without_entities(
        hass: HomeAssistant, params: dict[str, Any]
    ) -> dict[str, Any]:
        device = device_registry.async_get(params["device_id"])
        return {"device": device.dict_repr if device is not None else None}

    # The component's device_get as an older build answered it.
    monkeypatch.setattr(
        "custom_components.ha_mcp_tools.websocket_api._do_device_get",
        _device_get_without_entities,
    )
    component_ws_api.async_register_commands(hass)

    with patch.object(
        component_core_client,
        "send_websocket_message",
        wraps=component_core_client.send_websocket_message,
    ) as legacy:
        eid = await resolve_update_entity(
            component_core_client, device_id, platform="zha"
        )

    assert eid == "update.dev_firmware"
    assert len(_entity_list_calls(legacy)) == 1
