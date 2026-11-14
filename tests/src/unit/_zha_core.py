"""Core's real ``zha`` integration on a zigpy radio whose serial I/O does nothing.

The radio is a zigpy ``ControllerApplication`` handed to ZHA where bellows would
open the EZSP stick, as Core's own ``_FakeApp`` does; the network, the devices
and their link quality are zigpy's own objects.
"""

from __future__ import annotations

from unittest.mock import patch

import zigpy.config
import zigpy.device
import zigpy.state
import zigpy.types
import zigpy.zdo.types as zdo_t
from homeassistant.components.zha import const as zha_const
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from zigpy.application import ControllerApplication

IEEE = "00:11:22:33:44:55:66:77"
COORDINATOR_IEEE = "00:15:8d:00:02:32:4f:32"


class RadioWithoutHardware(ControllerApplication):
    """zigpy's application with its radio I/O doing nothing."""

    async def add_endpoint(self, descriptor: zdo_t.SimpleDescriptor) -> None:
        pass

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def force_remove(self, dev: zigpy.device.Device) -> None:
        pass

    async def load_network_info(self, *, load_devices: bool = False) -> None:
        pass

    async def permit_ncp(self, time_s: int = 60) -> None:
        pass

    async def permit_with_link_key(
        self, node: zigpy.types.EUI64, link_key: zigpy.types.KeyData, time_s: int = 60
    ) -> None:
        pass

    async def reset_network_info(self) -> None:
        pass

    async def send_packet(self, packet: zigpy.types.ZigbeePacket) -> None:
        pass

    async def start_network(self) -> None:
        pass

    async def write_network_info(
        self, *, network_info: zigpy.state.NetworkInfo, node_info: zigpy.state.NodeInfo
    ) -> None:
        pass


def zigbee_network() -> RadioWithoutHardware:
    """A coordinator and one end device (``IEEE``) heard at LQI 200, RSSI -45 dBm."""
    app = RadioWithoutHardware(
        {
            zigpy.config.CONF_DATABASE: None,
            zigpy.config.CONF_DEVICE: {zigpy.config.CONF_DEVICE_PATH: "/dev/null"},
            zigpy.config.CONF_STARTUP_ENERGY_SCAN: False,
            zigpy.config.CONF_NWK_BACKUP_ENABLED: False,
            zigpy.config.CONF_TOPO_SCAN_ENABLED: False,
            zigpy.config.CONF_OTA: {zigpy.config.CONF_OTA_ENABLED: False},
        }
    )
    coordinator_ieee = zigpy.types.EUI64.convert(COORDINATOR_IEEE)
    app.state.node_info.nwk = zigpy.types.NWK(0x0000)
    app.state.node_info.ieee = coordinator_ieee
    app.state.network_info.pan_id = zigpy.types.PanId(0x1234)
    app.state.network_info.extended_pan_id = zigpy.types.ExtendedPanId(coordinator_ieee)
    app.state.network_info.channel = zigpy.types.uint8_t(15)
    coordinator = app.add_device(ieee=coordinator_ieee, nwk=zigpy.types.NWK(0x0000))
    coordinator.node_desc = zdo_t.NodeDescriptor(
        logical_type=zdo_t.LogicalType.Coordinator
    )
    coordinator.add_endpoint(1)
    device = app.add_device(
        ieee=zigpy.types.EUI64.convert(IEEE), nwk=zigpy.types.NWK(0xB79C)
    )
    device.node_desc = zdo_t.NodeDescriptor(logical_type=zdo_t.LogicalType.EndDevice)
    device.lqi = 200
    device.rssi = -45
    return app


async def set_up_zha(
    hass: HomeAssistant, network: RadioWithoutHardware
) -> MockConfigEntry:
    """Core's ``zha`` integration on ``network``, as Core's ``setup_zha`` fixture."""
    entry = MockConfigEntry(
        version=5,
        domain=zha_const.DOMAIN,
        data={
            zigpy.config.CONF_DEVICE: {
                zigpy.config.CONF_DEVICE_PATH: "/dev/ttyUSB0",
                zigpy.config.CONF_DEVICE_BAUDRATE: 115200,
                zigpy.config.CONF_DEVICE_FLOW_CONTROL: "hardware",
            },
            zha_const.CONF_RADIO_TYPE: "ezsp",
        },
    )
    entry.add_to_hass(hass)
    with patch(
        "bellows.zigbee.application.ControllerApplication.new", return_value=network
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry
