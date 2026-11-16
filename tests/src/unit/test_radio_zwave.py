"""``ha_manage_radio``'s Z-Wave actions on real Home Assistant Core.

Core's own ``zwave_js`` integration answers every ``zwave_js/*`` command and
runs the ``zwave_js.ping`` service. Only the Z-Wave JS server below
zwave-js-server-python is replaced (``_zwave_js``): its driver, controller and
nodes are the library's real models built from the state the server sends, and
``SERVER_ANSWERS`` is what the server answers each command the library sends.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from zwave_js_server.const import (
    CommandClass,
    InclusionState,
    InclusionStrategy,
    NodeStatus,
    QRCodeVersion,
    SecurityClass,
)
from zwave_js_server.model.driver import Driver
from zwave_js_server.model.version import VersionInfo

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_radio import RadioTools

from ._command_faults import received
from ._tool_errors import error_body, error_of
from ._zwave_js import (
    HOME_ID,
    NODE_ID,
    node_state,
    set_up_zwave_js,
    zwave_js_server_connection,
)

# A SmartStart QR code's text, as a scanner reads it.
QR_CODE = "90" + "0" * 50 + "1" * 40
CONTROLLER_STATE = {
    "homeId": HOME_ID,
    "ownNodeId": 1,
    "inclusionState": InclusionState.IDLE,
    "status": 0,
}
SERVER_ANSWERS: dict[str, Any] = {
    "controller.get_state": {"state": CONTROLLER_STATE},
    "controller.begin_inclusion": {"success": True},
    "controller.begin_exclusion": {"success": True},
    "controller.rebuild_node_routes": {"success": True},
    "controller.begin_rebuilding_routes": {"success": True},
    "node.ping": {"responded": True},
    "node.set_value": {"result": {"status": 255}},
}
RAMP_RATE = 5


def ramp_rate_parameter() -> dict[str, Any]:
    """Configuration parameter 5 as the Z-Wave JS server reports it."""
    return {
        "commandClass": CommandClass.CONFIGURATION,
        "commandClassName": "Configuration",
        "endpoint": 0,
        "property": RAMP_RATE,
        "propertyName": "Ramp rate",
        "ccVersion": 1,
        "metadata": {
            "type": "number",
            "readable": True,
            "writeable": True,
            "label": "Ramp rate",
            "min": 0,
            "max": 99,
            "valueSize": 1,
            "format": 0,
            "allowManualEntry": True,
            "isFromConfig": True,
        },
        "value": 8,
    }


@pytest.fixture
def nodes() -> list[dict[str, Any]]:
    """The network's nodes; a test overrides it by parametrizing."""
    return [node_state([ramp_rate_parameter()])]


@pytest.fixture
def server(nodes: list[dict[str, Any]]) -> Iterator[MagicMock]:
    """The Z-Wave JS server, holding ``nodes`` and answering ``SERVER_ANSWERS``."""
    with zwave_js_server_connection(nodes[0]) as client:
        client.driver = Driver(
            client,
            {"controller": CONTROLLER_STATE, "nodes": nodes},
            {"enabled": True, "level": "info", "logToFile": False},
        )
        client.version = VersionInfo(
            driver_version="15.0.0",
            server_version="3.0.0",
            home_id=HOME_ID,
            min_schema_version=0,
            max_schema_version=44,
        )
        client.ws_server_url = "ws://test.org"
        client.server_logging_enabled = False

        async def answer(message: dict[str, Any], *_: Any, **__: Any) -> Any:
            return SERVER_ANSWERS.get(message["command"], {})

        client.async_send_command.side_effect = answer
        yield client


@pytest.fixture
async def entry(hass: HomeAssistant, server: MagicMock) -> MockConfigEntry:
    assert await async_setup_component(hass, "config", {})
    zwave_entry = await set_up_zwave_js(hass)
    server.async_send_command.reset_mock()
    return zwave_entry


@pytest.fixture
def device_id(hass: HomeAssistant, entry: MockConfigEntry) -> str:
    device = dr.async_get(hass).async_get_device_by_identifier(
        ("zwave_js", f"{HOME_ID}-{NODE_ID}"), entry.entry_id
    )
    assert device is not None
    return device.id


@pytest.fixture
async def without_zwave(hass: HomeAssistant) -> None:
    """Core with its ``config`` commands and no Z-Wave JS entry."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
def radio(core_client: HomeAssistantClient) -> RadioTools:
    return RadioTools(core_client)


def sent_to_server(server: MagicMock) -> list[dict[str, Any]]:
    return [call.args[0] for call in server.async_send_command.call_args_list]


async def test_diagnostics_reports_the_nodes_status(
    radio: RadioTools, device_id: str
) -> None:
    out = await radio.ha_manage_radio(
        radio="zwave", action="diagnostics", device_id=device_id
    )

    assert out["node_status"] == {
        "node_id": NODE_ID,
        "status": NodeStatus.ALIVE,
        "is_routing": True,
        "is_secure": True,
        "highest_security_class": SecurityClass.S2_AUTHENTICATED,
        "zwave_plus_version": 2,
        "is_controller_node": False,
    }


async def test_diagnostics_of_a_device_core_does_not_know_is_not_found(
    radio: RadioTools, entry: MockConfigEntry
) -> None:
    """Core's not-found verdict comes back as such, with the step to find the
    device's id."""
    with pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(
            radio="zwave", action="diagnostics", device_id="no-such-device"
        )

    body = error_body(exc)
    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert body["ha_error_code"] == "not_found"
    assert (body["ws_type"], body["device_id"]) == (
        "zwave_js/node_status",
        "no-such-device",
    )
    assert "ha_get_device" in body["error"]["suggestion"]


async def test_network_status_summarizes_the_controller_and_nodes(
    radio: RadioTools, entry: MockConfigEntry
) -> None:
    out = await radio.ha_manage_radio(radio="zwave", action="network_status")

    assert out["config_entry_id"] == entry.entry_id
    assert out["controller"]["home_id"] == HOME_ID
    assert "nodes" not in out["controller"]
    assert [node["node_id"] for node in out["nodes"]] == [NODE_ID]
    assert (out["count"], out["total_count"]) == (1, 1)
    assert "truncated" not in out


@pytest.mark.parametrize(
    "nodes",
    [[{**node_state([]), "nodeId": node_id} for node_id in range(2, 53)]],
)
async def test_network_status_caps_a_large_network_and_says_so(
    radio: RadioTools, entry: MockConfigEntry
) -> None:
    out = await radio.ha_manage_radio(radio="zwave", action="network_status")

    assert (out["count"], out["total_count"], out["truncated"]) == (50, 51, True)


async def test_network_status_of_an_unloaded_entry_says_so(
    hass: HomeAssistant, radio: RadioTools, entry: MockConfigEntry
) -> None:
    """A Z-Wave JS entry that is not loaded gets Core's not-loaded verdict and
    the step to load the integration."""
    assert await hass.config_entries.async_unload(entry.entry_id)

    with pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(radio="zwave", action="network_status")

    body = error_body(exc)
    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    assert body["ha_error_code"] == "not_loaded"
    assert body["error"]["message"].startswith("The zwave_js integration is not loaded")
    assert "integration" in body["error"]["suggestion"]


async def test_network_status_without_zwave_reports_it_unavailable(
    radio: RadioTools, without_zwave: None
) -> None:
    out = await radio.ha_manage_radio(radio="zwave", action="network_status")

    assert out["success"] is True
    assert out["available"] is False
    assert out["warnings"]


async def test_ping_pings_the_node(
    radio: RadioTools, server: MagicMock, device_id: str
) -> None:
    out = await radio.ha_manage_radio(radio="zwave", action="ping", device_id=device_id)

    assert out["success"] is True
    assert {"command": "node.ping", "nodeId": NODE_ID} in sent_to_server(server)


async def test_add_without_a_credential_names_the_accepted_ones(
    radio: RadioTools, entry: MockConfigEntry
) -> None:
    with pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(radio="zwave", action="add")

    assert "qr_provisioning_information" in error_of(exc)["message"]


async def test_add_with_a_qr_code_starts_secure_inclusion(
    radio: RadioTools, server: MagicMock, entry: MockConfigEntry
) -> None:
    out = await radio.ha_manage_radio(
        radio="zwave",
        action="add",
        params={
            "qr_code_string": QR_CODE,
            "inclusion_strategy": InclusionStrategy.SECURITY_S2,
        },
    )

    assert (out["mode"], out["result"]) == ("add_node", True)
    inclusion = {
        "command": "controller.begin_inclusion",
        "options": {"strategy": InclusionStrategy.SECURITY_S2, "provisioning": QR_CODE},
    }
    assert inclusion in sent_to_server(server)


async def test_add_with_only_a_qr_code_starts_inclusion(
    radio: RadioTools, server: MagicMock, entry: MockConfigEntry
) -> None:
    """The QR code alone is enough: the action asks for nothing more, and
    Z-Wave JS includes a device from its QR code only with S2 security."""
    out = await radio.ha_manage_radio(
        radio="zwave", action="add", params={"qr_code_string": QR_CODE}
    )

    assert out["mode"] == "add_node"
    assert any(
        message["command"] == "controller.begin_inclusion"
        for message in sent_to_server(server)
    )


async def test_add_with_smart_start_needs_the_provisioning_information(
    radio: RadioTools, entry: MockConfigEntry
) -> None:
    with pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(
            radio="zwave", action="add", params={"smart_start": True}
        )

    assert "qr_provisioning_information" in error_of(exc)["message"]


async def test_add_with_smart_start_provisions_the_node(
    radio: RadioTools, server: MagicMock, entry: MockConfigEntry
) -> None:
    dsk = "11111-22222-33333-44444-55555-66666-77777-88888"
    provisioning = {
        "version": QRCodeVersion.SMART_START,
        "securityClasses": [SecurityClass.S2_AUTHENTICATED],
        "dsk": dsk,
        "genericDeviceClass": 1,
        "specificDeviceClass": 1,
        "installerIconType": 1,
        "manufacturerId": 1,
        "productType": 1,
        "productId": 1,
        "applicationVersion": "1.0",
    }

    out = await radio.ha_manage_radio(
        radio="zwave",
        action="add",
        params={"smart_start": True, "qr_provisioning_information": provisioning},
    )

    assert out["mode"] == "smart_start"
    provisioned = [
        message["entry"]["dsk"]
        for message in sent_to_server(server)
        if message["command"] == "controller.provision_smart_start_node"
    ]
    assert provisioned == [dsk]


async def test_add_with_two_credentials_refuses_before_reaching_core(
    hass: HomeAssistant, radio: RadioTools, entry: MockConfigEntry
) -> None:
    """Z-Wave JS takes one inclusion credential; two are refused here, and Core
    is never asked to include."""
    with received(hass, "zwave_js/add_node") as core, pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(
            radio="zwave",
            action="add",
            params={"qr_code_string": QR_CODE, "dsk": "11111-22222"},
        )

    message = error_of(exc)["message"]
    assert "qr_code_string" in message
    assert "dsk" in message
    assert "mutually exclusive" in message.lower()
    assert core["zwave_js/add_node"] == []


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("add", {"qr_code_string": QR_CODE}),
        ("remove_device", None),
        ("rebuild_routes", {"scope": "network"}),
        ("hard_reset", None),
    ],
)
async def test_a_controller_action_without_zwave_fails(
    radio: RadioTools,
    without_zwave: None,
    action: str,
    params: dict[str, Any] | None,
) -> None:
    """A controller action on a Home Assistant without Z-Wave JS fails instead of
    reporting a no-op as done."""
    with pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(
            radio="zwave", action=action, params=params, confirm=True
        )

    assert "zwave_js integration is not configured" in error_of(exc)["message"]


async def test_remove_device_of_a_failed_node_removes_it(
    radio: RadioTools, server: MagicMock, device_id: str
) -> None:
    out = await radio.ha_manage_radio(
        radio="zwave",
        action="remove_device",
        device_id=device_id,
        params={"failed": True},
        confirm=True,
    )

    assert out["mode"] == "failed"
    assert {"command": "controller.remove_failed_node", "nodeId": NODE_ID} in (
        sent_to_server(server)
    )


async def test_remove_device_of_a_failed_node_needs_the_device(
    radio: RadioTools, entry: MockConfigEntry
) -> None:
    with pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(
            radio="zwave",
            action="remove_device",
            params={"failed": True},
            confirm=True,
        )

    assert "device_id" in error_of(exc)["message"]


async def test_remove_device_opens_exclusion_on_the_controller(
    hass: HomeAssistant,
    radio: RadioTools,
    server: MagicMock,
    entry: MockConfigEntry,
) -> None:
    with received(hass, "zwave_js/remove_node") as core:
        out = await radio.ha_manage_radio(
            radio="zwave", action="remove_device", confirm=True
        )

    assert (out["mode"], out["result"]) == ("exclusion", True)
    assert [msg["entry_id"] for msg in core["zwave_js/remove_node"]] == [entry.entry_id]
    assert any(
        message["command"] == "controller.begin_exclusion"
        for message in sent_to_server(server)
    )


async def test_reinterview_reinterviews_the_node(
    radio: RadioTools, server: MagicMock, device_id: str
) -> None:
    out = await radio.ha_manage_radio(
        radio="zwave", action="reinterview", device_id=device_id
    )

    assert out["success"] is True
    reinterviews = [
        call.args[0] for call in server.async_send_command_no_wait.call_args_list
    ]
    assert {"command": "node.refresh_info", "nodeId": NODE_ID} in reinterviews


async def test_rebuild_routes_rebuilds_the_nodes_routes(
    radio: RadioTools, server: MagicMock, device_id: str
) -> None:
    out = await radio.ha_manage_radio(
        radio="zwave", action="rebuild_routes", device_id=device_id
    )

    assert (out["scope"], out["result"]) == ("node", True)
    assert {"command": "controller.rebuild_node_routes", "nodeId": NODE_ID} in (
        sent_to_server(server)
    )


async def test_rebuild_routes_of_the_network_starts_on_the_controller(
    hass: HomeAssistant,
    radio: RadioTools,
    server: MagicMock,
    entry: MockConfigEntry,
) -> None:
    with received(hass, "zwave_js/begin_rebuilding_routes") as core:
        out = await radio.ha_manage_radio(
            radio="zwave", action="rebuild_routes", params={"scope": "network"}
        )

    assert (out["scope"], out["result"]) == ("network", True)
    assert [msg["entry_id"] for msg in core["zwave_js/begin_rebuilding_routes"]] == [
        entry.entry_id
    ]
    assert {"command": "controller.begin_rebuilding_routes"} in sent_to_server(server)


async def test_rebuild_routes_of_a_node_needs_the_device(
    radio: RadioTools, entry: MockConfigEntry
) -> None:
    with pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(radio="zwave", action="rebuild_routes")

    assert "device_id" in error_of(exc)["message"]


async def test_set_config_param_writes_a_zero(
    radio: RadioTools, server: MagicMock, device_id: str
) -> None:
    """A value of 0 is written to the node's parameter, on the root endpoint."""
    out = await radio.ha_manage_radio(
        radio="zwave",
        action="set_config_param",
        device_id=device_id,
        params={"property": RAMP_RATE, "value": 0},
    )

    assert out["success"] is True
    writes = [
        (
            message["valueId"]["property"],
            message["valueId"]["endpoint"],
            message["value"],
        )
        for message in sent_to_server(server)
        if message["command"] == "node.set_value"
    ]
    assert writes == [(RAMP_RATE, 0, 0)]


async def test_firmware_update_of_a_node_without_an_update_entity_says_none(
    radio: RadioTools, device_id: str
) -> None:
    """A node Core offers no firmware update entity for gets the not-found
    answer, not a failed install."""
    with pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(
            radio="zwave", action="firmware_update", device_id=device_id
        )

    error = error_of(exc)
    assert error["code"] == "ENTITY_NOT_FOUND"
    assert "no firmware update is available" in error["message"]


async def test_hard_reset_resets_the_controller(
    radio: RadioTools, server: MagicMock, entry: MockConfigEntry
) -> None:
    """The controller is reset; Core answers once the reset controller is back,
    which the server announces with ``driver ready``."""

    async def answer(message: dict[str, Any], *_: Any, **__: Any) -> Any:
        if message["command"] == "driver.hard_reset":
            server.driver.emit(
                "driver ready", {"event": "driver ready", "source": "driver"}
            )
        return SERVER_ANSWERS.get(message["command"], {})

    server.async_send_command.side_effect = answer

    out = await radio.ha_manage_radio(radio="zwave", action="hard_reset", confirm=True)

    assert out["success"] is True
    assert {"command": "driver.hard_reset"} in sent_to_server(server)
