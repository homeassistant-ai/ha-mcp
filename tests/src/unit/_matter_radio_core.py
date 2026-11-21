"""Core's real ``matter`` integration on one commissioned Matter node.

As Core's own matter tests do, only the Matter server client
(``homeassistant.components.matter.MatterClient``) is replaced: an autospec mock
holding matter-python-client's real ``MatterNode``, built from the attributes
the server reports for the node. Each server command is an ``AsyncMock`` a test
reads back or gives the server's answer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components import matter
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from matter_server.client.models.node import (
    MatterFabricData,
    MatterNode,
    NetworkType,
    NodeDiagnostics,
    NodeType,
)
from matter_server.common.const import SCHEMA_VERSION
from matter_server.common.models import MatterNodeData, ServerInfoMessage
from pytest_homeassistant.common import MockConfigEntry

NODE_ID = 1
COMPRESSED_FABRIC_ID = 0xDEADBEEF
HOME_ASSISTANT_FABRIC = 2
OTHER_CONTROLLER_FABRIC = 1

# Endpoint 0 of a light: root node device type, basic information and the OTA
# requestor (idle, ready for an update), as the server reports them.
_ATTRIBUTES = {
    "0/29/0": [{"0": 22, "1": 1}],
    "0/29/1": [29, 40, 42],
    "0/40/1": "Acme",
    "0/40/2": 65521,
    "0/40/3": "Bulb",
    "0/40/4": 32768,
    "0/40/9": 1,
    "0/40/10": "1.0",
    "0/42/1": True,
    "0/42/2": 1,
    "0/42/3": None,
}


def node_diagnostics() -> NodeDiagnostics:
    """What the server reports for the node: on Thread, joined to two fabrics."""
    return NodeDiagnostics(
        node_id=NODE_ID,
        network_type=NetworkType.THREAD,
        node_type=NodeType.END_DEVICE,
        network_name="my-thread",
        ip_adresses=["fe80::1"],
        mac_address="aa:bb:cc:dd:ee:ff:00:11",
        available=True,
        active_fabrics=[
            MatterFabricData(fabric_id=99, vendor_id=4939, fabric_index=fabric)
            for fabric in (OTHER_CONTROLLER_FABRIC, HOME_ASSISTANT_FABRIC)
        ],
        active_fabric_index=HOME_ASSISTANT_FABRIC,
    )


@contextmanager
def matter_server_client() -> Iterator[MagicMock]:
    """The Matter server client, holding the node with ``_ATTRIBUTES``."""
    node = MatterNode(
        MatterNodeData(
            node_id=NODE_ID,
            date_commissioned=datetime(2026, 1, 1, tzinfo=UTC),
            last_interview=datetime(2026, 1, 1, tzinfo=UTC),
            interview_version=6,
            available=True,
            attributes=dict(_ATTRIBUTES),
        )
    )
    with patch.object(matter, "MatterClient", autospec=True) as client_class:
        client = client_class.return_value
        stop = asyncio.Event()

        async def start_listening(init_ready: asyncio.Event | None) -> None:
            if init_ready is not None:
                init_ready.set()
            await stop.wait()

        client.connect = AsyncMock()
        client.start_listening = AsyncMock(side_effect=start_listening)
        client.check_node_update = AsyncMock(return_value=None)
        client.get_nodes.return_value = [node]
        client.get_node.return_value = node
        client.node_diagnostics = AsyncMock(return_value=node_diagnostics())
        client.server_info = ServerInfoMessage(
            fabric_id=1,
            compressed_fabric_id=COMPRESSED_FABRIC_ID,
            schema_version=SCHEMA_VERSION,
            min_supported_schema_version=SCHEMA_VERSION,
            sdk_version="2025.1.0",
            wifi_credentials_set=True,
            thread_credentials_set=True,
            bluetooth_enabled=False,
        )
        yield client


async def set_up_matter(hass: HomeAssistant) -> str:
    """Core's ``matter`` integration on the patched server; the node's device id."""
    entry = MockConfigEntry(
        domain=matter.DOMAIN, data={"url": "ws://localhost:5580/ws"}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    identifier = f"deviceid_{COMPRESSED_FABRIC_ID:016X}-{NODE_ID:016X}-MatterNodeDevice"
    device = dr.async_get(hass).async_get_device_by_identifier(
        (matter.DOMAIN, identifier), entry.entry_id
    )
    assert device is not None
    return device.id
