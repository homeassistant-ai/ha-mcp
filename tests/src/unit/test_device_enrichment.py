"""Radio enrichment of a single ``ha_get_device`` lookup.

A Z-Wave JS device's node id comes from its identifier, a ZHA device's IEEE
address likewise, and each is used to read the radio's own status. The
devices and config entries are Core's; the ZHA and Z-Wave JS integrations
cannot be loaded here (their libraries are not installed with Core), so their
WebSocket commands are registered with Core's own decorator, as those
integrations register them.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_registry import RegistryTools

from ._client_spies import sent_messages

_IEEE = "00:11:22:33:44:55:66:77"
_HOME_ID = "3232323232"


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


def _radio_command(
    hass: HomeAssistant,
    command: str,
    reply: Any,
    fields: dict[Any, Any] | None = None,
) -> None:
    """Register the radio integration's ``command``; ``reply=None`` never answers."""

    @websocket_api.websocket_command({"type": command, **(fields or {})})
    @callback
    def handler(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        if reply is not None:
            connection.send_result(msg["id"], reply)

    websocket_api.async_register_command(hass, handler)


def _node_status(hass: HomeAssistant, reply: Any) -> None:
    _radio_command(hass, "zwave_js/node_status", reply, {"device_id": str})


def _sent_types(spy: Any) -> list[str]:
    return [call.args[0]["type"] for call in spy.call_args_list]


@pytest.mark.parametrize(
    "identifier",
    [f"{_HOME_ID}-5", f"{_HOME_ID}-5-134:3:100"],
    ids=["node", "node-with-product"],
)
async def test_zwave_node_id_and_status_come_from_the_identifier(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tools: RegistryTools,
    identifier: str,
) -> None:
    """``{home_id}-{node_id}`` and the product-suffixed form both name node 5,
    whose status is read for this device."""
    _node_status(
        hass,
        {
            "node_id": 5,
            "status": "alive",
            "is_routing": True,
            "is_secure": True,
            "highest_security_class": "S2_Authenticated",
            "zwave_plus_version": 2,
            "is_controller_node": False,
        },
    )
    dev = _device(hass, "zwave_js", identifier)

    with sent_messages(core_client) as spy:
        result = await tools.ha_get_device(device_id=dev.id)

    assert result["success"] is True
    device = result["device"]
    assert device["integration_type"] == "zwave_js"
    assert device["node_id"] == "5"
    assert device["node_status"]["node_id"] == 5
    assert device["node_status"]["status"] == "alive"
    assert {"type": "zwave_js/node_status", "device_id": dev.id} in [
        call.args[0] for call in spy.call_args_list
    ]
    assert "warnings" not in result


async def test_zwave_identifier_without_node_part_reads_no_status(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: RegistryTools
) -> None:
    """An identifier with no ``-{node_id}`` gives no node id, so no status
    is asked for."""
    dev = _device(hass, "zwave_js", _HOME_ID)

    with sent_messages(core_client) as spy:
        result = await tools.ha_get_device(device_id=dev.id)

    device = result["device"]
    assert device["integration_type"] == "zwave_js"
    assert "node_id" not in device
    assert "node_status" not in device
    assert "zwave_js/node_status" not in _sent_types(spy)


async def test_zha_device_gets_ieee_address_and_radio_metrics(
    hass: HomeAssistant, tools: RegistryTools
) -> None:
    _radio_command(
        hass,
        "zha/devices",
        [
            {"ieee": "aa:bb:cc:dd:ee:ff:00:11", "lqi": 10, "rssi": -90},
            {"ieee": _IEEE, "lqi": 200, "rssi": -45},
        ],
    )
    dev = _device(hass, "zha", _IEEE)

    result = await tools.ha_get_device(device_id=dev.id)

    device = result["device"]
    assert device["integration_type"] == "zha"
    assert device["ieee_address"] == _IEEE
    assert device["radio_metrics"] == {"lqi": 200, "rssi": -45}
    # A radio read that succeeded leaves the answer without warnings.
    assert "warnings" not in result


@pytest.mark.parametrize(
    ("domain", "identifier", "command", "fields", "kept", "absent", "warning"),
    [
        (
            "zha",
            _IEEE,
            "zha/devices",
            None,
            ("ieee_address", _IEEE),
            "radio_metrics",
            "ZHA radio metrics",
        ),
        (
            "zwave_js",
            f"{_HOME_ID}-5",
            "zwave_js/node_status",
            {"device_id": str},
            ("node_id", "5"),
            "node_status",
            "Z-Wave node status",
        ),
    ],
    ids=["zha", "zwave_js"],
)
async def test_unanswered_radio_read_still_returns_the_device(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    tools: RegistryTools,
    domain: str,
    identifier: str,
    command: str,
    fields: dict[Any, Any] | None,
    kept: tuple[str, str],
    absent: str,
    warning: str,
) -> None:
    """A radio read that gets no answer does not fail the lookup: the device
    comes back without the radio fields, the identifier parsing intact, and a
    warning names the skipped read."""
    _radio_command(hass, command, None, fields)
    # How long the client waits for an answer; shortened so the read gives up
    # quickly.
    monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", 1.0)
    dev = _device(hass, domain, identifier)

    with sent_messages(core_client) as spy:
        result = await tools.ha_get_device(device_id=dev.id)

    assert result["success"] is True
    device = result["device"]
    assert absent not in device
    assert device[kept[0]] == kept[1]
    assert _sent_types(spy).count(command) == 1
    assert [w for w in result["warnings"] if w.startswith(f"{warning} unavailable")]
