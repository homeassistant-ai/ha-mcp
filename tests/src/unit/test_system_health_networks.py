"""The Z-Wave and Matter sections of ``ha_get_system_health`` on real Home Assistant Core.

Core's own ``zwave_js`` and ``matter`` integrations answer every command. Only
the server below each library is replaced, as Core's own tests replace it: the
Z-Wave JS server connection (``_zwave_js``) and the Matter server client, a
mock of ``MatterClient`` that returns matter-python-client's real topology
models.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components import matter as matter_integration
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from matter_server.common.const import SCHEMA_VERSION
from matter_server.common.models import (
    NetworkTopology,
    NetworkTopologyConnection,
    NetworkTopologyNode,
    ServerInfoMessage,
)
from pytest_homeassistant.common import MockConfigEntry
from zwave_js_server.model.version import VersionInfo

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.system_health_sections import MATTER_TOPOLOGY_LIMIT
from ha_mcp.tools.tools_system import SystemTools

from ._zwave_js import (
    HOME_ID,
    NODE_ID,
    node_state,
    set_up_zwave_js,
    zwave_js_server_connection,
)

# The first Matter server schema that serves the network topology.
TOPOLOGY_SCHEMA_VERSION = 13


@pytest.fixture(autouse=True)
async def _core_commands(hass: HomeAssistant) -> None:
    """Core's ``system_health`` baseline and its ``config_entries/get``."""
    assert await async_setup_component(hass, "system_health", {})
    assert await async_setup_component(hass, "config", {})


async def _section(client: HomeAssistantClient, name: str) -> dict[str, Any]:
    result = await SystemTools(client).ha_get_system_health(include=name)
    section: dict[str, Any] = result[name]
    return section


async def test_zwave_network_reports_the_client_and_moves_the_nodes_out(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    with zwave_js_server_connection(node_state([])) as server:
        await set_up_zwave_js(hass)
        # What Core reads for the status: the connection's own attributes, the
        # server's version message and its answer to ``controller.get_state``.
        server.version = VersionInfo(
            driver_version="15.0.0",
            server_version="3.0.0",
            home_id=HOME_ID,
            min_schema_version=0,
            max_schema_version=44,
        )
        server.ws_server_url = "ws://test.org"
        server.server_logging_enabled = False
        server.async_send_command.return_value = {
            "state": {
                "homeId": HOME_ID,
                "ownNodeId": 1,
                "inclusionState": 0,
                "status": 0,
            }
        }

        zwave = await _section(core_client, "zwave_network")

    assert "error" not in zwave
    assert zwave["client"]["state"] == "connected"
    assert "nodes" not in zwave["controller"]
    assert [node["node_id"] for node in zwave["nodes"]] == [NODE_ID]
    assert zwave["nodes"][0]["ready"] is True
    assert (zwave["count"], zwave["total_count"]) == (1, 1)


async def test_zwave_network_skips_a_disabled_entry(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    MockConfigEntry(
        domain="zwave_js", disabled_by=ConfigEntryDisabler.USER
    ).add_to_hass(hass)

    zwave = await _section(core_client, "zwave_network")

    assert zwave["error"] == "No enabled Z-Wave JS config entry found"


@pytest.fixture
def matter_server() -> Iterator[MagicMock]:
    """The Matter server client, as Core's ``matter_client`` fixture mocks it."""
    with patch.object(
        matter_integration, "MatterClient", autospec=True
    ) as client_class:
        client = client_class.return_value

        async def listen(init_ready: asyncio.Event | None) -> None:
            if init_ready is not None:
                init_ready.set()
            await asyncio.Event().wait()

        client.connect = AsyncMock()
        client.check_node_update = AsyncMock(return_value=None)
        client.start_listening = AsyncMock(side_effect=listen)
        client.server_info = ServerInfoMessage(
            fabric_id=12341234,
            compressed_fabric_id=1234,
            schema_version=TOPOLOGY_SCHEMA_VERSION,
            sdk_version="2022.11.1",
            wifi_credentials_set=True,
            thread_credentials_set=True,
            min_supported_schema_version=SCHEMA_VERSION,
            bluetooth_enabled=False,
        )
        yield client


async def _set_up_matter(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain="matter", data={"url": "ws://localhost:5580/ws"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _topology(node_count: int) -> NetworkTopology:
    """Thread nodes, the last one offline, each linked to the first."""
    nodes = [
        NetworkTopologyNode(
            id=str(node_id),
            kind="matter",
            network_type="thread",
            node_id=node_id,
            role="router",
            available=node_id != node_count,
        )
        for node_id in range(1, node_count + 1)
    ]
    connections = [
        NetworkTopologyConnection(
            source=node.id, target="1", network="thread", strength="strong"
        )
        for node in nodes[1:]
    ]
    return NetworkTopology(
        collected_at=1767888000000, nodes=nodes, connections=connections
    )


async def test_matter_network_forwards_the_entry_and_the_topology(
    hass: HomeAssistant, core_client: HomeAssistantClient, matter_server: MagicMock
) -> None:
    entry = await _set_up_matter(hass)
    matter_server.get_network_topology = AsyncMock(return_value=_topology(3))

    matter = await _section(core_client, "matter_network")

    assert matter["config_entry"]["entry_id"] == entry.entry_id
    assert matter["config_entry"]["state"] == "loaded"
    assert matter["config_entry"]["supports_options"] is False
    topology = matter["topology"]
    assert topology["collected_at"] == 1767888000000
    assert (topology["node_count"], topology["connection_count"]) == (3, 2)
    assert [node["available"] for node in topology["nodes"]] == [True, True, False]
    assert all("ha_device_id" in node for node in topology["nodes"])
    assert "truncated" not in topology


async def test_matter_network_caps_nodes_and_connections(
    hass: HomeAssistant, core_client: HomeAssistantClient, matter_server: MagicMock
) -> None:
    await _set_up_matter(hass)
    total = MATTER_TOPOLOGY_LIMIT + 10
    matter_server.get_network_topology = AsyncMock(return_value=_topology(total))

    topology = (await _section(core_client, "matter_network"))["topology"]

    assert len(topology["nodes"]) == len(topology["connections"]) == 50
    assert (topology["node_count"], topology["connection_count"]) == (total, total - 1)
    assert topology["truncated"] is True


async def test_matter_network_keeps_the_entry_when_the_server_has_no_topology(
    hass: HomeAssistant, core_client: HomeAssistantClient, matter_server: MagicMock
) -> None:
    matter_server.server_info.schema_version = TOPOLOGY_SCHEMA_VERSION - 1
    entry = await _set_up_matter(hass)

    matter = await _section(core_client, "matter_network")

    assert matter["config_entry"]["entry_id"] == entry.entry_id
    assert "does not support network topology" in matter["topology"]["error"]


async def test_matter_network_skips_a_disabled_entry(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    MockConfigEntry(domain="matter", disabled_by=ConfigEntryDisabler.USER).add_to_hass(
        hass
    )

    matter = await _section(core_client, "matter_network")

    assert matter == {"error": "No enabled Matter config entry found"}
