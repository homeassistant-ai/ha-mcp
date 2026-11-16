"""``ha_manage_radio``'s dispatcher and its Matter actions, and the Matter
diagnostics ``ha_get_device`` adds, on real Home Assistant Core.

Core's own ``matter`` integration answers every ``matter/*`` command, on the
Matter server client Core's own tests replace (``_matter_radio_core.py``): a
test reads back what the server was asked and gives the server's answers.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from matter_server.common.models import (
    CommissioningParameters,
    MatterSoftwareVersion,
    UpdateSource,
)

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_radio import HANDLERS, RadioTools
from ha_mcp.tools.tools_registry import RegistryTools

from ._client_spies import sent_messages
from ._command_faults import dropped_on, pooled_socket
from ._matter_radio_core import (
    HOME_ASSISTANT_FABRIC,
    NODE_ID,
    OTHER_CONTROLLER_FABRIC,
    matter_server_client,
    set_up_matter,
)
from ._tool_errors import error_body, error_of

OFFERED_UPDATE = MatterSoftwareVersion(
    vid=65521,
    pid=32768,
    software_version=2,
    software_version_string="2.0",
    firmware_information=None,
    min_applicable_software_version=0,
    max_applicable_software_version=1,
    release_notes_url=None,
    update_source=UpdateSource.MAIN_NET_DCL,
)


@pytest.fixture
def software_update() -> MatterSoftwareVersion | None:
    """The update the server offers the node; a test overrides it."""
    return None


@pytest.fixture
def matter_server(software_update: MatterSoftwareVersion | None) -> Iterator[MagicMock]:
    with matter_server_client() as client:
        client.check_node_update.return_value = software_update
        yield client


@pytest.fixture
async def config(hass: HomeAssistant) -> None:
    """Core's ``config`` commands: config entries and the registries."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
async def device_id(hass: HomeAssistant, config: None, matter_server: MagicMock) -> str:
    """The device Core's ``matter`` integration registers for the node."""
    return await set_up_matter(hass)


@pytest.fixture
def radio(core_client: HomeAssistantClient) -> RadioTools:
    return RadioTools(core_client)


async def test_matter_device_gets_its_node_diagnostics(
    core_client: HomeAssistantClient, device_id: str
) -> None:
    """``ha_get_device`` adds the node's diagnostics, its IP addresses under
    their correct name although the server spells the field ``ip_adresses``."""
    result = await RegistryTools(core_client).ha_get_device(device_id=device_id)

    device = result["device"]
    assert device["integration_type"] == "matter"
    diagnostics = device["node_diagnostics"]
    assert (diagnostics["network_type"], diagnostics["available"]) == ("thread", True)
    assert diagnostics["active_fabric_index"] == HOME_ASSISTANT_FABRIC
    assert diagnostics["ip_addresses"] == ["fe80::1"]


async def test_matter_device_whose_diagnostics_get_no_answer_still_comes_back(
    hass: HomeAssistant, core_client: HomeAssistantClient, device_id: str
) -> None:
    """The connection drops as the diagnostics are read: the device comes back
    without them, and a warning names the skipped read."""
    socket = await pooled_socket(core_client)

    with dropped_on(hass, "matter/node_diagnostics", socket):
        result = await RegistryTools(core_client).ha_get_device(device_id=device_id)

    assert result["success"] is True
    assert result["device"]["id"] == device_id
    assert "node_diagnostics" not in result["device"]
    assert [warning.split(":")[0] for warning in result["warnings"]] == [
        "Matter node diagnostics unavailable"
    ]


async def test_diagnostics_report_the_nodes_ip_addresses_under_their_name(
    radio: RadioTools, matter_server: MagicMock, device_id: str
) -> None:
    out = await radio.ha_manage_radio(
        radio="matter", action="diagnostics", device_id=device_id
    )

    assert out["diagnostics"]["network_type"] == "thread"
    assert out["diagnostics"]["ip_addresses"] == ["fe80::1"]
    assert "ip_adresses" not in out["diagnostics"]
    matter_server.node_diagnostics.assert_awaited_once_with(node_id=NODE_ID)


async def test_ping_returns_the_servers_reachability(
    radio: RadioTools, matter_server: MagicMock, device_id: str
) -> None:
    matter_server.ping_node.return_value = {"fe80::1": True}

    out = await radio.ha_manage_radio(
        radio="matter", action="ping", device_id=device_id
    )

    assert out["reachability"] == {"fe80::1": True}
    matter_server.ping_node.assert_awaited_once_with(node_id=NODE_ID)


async def test_unknown_action_lists_the_supported_ones(radio: RadioTools) -> None:
    with pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(radio="matter", action="nope", device_id="d1")

    assert error_body(exc)["supported"] == sorted(HANDLERS["matter"].SUPPORTED)


async def test_missing_required_params_are_all_named(radio: RadioTools) -> None:
    with pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(radio="matter", action="set_wifi_credentials")

    assert error_body(exc)["missing"] == ["network_name", "password"]


@pytest.mark.parametrize(
    ("radio_name", "action", "params"),
    [
        ("matter", "remove_fabric", None),
        ("zigbee", "remove_device", None),
        ("zigbee", "network_restore", {"backup": {"version": 1}}),
        ("zigbee", "change_channel", {"new_channel": 25}),
        ("zwave", "remove_device", None),
        ("zwave", "hard_reset", None),
    ],
)
async def test_a_destructive_action_without_confirm_sends_nothing(
    core_client: HomeAssistantClient,
    radio: RadioTools,
    radio_name: Any,
    action: str,
    params: dict[str, Any] | None,
) -> None:
    """Each action the ``confirm`` parameter names as destructive is refused
    without ``confirm=True``, before anything reaches Home Assistant."""
    with sent_messages(core_client) as spy, pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(
            radio=radio_name, action=action, device_id="d1", params=params
        )

    assert "confirm=True" in error_of(exc)["message"]
    assert spy.await_count == 0


async def test_remove_fabric_refuses_home_assistants_own_fabric(
    radio: RadioTools, matter_server: MagicMock, device_id: str
) -> None:
    """Without a fabric index, or with Home Assistant's own, the removal is
    refused and points to ``ha_remove_device``; the node keeps every fabric."""
    for params in (None, {"fabric_index": HOME_ASSISTANT_FABRIC}):
        with pytest.raises(ToolError) as exc:
            await radio.ha_manage_radio(
                radio="matter",
                action="remove_fabric",
                device_id=device_id,
                params=params,
                confirm=True,
            )
        assert "ha_remove_device" in error_of(exc)["message"]

    matter_server.remove_matter_fabric.assert_not_awaited()


async def test_remove_fabric_removes_another_controllers_fabric(
    radio: RadioTools, matter_server: MagicMock, device_id: str
) -> None:
    out = await radio.ha_manage_radio(
        radio="matter",
        action="remove_fabric",
        device_id=device_id,
        params={"fabric_index": OTHER_CONTROLLER_FABRIC},
        confirm=True,
    )

    assert out["fabric_index"] == OTHER_CONTROLLER_FABRIC
    matter_server.remove_matter_fabric.assert_awaited_once_with(
        node_id=NODE_ID, fabric_index=OTHER_CONTROLLER_FABRIC
    )


async def test_share_out_returns_the_pairing_codes(
    radio: RadioTools, matter_server: MagicMock, device_id: str
) -> None:
    matter_server.open_commissioning_window.return_value = CommissioningParameters(
        setup_pin_code=20202021,
        setup_manual_code="3497-011-2332",
        setup_qr_code="MT:Y3",
    )

    out = await radio.ha_manage_radio(
        radio="matter", action="share_out", device_id=device_id
    )

    codes = out["commissioning"]
    assert (codes["setup_manual_code"], codes["setup_qr_code"]) == (
        "3497-011-2332",
        "MT:Y3",
    )
    matter_server.open_commissioning_window.assert_awaited_once_with(node_id=NODE_ID)


async def test_an_entity_id_targets_its_device(
    core_client: HomeAssistantClient,
    radio: RadioTools,
    matter_server: MagicMock,
    entity_registry: er.EntityRegistry,
    device_id: str,
) -> None:
    """An entity_id is resolved to its device with Core's single-entity read,
    not a dump of the whole registry, and the node is read."""
    entity_id = er.async_entries_for_device(entity_registry, device_id)[0].entity_id

    with sent_messages(core_client) as spy:
        out = await radio.ha_manage_radio(
            radio="matter", action="diagnostics", entity_id=entity_id
        )

    assert out["diagnostics"]["node_id"] == NODE_ID
    sent = [call.args[0] for call in spy.call_args_list]
    assert {"type": "config/entity_registry/get", "entity_id": entity_id} in sent
    assert {"type": "matter/node_diagnostics", "device_id": device_id} in sent
    assert not [msg for msg in sent if msg["type"] == "config/entity_registry/list"]


@pytest.mark.parametrize("software_update", [OFFERED_UPDATE])
async def test_firmware_update_installs_the_offered_version(
    radio: RadioTools, matter_server: MagicMock, device_id: str
) -> None:
    out = await radio.ha_manage_radio(
        radio="matter", action="firmware_update", device_id=device_id
    )

    assert out["long_running"] is True
    assert out["entity_id"].startswith("update.")
    matter_server.update_node.assert_awaited_once_with(
        node_id=NODE_ID, software_version=OFFERED_UPDATE.software_version
    )


async def test_commission_commissions_over_bluetooth_by_default(
    radio: RadioTools, matter_server: MagicMock, device_id: str
) -> None:
    """A commission with only a code asks the server for a full commissioning,
    not the network-only one Core would otherwise default to."""
    out = await radio.ha_manage_radio(
        radio="matter", action="commission", params={"code": "MT:Y3.13OTB00KA0648G00"}
    )

    assert (out["action"], out["long_running"]) == ("commission", True)
    matter_server.commission_with_code.assert_awaited_once_with(
        "MT:Y3.13OTB00KA0648G00", network_only=False
    )


async def test_commission_on_network_sends_the_pin(
    radio: RadioTools, matter_server: MagicMock, device_id: str
) -> None:
    out = await radio.ha_manage_radio(
        radio="matter", action="commission_on_network", params={"pin": 20202021}
    )

    assert (out["action"], out["long_running"]) == ("commission_on_network", True)
    matter_server.commission_on_network.assert_awaited_once_with(20202021, ip_addr=None)


async def test_interview_reinterviews_the_node(
    radio: RadioTools, matter_server: MagicMock, device_id: str
) -> None:
    out = await radio.ha_manage_radio(
        radio="matter", action="interview", device_id=device_id
    )

    assert (out["action"], out["long_running"]) == ("interview", True)
    matter_server.interview_node.assert_awaited_once_with(node_id=NODE_ID)


async def test_set_thread_hands_the_dataset_to_the_server(
    radio: RadioTools, matter_server: MagicMock, device_id: str
) -> None:
    await radio.ha_manage_radio(
        radio="matter",
        action="set_thread",
        params={"thread_operation_dataset": "0e080000000000010000"},
    )

    matter_server.set_thread_operational_dataset.assert_awaited_once_with(
        "0e080000000000010000"
    )


async def test_set_wifi_credentials_hands_both_to_the_server(
    radio: RadioTools, matter_server: MagicMock, device_id: str
) -> None:
    await radio.ha_manage_radio(
        radio="matter",
        action="set_wifi_credentials",
        params={"network_name": "Home", "password": "secret"},
    )

    matter_server.set_wifi_credentials.assert_awaited_once_with(
        ssid="Home", credentials="secret"
    )


async def test_network_status_names_the_matter_entry(
    hass: HomeAssistant, radio: RadioTools, device_id: str
) -> None:
    out = await radio.ha_manage_radio(radio="matter", action="network_status")

    [entry] = hass.config_entries.async_entries("matter")
    assert out["config_entry_id"] == entry.entry_id
    assert "diagnostics" in out["note"]


async def test_network_status_without_matter_reports_it_unavailable(
    radio: RadioTools, config: None
) -> None:
    out = await radio.ha_manage_radio(radio="matter", action="network_status")

    assert out["available"] is False
    assert out["warnings"]


async def test_a_refused_admin_command_asks_for_an_administrator(
    read_only_client: HomeAssistantClient, device_id: str
) -> None:
    with pytest.raises(ToolError) as exc:
        await RadioTools(read_only_client).ha_manage_radio(
            radio="matter", action="share_out", device_id=device_id
        )

    body = error_body(exc)
    assert body["error"]["code"] == "AUTH_INSUFFICIENT_PERMISSIONS"
    assert body["ha_error_code"] == "unauthorized"
    assert body["ws_type"] == "matter/open_commissioning_window"
    assert "administrator" in body["error"]["suggestion"]


async def test_a_command_of_an_integration_not_set_up_says_it_is_unavailable(
    radio: RadioTools, config: None
) -> None:
    with pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(
            radio="matter", action="diagnostics", device_id="d1"
        )

    body = error_body(exc)
    assert body["ha_error_code"] == "unknown_command"
    assert body["error"]["message"].startswith(
        "The matter integration or command is unavailable"
    )
    assert "supports" in body["error"]["suggestion"]


async def test_another_refusal_carries_cores_reason(
    radio: RadioTools, device_id: str
) -> None:
    """A refusal without a known next step fails with Core's own reason."""
    with pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(
            radio="matter", action="diagnostics", device_id="no-such-device"
        )

    body = error_body(exc)
    assert body["error"]["code"] == "SERVICE_CALL_FAILED"
    assert body["ha_error_code"] == "node_not_found"
    assert "no-such-device" in body["error"]["message"]


async def test_every_supported_action_reaches_its_handler(
    radio: RadioTools, config: None
) -> None:
    """Every action a radio lists reaches a branch of its handler, never the
    "unhandled" fall-through, given every parameter any action requires. Core
    has none of the radios set up, so most answers are its refusals."""
    params = {
        name: 1
        for handler in HANDLERS.values()
        for spec in handler.SUPPORTED.values()
        for name in spec.required
    }
    for radio_name, handler in HANDLERS.items():
        for action in handler.SUPPORTED:
            try:
                await radio.ha_manage_radio(
                    radio=radio_name,
                    action=action,
                    device_id="d1",
                    params=dict(params),
                    confirm=True,
                )
            except ToolError as exc:
                # A handler that raised something of its own is a bug, not a refusal.
                code = json.loads(str(exc))["error"]["code"]
                assert code != "INTERNAL_ERROR", f"{radio_name}/{action}: {exc}"
                assert "unhandled" not in str(exc), f"{radio_name}/{action}"
                assert "requires:" not in str(exc), f"{radio_name}/{action}"


async def test_an_entity_core_does_not_know_is_not_found(
    core_client: HomeAssistantClient, radio: RadioTools, config: None
) -> None:
    with sent_messages(core_client) as spy, pytest.raises(ToolError) as exc:
        await radio.ha_manage_radio(
            radio="matter", action="diagnostics", entity_id="light.ghost"
        )

    error = error_of(exc)
    assert error["code"] == "ENTITY_NOT_FOUND"
    assert "light.ghost" in error["message"]
    assert spy.call_args_list[0].args[0] == {
        "type": "config/entity_registry/get",
        "entity_id": "light.ghost",
    }


async def test_an_unreachable_home_assistant_is_not_a_missing_entity(
    unreachable_client: HomeAssistantClient,
) -> None:
    """When Home Assistant cannot be reached the entity lookup fails as a
    connection failure, never as an entity that does not exist (#1832)."""
    with pytest.raises(ToolError) as exc:
        await RadioTools(unreachable_client).ha_manage_radio(
            radio="matter", action="diagnostics", entity_id="light.real"
        )

    assert error_of(exc)["code"] == "CONNECTION_FAILED"
