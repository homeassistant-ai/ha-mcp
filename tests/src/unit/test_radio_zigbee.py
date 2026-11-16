"""``ha_manage_radio``'s Zigbee (ZHA) actions on real Home Assistant Core.

Core's own ``zha`` integration answers every ``zha/*`` command and runs every
``zha.*`` service, on a zigpy radio that keeps the packets it sends and a light
that answers them (``_zha_radio_core.py``). A test asserts on what reached Core,
on what went out on the air, and on Core's own registries.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import zigpy.application
import zigpy.types as t
import zigpy.zdo.types as zdo_t
from homeassistant.components import websocket_api
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, async_capture_events
from pytest_homeassistant.typing import WebSocketGenerator
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import Groups, LevelControl, OnOff

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_radio import RadioTools

from ._command_faults import HELD_WAIT_S, received
from ._component_core import ws_result
from ._tool_errors import error_of
from ._zha_core import COORDINATOR_IEEE, IEEE, set_up_zha
from ._zha_radio_core import ENDPOINT, LightNetworkRadio, light_network

LIGHT_MEMBER = {"ieee": IEEE, "endpoint_id": ENDPOINT}


@pytest.fixture
def radio(core_client: HomeAssistantClient) -> RadioTools:
    return RadioTools(core_client)


@pytest.fixture
def network() -> LightNetworkRadio:
    return light_network()


@pytest.fixture
async def zha_entry(hass: HomeAssistant, network: LightNetworkRadio) -> MockConfigEntry:
    """Core's ``zha`` integration running ``network``, with Core's ``config``."""
    assert await async_setup_component(hass, "config", {})
    entry = await set_up_zha(hass, network)
    network.sent.clear()
    return entry


def _device_id(hass: HomeAssistant, entry: MockConfigEntry, ieee: str) -> str:
    device = dr.async_get(hass).async_get_device_by_identifier(
        ("zha", ieee), entry.entry_id
    )
    assert device is not None
    return device.id


@pytest.fixture
def light_id(hass: HomeAssistant, zha_entry: MockConfigEntry) -> str:
    return _device_id(hass, zha_entry, IEEE)


def _general(
    network: LightNetworkRadio, cluster_id: int, command: foundation.GeneralCommand
) -> list[Any]:
    return [
        request
        for header, request in network.requests_to(cluster_id)
        if header.frame_control.is_general and header.command_id == command
    ]


def _cluster_commands(network: LightNetworkRadio, cluster_id: int) -> list[Any]:
    return [
        request
        for header, request in network.requests_to(cluster_id)
        if header.frame_control.is_cluster
    ]


async def test_network_status_returns_the_settings_and_refreshes_the_topology(
    hass: HomeAssistant,
    radio: RadioTools,
    zha_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ZHA network's settings come back, and a topology scan is started so
    the next read shows current routes. Core starts the scan without answering
    ``zha/topology/update``, so the tool must not wait for a reply: the
    shortened wait makes a tool that does fail at once."""
    monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", HELD_WAIT_S)

    with received(hass, "zha/topology/update") as core:
        out = await radio.ha_manage_radio(radio="zigbee", action="network_status")
        async with asyncio.timeout(5):
            while not core["zha/topology/update"]:
                await asyncio.sleep(0)

    assert out["config_entry_id"] == zha_entry.entry_id
    assert out["network"]["settings"]["network_info"]["channel"] == 15
    assert len(core["zha/topology/update"]) == 1


async def test_network_status_survives_a_refused_topology_refresh(
    hass: HomeAssistant, radio: RadioTools, zha_entry: MockConfigEntry
) -> None:
    """The topology refresh is best effort: a Core that refuses it (here one
    without the command) still gets the network settings back."""
    hass.data[websocket_api.DOMAIN].pop("zha/topology/update")

    out = await radio.ha_manage_radio(radio="zigbee", action="network_status")

    assert out["success"] is True
    assert out["config_entry_id"] == zha_entry.entry_id
    assert out["network"]["settings"]["network_info"]["channel"] == 15


async def test_network_status_without_zha_reports_it_unavailable(
    hass: HomeAssistant, radio: RadioTools
) -> None:
    assert await async_setup_component(hass, "config", {})

    out = await radio.ha_manage_radio(radio="zigbee", action="network_status")

    assert out["success"] is True
    assert out["available"] is False
    assert out["warnings"]


async def test_cluster_read_reads_the_lights_server_cluster(
    radio: RadioTools, network: LightNetworkRadio, light_id: str
) -> None:
    """Without a cluster_type the read goes to the light's own (server) On/Off
    cluster, and the light's answer comes back."""
    out = await radio.ha_manage_radio(
        radio="zigbee",
        action="cluster_read",
        device_id=light_id,
        params={
            "endpoint_id": ENDPOINT,
            "cluster_id": OnOff.cluster_id,
            "attribute": OnOff.AttributeDefs.on_off.id,
        },
    )

    assert out["ieee"] == IEEE
    assert out["value"] == str(t.Bool.true)
    # The first read is the tool's; ZHA reads more once the light answers.
    reads = _general(
        network, OnOff.cluster_id, foundation.GeneralCommand.Read_Attributes
    )
    assert reads[0].attribute_ids == [OnOff.AttributeDefs.on_off.id]


async def test_permit_join_opens_the_whole_network_for_a_minute(
    hass: HomeAssistant,
    radio: RadioTools,
    network: LightNetworkRadio,
    zha_entry: MockConfigEntry,
) -> None:
    with received(hass, "zha/devices/permit") as core:
        out = await radio.ha_manage_radio(radio="zigbee", action="permit_join")

    assert (out["duration"], out["ieee"]) == (60, None)
    assert [
        (msg["duration"], msg.get("ieee")) for msg in core["zha/devices/permit"]
    ] == [(60, None)]
    permits = [
        packet
        for packet in network.sent
        if packet.cluster_id == zdo_t.ZDOCmd.Mgmt_Permit_Joining_req
    ]
    assert permits
    assert all(
        packet.dst is not None and packet.dst.addr_mode == t.AddrMode.Broadcast
        for packet in permits
    )


async def test_permit_join_through_a_device_names_its_address(
    hass: HomeAssistant, radio: RadioTools, zha_entry: MockConfigEntry
) -> None:
    """A device_id opens the network through that device: Core gets its IEEE
    address (here the coordinator's, which opens joining on itself)."""
    coordinator_id = _device_id(hass, zha_entry, COORDINATOR_IEEE)

    with received(hass, "zha/devices/permit") as core:
        out = await radio.ha_manage_radio(
            radio="zigbee",
            action="permit_join",
            device_id=coordinator_id,
            params={"duration": 30},
        )

    assert (out["duration"], out["ieee"]) == (30, COORDINATOR_IEEE)
    assert [
        (msg["duration"], str(msg["ieee"])) for msg in core["zha/devices/permit"]
    ] == [(30, COORDINATOR_IEEE)]


async def test_remove_device_removes_the_light_from_home_assistant(
    hass: HomeAssistant, radio: RadioTools, network: LightNetworkRadio, light_id: str
) -> None:
    """ZHA asks the light to leave and drops it, so Core's device registry no
    longer holds it."""
    out = await radio.ha_manage_radio(
        radio="zigbee", action="remove_device", device_id=light_id, confirm=True
    )
    await hass.async_block_till_done()

    assert out["ieee"] == IEEE
    assert dr.async_get(hass).async_get(light_id) is None
    assert any(
        packet.cluster_id == zdo_t.ZDOCmd.Mgmt_Leave_req for packet in network.sent
    )


async def test_reconfigure_reinterviews_the_light(
    hass: HomeAssistant, radio: RadioTools, network: LightNetworkRadio, light_id: str
) -> None:
    with received(hass, "zha/devices/reconfigure") as core:
        out = await radio.ha_manage_radio(
            radio="zigbee", action="reconfigure", device_id=light_id
        )
    await hass.async_block_till_done()

    assert out["ieee"] == IEEE
    assert [str(msg["ieee"]) for msg in core["zha/devices/reconfigure"]] == [IEEE]
    assert any(
        packet.cluster_id == zdo_t.ZDOCmd.Node_Desc_req for packet in network.sent
    )


async def test_a_device_of_another_integration_is_not_a_zigbee_device(
    hass: HomeAssistant, radio: RadioTools, zha_entry: MockConfigEntry
) -> None:
    hue = MockConfigEntry(domain="hue")
    hue.add_to_hass(hass)
    lamp = dr.async_get(hass).async_get_or_create(
        config_entry_id=hue.entry_id, identifiers={("hue", "abc")}
    )

    with pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(
            radio="zigbee", action="diagnostics", device_id=lamp.id
        )

    assert "not a ZHA Zigbee device" in error_of(exc)["message"]


async def test_firmware_update_installs_the_lights_update_entity(
    hass: HomeAssistant, radio: RadioTools, light_id: str
) -> None:
    calls = async_capture_events(hass, EVENT_CALL_SERVICE)

    out = await radio.ha_manage_radio(
        radio="zigbee", action="firmware_update", device_id=light_id
    )

    assert out["long_running"] is True
    installs = [
        call.data["service_data"]
        for call in calls
        if (call.data["domain"], call.data["service"]) == ("update", "install")
    ]
    assert installs == [{"entity_id": out["entity_id"]}]
    assert out["entity_id"].startswith("update.")


async def _zha_groups(hass: HomeAssistant, ws: WebSocketGenerator) -> dict[int, str]:
    groups = await ws_result(await ws(hass), "zha/groups")
    return {group["group_id"]: group["name"] for group in groups}


async def test_group_add_creates_the_group(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    radio: RadioTools,
    zha_entry: MockConfigEntry,
) -> None:
    out = await radio.ha_manage_radio(
        radio="zigbee", action="group_add", params={"group_name": "Kitchen"}
    )

    groups = await _zha_groups(hass, hass_ws_client)
    assert groups == {out["group"]["group_id"]: "Kitchen"}


async def test_group_remove_deletes_the_group(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    radio: RadioTools,
    zha_entry: MockConfigEntry,
) -> None:
    group = await radio.ha_manage_radio(
        radio="zigbee", action="group_add", params={"group_name": "Kitchen"}
    )
    group_id = group["group"]["group_id"]

    out = await radio.ha_manage_radio(
        radio="zigbee",
        action="group_remove",
        params={"group_ids": [group_id]},
        confirm=True,
    )

    assert out["success"] is True
    assert await _zha_groups(hass, hass_ws_client) == {}


async def test_group_members_add_joins_the_light_to_the_group(
    radio: RadioTools, network: LightNetworkRadio, zha_entry: MockConfigEntry
) -> None:
    group = await radio.ha_manage_radio(
        radio="zigbee", action="group_add", params={"group_name": "Kitchen"}
    )
    group_id = group["group"]["group_id"]

    out = await radio.ha_manage_radio(
        radio="zigbee",
        action="group_members_add",
        params={"group_id": group_id, "members": [LIGHT_MEMBER]},
    )

    assert [
        (member["device"]["ieee"], member["endpoint_id"])
        for member in out["group"]["members"]
    ] == [(IEEE, ENDPOINT)]
    joins = _cluster_commands(network, Groups.cluster_id)
    assert [(join.command.name, join.group_id) for join in joins] == [("add", group_id)]


async def test_group_members_remove_takes_the_light_out_of_the_group(
    radio: RadioTools, network: LightNetworkRadio, zha_entry: MockConfigEntry
) -> None:
    group = await radio.ha_manage_radio(
        radio="zigbee", action="group_add", params={"group_name": "Kitchen"}
    )
    group_id = group["group"]["group_id"]
    await radio.ha_manage_radio(
        radio="zigbee",
        action="group_members_add",
        params={"group_id": group_id, "members": [LIGHT_MEMBER]},
    )

    out = await radio.ha_manage_radio(
        radio="zigbee",
        action="group_members_remove",
        params={"group_id": group_id, "members": [LIGHT_MEMBER]},
    )

    assert out["group"]["members"] == []
    leaves = _cluster_commands(network, Groups.cluster_id)
    assert (leaves[-1].command.name, leaves[-1].group_id) == ("remove", group_id)


@pytest.mark.parametrize(("action", "confirm"), [("bind", False), ("unbind", True)])
async def test_bind_and_unbind_send_both_addresses(
    hass: HomeAssistant,
    radio: RadioTools,
    zha_entry: MockConfigEntry,
    action: str,
    confirm: bool,
) -> None:
    command = f"zha/devices/{action}"

    with received(hass, command) as core:
        out = await radio.ha_manage_radio(
            radio="zigbee",
            action=action,
            params={"source_ieee": IEEE, "target_ieee": COORDINATOR_IEEE},
            confirm=confirm,
        )

    assert out["success"] is True
    assert [
        (str(msg["source_ieee"]), str(msg["target_ieee"])) for msg in core[command]
    ] == [(IEEE, COORDINATOR_IEEE)]


async def test_cluster_write_writes_the_lights_server_cluster(
    radio: RadioTools, network: LightNetworkRadio, light_id: str
) -> None:
    """The write goes to the light's own Level Control cluster, and a level of
    0 is written, not dropped as empty."""
    out = await radio.ha_manage_radio(
        radio="zigbee",
        action="cluster_write",
        device_id=light_id,
        params={
            "endpoint_id": ENDPOINT,
            "cluster_id": LevelControl.cluster_id,
            "attribute": LevelControl.AttributeDefs.current_level.id,
            "value": 0,
        },
        confirm=True,
    )

    assert out["ieee"] == IEEE
    writes = _general(
        network, LevelControl.cluster_id, foundation.GeneralCommand.Write_Attributes
    )
    assert [
        [(record.attrid, record.value.value) for record in write.attributes]
        for write in writes
    ] == [[(LevelControl.AttributeDefs.current_level.id, 0)]]


async def test_cluster_command_sends_the_command_to_the_light(
    radio: RadioTools, network: LightNetworkRadio, light_id: str
) -> None:
    out = await radio.ha_manage_radio(
        radio="zigbee",
        action="cluster_command",
        device_id=light_id,
        params={
            "endpoint_id": ENDPOINT,
            "cluster_id": OnOff.cluster_id,
            "command": OnOff.ServerCommandDefs.off.id,
            "command_type": "server",
            "params": {},
        },
        confirm=True,
    )

    assert out["success"] is True
    commands = _cluster_commands(network, OnOff.cluster_id)
    assert [command.command.name for command in commands] == ["off"]


async def test_cluster_command_without_arguments_reaches_the_light(
    radio: RadioTools, network: LightNetworkRadio, light_id: str
) -> None:
    """A command that takes no arguments needs none passed: only the parameters
    the action lists as required are given."""
    await radio.ha_manage_radio(
        radio="zigbee",
        action="cluster_command",
        device_id=light_id,
        params={
            "endpoint_id": ENDPOINT,
            "cluster_id": OnOff.cluster_id,
            "command": OnOff.ServerCommandDefs.off.id,
            "command_type": "server",
        },
        confirm=True,
    )

    commands = _cluster_commands(network, OnOff.cluster_id)
    assert [command.command.name for command in commands] == ["off"]


async def test_network_backup_returns_the_networks_backup(
    radio: RadioTools, zha_entry: MockConfigEntry
) -> None:
    out = await radio.ha_manage_radio(radio="zigbee", action="network_backup")

    network_info = out["backup"]["backup"]["network_info"]
    assert (network_info["channel"], network_info["pan_id"]) == (15, "1234")
    assert out["backup"]["is_complete"] is True


async def test_network_restore_restores_a_backup(
    hass: HomeAssistant, radio: RadioTools, zha_entry: MockConfigEntry
) -> None:
    backup = (await radio.ha_manage_radio(radio="zigbee", action="network_backup"))[
        "backup"
    ]["backup"]

    with received(hass, "zha/network/backups/restore") as core:
        out = await radio.ha_manage_radio(
            radio="zigbee",
            action="network_restore",
            params={"backup": backup},
            confirm=True,
        )

    assert out["success"] is True
    restored = core["zha/network/backups/restore"]
    assert [msg["backup"].as_dict()["network_info"] for msg in restored] == [
        backup["network_info"]
    ]


async def test_change_channel_moves_the_network(
    radio: RadioTools,
    network: LightNetworkRadio,
    zha_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The network is told to move and the coordinator moves with it. zigpy's
    pause between its announcements is cut so the test does not wait."""
    monkeypatch.setattr(zigpy.application, "CHANNEL_CHANGE_BROADCAST_DELAY_S", 0)

    out = await radio.ha_manage_radio(
        radio="zigbee",
        action="change_channel",
        params={"new_channel": 25},
        confirm=True,
    )

    assert out["new_channel"] == 25
    assert network.state.network_info.channel == 25
    assert any(
        packet.cluster_id == zdo_t.ZDOCmd.Mgmt_NWK_Update_req
        and packet.dst is not None
        and packet.dst.addr_mode == t.AddrMode.Broadcast
        for packet in network.sent
    )
