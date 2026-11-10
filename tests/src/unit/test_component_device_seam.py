"""Cross-seam contract tests for the component's ``device_get`` / ``device_list``.

The devices and entities live in Core's own registries. The REAL component
functions read them and serve the result through the mocked WS transport to the
REAL server tools, so a key a consumer reads that Core stops emitting fails here
as well as a consumer that misreads the component's output.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.tools import component_devices
from ha_mcp.tools.radio.zigbee import _resolve_ieee

from ._component_routing_helpers import patch_ws
from ._component_ws_api import wsapi
from .test_component_readapi_contract import _real_component_ws
from .test_ha_get_device_component_routing import (
    RoutingClient as GetDeviceRoutingClient,
)
from .test_ha_get_device_component_routing import (
    _build_get_device,
)
from .test_ha_remove_device_component_routing import (
    RoutingClient as RemoveDeviceRoutingClient,
)
from .test_ha_remove_device_component_routing import (
    _build_remove_device,
)
from .test_radio_zigbee_component_routing import (
    RoutingClient as ZigbeeRoutingClient,
)


class TestDeviceSeam:
    """Core's device and entity rows reconcile every consumer.

    One REAL ``_do_device_get`` output drives consumer sites through their real
    server code: ``ha_get_device`` (its device transform AND its per-device entity
    join via ``include_entities``), ``ha_remove_device`` (its device lookup), and
    ``_resolve_ieee`` (its ``identifiers`` read). Each consumer is served the
    device in one in-process ``device_get`` frame, never the whole-registry dump;
    the entity join carries ``config/entity_registry/list``-shaped rows so the
    device's entity list needs no separate entity-registry dump either.
    """

    @pytest.fixture
    def dev(self, hass: HomeAssistant) -> dr.DeviceEntry:
        """A ZHA device in Core's own registries, owned by config entry ``cfg-1``."""
        MockConfigEntry(domain="zha", entry_id="cfg-1").add_to_hass(hass)
        registry = dr.async_get(hass)
        device = registry.async_get_or_create(
            config_entry_id="cfg-1",
            identifiers={("zha", "00:11:22:33:44:55:66:77")},
            name="Kitchen Sensor",
            manufacturer="Aqara",
            model="T1",
            suggested_area="Kitchen",
        )
        updated = registry.async_update_device(
            device.id,
            name_by_user="Kitchen",
            labels={lr.async_get(hass).async_create("important").label_id},
        )
        assert updated is not None
        return updated

    @pytest.mark.asyncio
    async def test_get_device_transform_from_real_component(
        self, hass: HomeAssistant, dev: dr.DeviceEntry
    ) -> None:
        entity_registry = er.async_get(hass)
        sensor = entity_registry.async_get_or_create(
            "sensor", "zha", "kitchen-temp", device_id=dev.id
        )
        entity_registry.async_update_entity(sensor.entity_id, name="Kitchen Temp")
        update = entity_registry.async_get_or_create(
            "update", "zha", "kitchen-fw", device_id=dev.id
        )
        # config/entity_registry/list lists disabled entities too.
        disabled = entity_registry.async_get_or_create(
            "sensor",
            "zha",
            "kitchen-rssi",
            device_id=dev.id,
            disabled_by=er.RegistryEntryDisabler.USER,
        )
        client = GetDeviceRoutingClient(
            entries=[{"entry_id": "cfg-1", "domain": "zha"}]
        )
        # A zha device triggers the ZHA metrics enricher (a dedicated path outside
        # the device_get seam); serve it an empty result so it is a graceful no-op.
        base_send = client.send_websocket_message

        async def _send(msg: Any) -> dict[str, Any]:
            if msg.get("type") == "zha/devices":
                return {"success": True, "result": []}
            return await base_send(msg)

        client.send_websocket_message = _send  # type: ignore[assignment]
        ws = _real_component_ws(hass)
        get_device = _build_get_device(client)
        with patch_ws(ws, component_devices):
            resp = await get_device(device_id=dev.id)

        assert resp["success"] is True
        info = resp["device"]
        # Read out of Core's dict_repr; integration_type is the domain the
        # client's config_entries/get answer gives the owning entry.
        assert info["device_id"] == dev.id
        assert (info["name"], info["name_by_user"]) == ("Kitchen Sensor", "Kitchen")
        assert info["area_id"] == "kitchen"
        assert info["labels"] == ["important"]
        assert info["config_entry_id"] == "cfg-1"
        assert info["integration_type"] == "zha"
        assert info["ieee_address"] == "00:11:22:33:44:55:66:77"
        # The device's entity list came from the include_entities join over the REAL
        # component output — the whole device AND entity registries were never dumped.
        assert resp["entity_count"] == 3
        assert {e["entity_id"] for e in resp["entities"]} == {
            sensor.entity_id,
            update.entity_id,
            disabled.entity_id,
        }
        assert client.device_list_calls == 0
        assert client.entity_list_calls == 0

        # Pin the RAW entity-row shape the component emits (config/entity_registry/list
        # parity, device_id carried) so the join survives byte-for-byte across the seam.
        raw = wsapi._do_device_get(
            hass, {"device_id": dev.id, "include_entities": True}
        )
        raw_row = next(e for e in raw["entities"] if e["entity_id"] == sensor.entity_id)
        assert raw_row["device_id"] == dev.id
        assert raw_row["platform"] == "zha"
        assert raw_row["name"] == "Kitchen Temp"

    @pytest.mark.asyncio
    async def test_core_2026_9_child_device_is_retained_with_effective_area(
        self, hass: HomeAssistant
    ) -> None:
        """Core's reduced child row reaches the public tools.

        The row has no direct ``area_id`` and names its owner only in
        ``config_entry_id`` (no ``primary_config_entry``, no ``config_entries``).
        The public ``area_id`` carries Core's effective parent-derived area and
        ``integration_type`` the owning entry's domain, while the raw component
        record keeps ``parent_device_id`` and its own empty ``area_id``.
        """
        MockConfigEntry(domain="hue", entry_id="cfg-hue").add_to_hass(hass)
        registry = dr.async_get(hass)
        parent = registry.async_get_or_create(
            config_entry_id="cfg-hue",
            identifiers={("hue", "bridge")},
            name="Bridge",
            suggested_area="Office",
        )
        child = registry.async_get_or_create_child(
            config_entry_id="cfg-hue",
            identifiers={("hue", "ch1")},
            name="Channel 1",
            parent_device_id=parent.id,
        )
        client = GetDeviceRoutingClient(
            entries=[{"entry_id": "cfg-hue", "domain": "hue"}]
        )
        ws = _real_component_ws(hass)
        get_device = _build_get_device(client)
        with patch_ws(ws, component_devices):
            listed = await get_device()
            single = await get_device(device_id=child.id)

        by_id = {row["device_id"]: row for row in listed["devices"]}
        assert set(by_id) == {parent.id, child.id}
        assert by_id[child.id]["area_id"] == "office"
        assert single["device"]["area_id"] == "office"
        assert single["device"]["integration_type"] == "hue"
        raw = wsapi._do_device_list(hass, {})["devices"]
        raw_child = next(row for row in raw if row["id"] == child.id)
        assert raw_child["parent_device_id"] == parent.id
        assert raw_child["area_id"] is None

    @pytest.mark.asyncio
    async def test_remove_device_from_real_component(
        self, hass: HomeAssistant, dev: dr.DeviceEntry, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = MagicMock()
        settings.enable_auto_backup = False
        monkeypatch.setattr(
            "ha_mcp.tools.auto_backup.get_global_settings", lambda: settings
        )
        client = RemoveDeviceRoutingClient()
        ws = _real_component_ws(hass)
        remove_device = _build_remove_device(client)
        with patch_ws(ws, component_devices):
            resp = await remove_device(device_id=dev.id)

        assert resp["success"] is True
        assert resp["device_name"] == "Kitchen"
        # The device was looked up through device_get and removed by id.
        assert client.remove_calls == [dev.id]
        assert client.device_list_calls == 0

    @pytest.mark.asyncio
    async def test_resolve_ieee_from_real_component(
        self, hass: HomeAssistant, dev: dr.DeviceEntry
    ) -> None:
        client = ZigbeeRoutingClient()
        ws = _real_component_ws(hass)
        with patch_ws(ws, component_devices):
            ieee = await _resolve_ieee(client, dev.id)

        # _resolve_ieee parsed the identifiers out of the same raw shape.
        assert ieee == "00:11:22:33:44:55:66:77"
        assert client.device_list_calls == 0
