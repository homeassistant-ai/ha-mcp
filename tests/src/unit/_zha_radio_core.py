"""Core's real ``zha`` integration on a zigpy radio that keeps what it sends.

The radio is ``_zha_core``'s, handed to ZHA where bellows would open the EZSP
stick; its network is the coordinator and a light (``IEEE``) on ``ENDPOINT``.
Every packet ZHA sends is kept. A broadcast, a groupcast and a packet to the
coordinator itself go out, and the radio moves itself to a new channel as a
radio library does. The light answers each ZCL request as a light does: its
On/Off attribute reads as on, a write and a command succeed, a group is joined
or left. It answers no ZDO request, so those are not acknowledged, at once.
"""

from __future__ import annotations

from typing import Any

import zigpy.config
import zigpy.device
import zigpy.endpoint
import zigpy.exceptions
import zigpy.profiles.zha
import zigpy.state
import zigpy.types as t
import zigpy.zdo
import zigpy.zdo.types as zdo_t
from zigpy.zcl import foundation
from zigpy.zcl.clusters.general import Basic, Groups, LevelControl, OnOff, Ota

from ._zha_core import COORDINATOR_IEEE, IEEE, RadioWithoutHardware

ENDPOINT = 1
_LIGHT_NWK = 0xB79C


def _answer(
    cluster_id: int, header: foundation.ZCLHeader, request: Any
) -> tuple[int, bool, bytes]:
    """The light's reply to a ZCL request: command id, cluster-specific, payload."""
    general = foundation.GeneralCommand
    done = {"status": foundation.Status.SUCCESS}
    command = header.command_id if header.frame_control.is_general else None
    if cluster_id == Groups.cluster_id and header.frame_control.is_cluster:
        response = getattr(Groups.ClientCommandDefs, f"{request.command.name}_response")
        payload = response.schema(group_id=request.group_id, **done)
        return response.id, True, payload.serialize()
    if command == general.Read_Attributes:
        records = [_read(cluster_id, attrid) for attrid in request.attribute_ids]
        payload = foundation.ReadAttributesResponse(status_records=records)
        return general.Read_Attributes_rsp, False, payload.serialize()
    if command == general.Write_Attributes:
        written = foundation.WriteAttributesStatusRecord(**done)
        payload = foundation.WriteAttributesResponseSchema(status_records=[written])
        return general.Write_Attributes_rsp, False, payload.serialize()
    if command == general.Configure_Reporting:
        configured = foundation.ConfigureReportingResponseRecord(**done)
        reply = foundation.ConfigureReportingResponse([configured]).serialize()
        return general.Configure_Reporting_rsp, False, reply
    payload = foundation.DefaultResponse(command_id=header.command_id, **done)
    return general.Default_Response, False, payload.serialize()


def _read(cluster_id: int, attrid: int) -> foundation.ReadAttributeRecord:
    if (cluster_id, attrid) != (OnOff.cluster_id, OnOff.AttributeDefs.on_off.id):
        return foundation.ReadAttributeRecord(
            attrid=t.uint16_t(attrid), status=foundation.Status.UNSUPPORTED_ATTRIBUTE
        )
    on = foundation.TypeValue(
        type=foundation.DataType.from_python_type(t.Bool).type_id, value=t.Bool.true
    )
    return foundation.ReadAttributeRecord(
        attrid=t.uint16_t(attrid), status=foundation.Status.SUCCESS, value=on
    )


class LightNetworkRadio(RadioWithoutHardware):
    """The radio of a network of the coordinator and one light."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(config)
        self.sent: list[t.ZigbeePacket] = []

    def light_cluster(self, cluster_id: int) -> Any:
        light = self.get_device(nwk=t.NWK(_LIGHT_NWK))
        assert isinstance(light, zigpy.device.Device)
        endpoint = light.endpoints[ENDPOINT]
        return endpoint.in_clusters.get(cluster_id) or endpoint.out_clusters[cluster_id]

    def requests_to(self, cluster_id: int) -> list[tuple[foundation.ZCLHeader, Any]]:
        """Each ZCL request sent to the light's cluster, parsed by zigpy."""
        return [
            self.light_cluster(cluster_id).deserialize(packet.data.serialize())
            for packet in self.sent
            if packet.cluster_id == cluster_id and packet.dst_ep == ENDPOINT
        ]

    async def send_packet(self, packet: t.ZigbeePacket) -> None:
        self.sent.append(packet)
        assert packet.dst is not None
        if packet.dst.addr_mode != t.AddrMode.NWK or packet.dst.address == 0x0000:
            return
        if packet.dst_ep == zigpy.zdo.ZDO_ENDPOINT:
            raise zigpy.exceptions.DeliveryError("The light did not acknowledge")
        self.packet_received(self._light_reply(packet))

    def _light_reply(self, packet: t.ZigbeePacket) -> t.ZigbeePacket:
        cluster = self.light_cluster(packet.cluster_id)
        header, request = cluster.deserialize(packet.data.serialize())
        command_id, is_cluster, payload = _answer(packet.cluster_id, header, request)
        make = (
            foundation.ZCLHeader.cluster if is_cluster else foundation.ZCLHeader.general
        )
        reply_header = make(
            header.tsn, command_id, direction=header.frame_control.direction.flip()
        )
        return t.ZigbeePacket(
            src=t.AddrModeAddress(addr_mode=t.AddrMode.NWK, address=t.NWK(_LIGHT_NWK)),
            src_ep=t.uint8_t(ENDPOINT),
            dst=t.AddrModeAddress(addr_mode=t.AddrMode.NWK, address=t.NWK(0x0000)),
            dst_ep=packet.src_ep,
            tsn=header.tsn,
            profile_id=packet.profile_id,
            cluster_id=packet.cluster_id,
            data=t.SerializableBytes(reply_header.serialize() + payload),
            lqi=t.uint8_t(200),
            rssi=t.int8s(-45),
        )

    async def _move_network_to_channel(
        self, new_channel: int, new_nwk_update_id: int
    ) -> None:
        self.state.network_info.channel = t.uint8_t(new_channel)
        self.state.network_info.nwk_update_id = t.uint8_t(new_nwk_update_id)


def light_network() -> LightNetworkRadio:
    """The coordinator on channel 15 and a light, interviewed when it joined."""
    radio = LightNetworkRadio(
        {
            zigpy.config.CONF_DATABASE: None,
            zigpy.config.CONF_DEVICE: {zigpy.config.CONF_DEVICE_PATH: "/dev/null"},
            zigpy.config.CONF_STARTUP_ENERGY_SCAN: False,
            zigpy.config.CONF_NWK_BACKUP_ENABLED: False,
            zigpy.config.CONF_TOPO_SCAN_ENABLED: False,
            zigpy.config.CONF_OTA: {zigpy.config.CONF_OTA_ENABLED: False},
        }
    )
    coordinator_ieee = t.EUI64.convert(COORDINATOR_IEEE)
    network = radio.state.network_info
    radio.state.node_info.nwk = t.NWK(0x0000)
    radio.state.node_info.ieee = coordinator_ieee
    network.pan_id = t.PanId(0x1234)
    network.extended_pan_id = t.ExtendedPanId(coordinator_ieee)
    network.channel = t.uint8_t(15)
    network.network_key = zigpy.state.Key(key=t.KeyData(range(16)), seq=t.uint8_t(1))
    coordinator = radio.add_device(ieee=coordinator_ieee, nwk=t.NWK(0x0000))
    coordinator.node_desc = zdo_t.NodeDescriptor(
        logical_type=zdo_t.LogicalType.Coordinator
    )
    coordinator.add_endpoint(1)
    light = radio.add_device(ieee=t.EUI64.convert(IEEE), nwk=t.NWK(_LIGHT_NWK))
    light.node_desc = zdo_t.NodeDescriptor(logical_type=zdo_t.LogicalType.EndDevice)
    endpoint = light.add_endpoint(ENDPOINT)
    endpoint.profile_id = zigpy.profiles.zha.PROFILE_ID
    endpoint.device_type = zigpy.profiles.zha.DeviceType.ON_OFF_LIGHT
    for cluster in (Basic, Groups, OnOff, LevelControl):
        endpoint.add_input_cluster(cluster.cluster_id)
    endpoint.add_output_cluster(Ota.cluster_id)
    endpoint.status = zigpy.endpoint.Status.ZDO_INIT
    return radio
