"""Radio enrichment of a single ``ha_get_device`` lookup.

A Z-Wave JS device's node id comes from its identifier, a ZHA device's IEEE
address likewise, and each is used to read the radio's own status. Core's
real ``zwave_js`` and ``zha`` integrations answer those reads, set up as Core's
own tests set them up; only the hardware below each library is replaced:

- Z-Wave JS: the connection to the Z-Wave JS server
  (``homeassistant.components.zwave_js.ZwaveClient``) is a mock whose driver,
  controller and node are zwave-js-server-python's real models, built from the
  node state the server would send.
- ZHA: the radio is a zigpy ``ControllerApplication`` whose serial I/O does
  nothing, handed to ZHA where bellows would open the EZSP stick; the device
  and its link quality are zigpy's own device objects.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import zigpy.config
import zigpy.device
import zigpy.state
import zigpy.types
import zigpy.zdo.types as zdo_t
from homeassistant.components.zha import const as zha_const
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from zigpy.application import ControllerApplication
from zwave_js_server.const import NodeStatus

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import tools_registry
from ha_mcp.tools.tools_registry import RegistryTools

from ._client_spies import sent_messages
from ._zwave_js import (
    HOME_ID,
    NODE_ID,
    node_state,
    set_up_zwave_js,
    zwave_js_server_connection,
)

_IEEE = "00:11:22:33:44:55:66:77"
_COORDINATOR_IEEE = "00:15:8d:00:02:32:4f:32"


@pytest.fixture(autouse=True)
async def _registry_commands(hass: HomeAssistant) -> None:
    """Core's ``config`` commands, which serve the device lookup's reads."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
def tools(core_client: HomeAssistantClient) -> RegistryTools:
    return RegistryTools(core_client)


def _device(hass: HomeAssistant, domain: str, identifier: str) -> dr.DeviceEntry:
    """A device in Core's registry, owned by a new config entry of ``domain``."""
    entry = MockConfigEntry(domain=domain)
    entry.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(domain, identifier)}
    )


@pytest.fixture
def zwave_js_server() -> Iterator[MagicMock]:
    """The Z-Wave JS server connection, holding node 5 with no values."""
    with zwave_js_server_connection(node_state([])) as client:
        yield client


@pytest.fixture
async def zwave_js(hass: HomeAssistant, zwave_js_server: MagicMock) -> MockConfigEntry:
    """Core's ``zwave_js`` integration, connected to ``zwave_js_server``."""
    return await set_up_zwave_js(hass)


@pytest.mark.parametrize(
    "identifier",
    [None, f"{HOME_ID}-{NODE_ID}-134:3:100"],
    ids=["node", "node-with-product"],
)
async def test_zwave_node_id_and_status_come_from_the_identifier(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools: RegistryTools,
    zwave_js: MockConfigEntry,
    identifier: str | None,
) -> None:
    """``{home_id}-{node_id}`` and the product-suffixed form both name node 5,
    whose status is read for this device.

    ``None`` is the device Core registers for node 5; the product-suffixed
    identifier alone is the device a replaced node leaves behind.
    """
    registry = dr.async_get(hass)
    if identifier is None:
        dev = registry.async_get_device_by_identifier(
            ("zwave_js", f"{HOME_ID}-{NODE_ID}"), zwave_js.entry_id
        )
        assert dev is not None
    else:
        dev = registry.async_get_or_create(
            config_entry_id=zwave_js.entry_id, identifiers={("zwave_js", identifier)}
        )

    with sent_messages(core_client) as spy:
        result = await tools.ha_get_device(device_id=dev.id)

    assert result["success"] is True
    device = result["device"]
    assert device["integration_type"] == "zwave_js"
    assert device["node_id"] == NODE_ID
    # Z-Wave JS's whole reply, including fields such as ``ready``.
    assert device["node_status"]["node_id"] == NODE_ID
    assert device["node_status"]["status"] == NodeStatus.ALIVE
    assert device["node_status"]["ready"] is True
    assert {"type": "zwave_js/node_status", "device_id": dev.id} in [
        call.args[0] for call in spy.call_args_list
    ]
    assert "warnings" not in result


# A SmartStart DSK: eight dash-separated five-digit blocks, as on a QR code.
_DSK = "34673-15546-46480-39591-32400-22155-07715-45994"


async def test_pre_provisioned_zwave_device_gets_no_node_id(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools: RegistryTools,
    zwave_js: MockConfigEntry,
) -> None:
    """A SmartStart device provisioned with a name but not yet included has no
    node: Core names it ``provision_<dsk>`` and its status read refuses. A
    block of the DSK must not be reported as its node id."""
    provisioned = await core_client.send_websocket_message(
        {
            "type": "zwave_js/provision_smart_start_node",
            "entry_id": zwave_js.entry_id,
            "qr_provisioning_information": {
                "version": 1,
                "securityClasses": [0],
                "dsk": _DSK,
                "genericDeviceClass": 1,
                "specificDeviceClass": 1,
                "installerIconType": 1,
                "manufacturerId": 1,
                "productType": 1,
                "productId": 1,
                "applicationVersion": "1.0",
            },
            "device_name": "Porch sensor",
        }
    )
    assert provisioned["success"] is True
    dev = dr.async_get(hass).async_get_device_by_identifier(
        ("zwave_js", f"provision_{_DSK}"), zwave_js.entry_id
    )
    assert dev is not None

    result = await tools.ha_get_device(device_id=dev.id)

    device = result["device"]
    assert device["integration_type"] == "zwave_js"
    assert "node_id" not in device
    assert "node_status" not in device
    assert result["warnings"] == [
        f"Z-Wave node status unavailable: Node for device {dev.id} can't be found"
    ]


class _RadioWithoutHardware(ControllerApplication):
    """zigpy's application with its radio I/O doing nothing, as Core's
    ``_FakeApp``: the network, devices and link quality are zigpy's own."""

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


@pytest.fixture
def zigbee_network() -> _RadioWithoutHardware:
    """A coordinator and one end device heard at LQI 200, RSSI -45 dBm."""
    app = _RadioWithoutHardware(
        {
            zigpy.config.CONF_DATABASE: None,
            zigpy.config.CONF_DEVICE: {zigpy.config.CONF_DEVICE_PATH: "/dev/null"},
            zigpy.config.CONF_STARTUP_ENERGY_SCAN: False,
            zigpy.config.CONF_NWK_BACKUP_ENABLED: False,
            zigpy.config.CONF_TOPO_SCAN_ENABLED: False,
            zigpy.config.CONF_OTA: {zigpy.config.CONF_OTA_ENABLED: False},
        }
    )
    coordinator_ieee = zigpy.types.EUI64.convert(_COORDINATOR_IEEE)
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
        ieee=zigpy.types.EUI64.convert(_IEEE), nwk=zigpy.types.NWK(0xB79C)
    )
    device.node_desc = zdo_t.NodeDescriptor(logical_type=zdo_t.LogicalType.EndDevice)
    device.lqi = 200
    device.rssi = -45
    return app


@pytest.fixture
async def zha(
    hass: HomeAssistant, zigbee_network: _RadioWithoutHardware
) -> MockConfigEntry:
    """Core's ``zha`` integration on ``zigbee_network``, as Core's
    ``setup_zha`` fixture sets it up."""
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
        "bellows.zigbee.application.ControllerApplication.new",
        return_value=zigbee_network,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_zha_device_gets_ieee_address_and_radio_metrics(
    hass: HomeAssistant, tools: RegistryTools, zha: MockConfigEntry
) -> None:
    """ZHA lists the coordinator too; the device gets its own link quality."""
    dev = dr.async_get(hass).async_get_device_by_identifier(
        ("zha", _IEEE), zha.entry_id
    )
    assert dev is not None

    result = await tools.ha_get_device(device_id=dev.id)

    device = result["device"]
    assert device["integration_type"] == "zha"
    assert device["ieee_address"] == _IEEE
    assert device["radio_metrics"] == {"lqi": 200, "rssi": -45}
    # A radio read that succeeded leaves the answer without warnings.
    assert "warnings" not in result


@pytest.mark.parametrize(
    ("domain", "identifier", "reader", "command", "kept", "absent", "warning"),
    [
        (
            "zha",
            _IEEE,
            "enrich_zha_metrics",
            "zha/devices",
            ("ieee_address", _IEEE),
            "radio_metrics",
            "ZHA radio metrics",
        ),
        (
            "zwave_js",
            f"{HOME_ID}-{NODE_ID}",
            "enrich_zwave_status",
            "zwave_js/node_status",
            ("integration_type", "zwave_js"),
            "node_status",
            "Z-Wave node status",
        ),
    ],
    ids=["zha", "zwave_js"],
)
async def test_unanswered_radio_read_still_returns_the_device(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    tools: RegistryTools,
    domain: str,
    identifier: str,
    reader: str,
    command: str,
    kept: tuple[str, str],
    absent: str,
    warning: str,
) -> None:
    """A radio read that gets no answer does not fail the lookup: the device
    comes back without the radio fields, the identifier parsing intact, and a
    warning names the skipped read.

    The client gets no answer only while the radio read runs; the lookup's
    other reads are Core's.
    """
    unanswered: list[AsyncMock] = []
    read = getattr(tools_registry, reader)

    async def read_without_answer(client: HomeAssistantClient, *args: Any) -> None:
        with patch.object(
            client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("Connection lost")),
        ) as no_answer:
            unanswered.append(no_answer)
            await read(client, *args)

    monkeypatch.setattr(tools_registry, reader, read_without_answer)
    dev = _device(hass, domain, identifier)

    result = await tools.ha_get_device(device_id=dev.id)

    assert result["success"] is True
    device = result["device"]
    assert absent not in device
    assert device[kept[0]] == kept[1]
    assert [
        call.args[0]["type"] for mock in unanswered for call in mock.await_args_list
    ] == [command]
    assert result["warnings"] == [f"{warning} unavailable: Connection lost"]
