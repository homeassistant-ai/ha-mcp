"""Z-Wave parameter reads work without Home Assistant entities.

The server's client talks to a real ``hass``. Z-Wave JS needs a Z-Wave server,
so it is a mock integration whose two parameter commands are registered on
Core's own WebSocket API with the schemas Core's ``zwave_js`` gives them, and
answer in the shape Core's handlers build. The device and entity registries,
``config/entity_registry/get``, Core's error replies and the socket are Core's.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, MockModule, mock_integration

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.tools.tools_radio import RadioTools

from ._client_spies import sent_messages

_DOMAIN = "zwave_js"


def parameter(
    value: Any, *, endpoint: int = 0, mask: int | None = None
) -> dict[str, Any]:
    """One parameter as Core's ``zwave_js/get_config_parameters`` reports it."""
    return {
        "property": 3,
        "property_key": mask,
        "endpoint": endpoint,
        "configuration_value_type": "manual_entry" if mask is None else "enumerated",
        "metadata": {
            "description": None,
            "label": "Ramp rate",
            "type": "number",
            "min": 0,
            "max": 99,
            "unit": None,
            "writeable": True,
            "readable": True,
            "default": 0,
            "states": {"0": "Immediate"},
        },
        "value": value,
    }


RawRead = Callable[[websocket_api.ActiveConnection, dict[str, Any]], Any]


@dataclass
class ZwaveNetwork:
    """What the Z-Wave server knows about the one node of the test."""

    device_id: str
    parameters: dict[str, Any] = field(default_factory=dict)
    raw_read: RawRead | None = None


def _register_commands(hass: HomeAssistant, network: ZwaveNetwork) -> None:
    """Register the integration's two parameter commands on Core."""

    def node_found(connection: websocket_api.ActiveConnection, msg: dict) -> bool:
        if msg["device_id"] == network.device_id:
            return True
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Node not found")
        return False

    @websocket_api.require_admin
    @websocket_api.websocket_command(
        {
            probatio.Required("type"): f"{_DOMAIN}/get_config_parameters",
            probatio.Required("device_id"): str,
        }
    )
    @websocket_api.async_response
    async def get_config_parameters(
        hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        if node_found(connection, msg):
            connection.send_result(msg["id"], network.parameters)

    @websocket_api.require_admin
    @websocket_api.websocket_command(
        {
            probatio.Required("type"): f"{_DOMAIN}/get_raw_config_parameter",
            probatio.Required("device_id"): str,
            probatio.Required("property"): int,
        }
    )
    @websocket_api.async_response
    async def get_raw_config_parameter(
        hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
    ) -> None:
        if not node_found(connection, msg):
            return
        assert network.raw_read is not None, "the node was not asked to answer"
        value = await network.raw_read(connection, msg)
        if value is not None:
            connection.send_result(msg["id"], {"value": value})

    websocket_api.async_register_command(hass, get_config_parameters)
    websocket_api.async_register_command(hass, get_raw_config_parameter)


def answers(value: Any) -> RawRead:
    async def read(connection: websocket_api.ActiveConnection, msg: dict) -> Any:
        return value

    return read


@pytest.fixture
async def network(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    device_registry: dr.DeviceRegistry,
) -> ZwaveNetwork:
    """A Z-Wave JS entry with one node device and no entities."""
    assert await async_setup_component(hass, "config", {})
    mock_integration(hass, MockModule(_DOMAIN))
    entry = MockConfigEntry(domain=_DOMAIN)
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(_DOMAIN, "3245146787-5")}
    )
    zwave = ZwaveNetwork(device_id=device.id)
    _register_commands(hass, zwave)
    return zwave


def sent_types(spy: AsyncMock) -> list[str]:
    return [call.args[0]["type"] for call in spy.call_args_list]


async def read_param(
    client: HomeAssistantClient, network: ZwaveNetwork, **params: Any
) -> dict[str, Any]:
    return await RadioTools(client).ha_manage_radio(
        radio="zwave",
        action="get_config_param",
        device_id=network.device_id,
        params={"property": 3, **params},
    )


async def failed_read(
    client: HomeAssistantClient, network: ZwaveNetwork, **params: Any
) -> dict[str, Any]:
    with pytest.raises(ToolError) as exc:
        await read_param(client, network, **params)
    body: dict[str, Any] = json.loads(str(exc.value))
    return body


async def test_parameters_need_no_config_entities_and_preserve_metadata(
    core_client: HomeAssistantClient, network: ZwaveNetwork
) -> None:
    network.parameters = {
        "root": parameter(0),
        "endpoint": parameter(5, endpoint=1),
    }

    with (
        sent_messages(core_client) as spy,
        patch.object(
            core_client, "call_service", wraps=core_client.call_service
        ) as service,
    ):
        result = await RadioTools(core_client).ha_manage_radio(
            radio="zwave", action="get_config_params", device_id=network.device_id
        )

    assert result["parameters"] == network.parameters
    assert result["source"] == "cache"
    assert result["refresh_requested"] is False
    assert sent_types(spy) == [f"{_DOMAIN}/get_config_parameters"]
    service.assert_not_called()


async def test_cached_read_selects_endpoint_and_bit_mask_without_polling(
    core_client: HomeAssistantClient, network: ZwaveNetwork
) -> None:
    network.parameters = {
        "root": parameter(0),
        "other": parameter(2, endpoint=1, mask=15),
        "selected": parameter(3, endpoint=1, mask=240),
    }

    with sent_messages(core_client) as spy:
        result = await read_param(core_client, network, endpoint=1, property_key=240)

    assert result["parameter"]["value"] == 3
    assert result["parameter"]["property_key"] == 240
    assert result["source"] == "cache"
    assert spy.await_count == 1


async def test_disabled_entity_is_only_an_optional_device_resolver(
    core_client: HomeAssistantClient,
    network: ZwaveNetwork,
    entity_registry: er.EntityRegistry,
) -> None:
    network.parameters = {"root": parameter(0)}
    entity = entity_registry.async_get_or_create(
        "number",
        _DOMAIN,
        "3245146787-5-112-0-3",
        device_id=network.device_id,
        suggested_object_id="example_disabled",
        disabled_by=er.RegistryEntryDisabler.USER,
    )

    with sent_messages(core_client) as spy:
        result = await RadioTools(core_client).ha_manage_radio(
            radio="zwave",
            action="get_config_param",
            entity_id=entity.entity_id,
            params={"property": 3},
        )

    assert result["parameter"]["value"] == 0
    assert result["device_id"] == network.device_id
    assert "warnings" not in result
    assert sent_types(spy) == [
        "config/entity_registry/get",
        f"{_DOMAIN}/get_config_parameters",
    ]


async def test_refresh_reads_raw_value_without_substituting_cached_partial(
    core_client: HomeAssistantClient, network: ZwaveNetwork
) -> None:
    network.parameters = {"partial": parameter(2, mask=15)}
    network.raw_read = answers(12)

    with (
        sent_messages(core_client) as spy,
        patch.object(
            core_client, "call_service", wraps=core_client.call_service
        ) as service,
    ):
        result = await read_param(core_client, network, refresh=True)

    assert result["parameter"]["value"] == 12
    assert result["parameter"]["property_key"] is None
    assert result["parameter"]["metadata"] is None
    assert "metadata_source" not in result
    assert result["source"] == "device_request"
    assert result["refresh_requested"] is True
    assert [call.args[0] for call in spy.call_args_list] == [
        {"type": f"{_DOMAIN}/get_config_parameters", "device_id": network.device_id},
        {
            "type": f"{_DOMAIN}/get_raw_config_parameter",
            "device_id": network.device_id,
            "property": 3,
        },
    ]
    service.assert_not_called()


@pytest.mark.parametrize(
    "params",
    [
        {"property": True},
        {"property": -1},
        {"property": 65536},
        {"property": 3, "endpoint": "1"},
        {"property": 3, "property_key": 0},
        {"property": 3, "refresh": "false"},
        {"property": 3, "refresh": True, "endpoint": 1},
        {"property": 3, "refresh": True, "property_key": 15},
    ],
)
async def test_invalid_identifiers_and_unsupported_refresh_never_send_commands(
    core_client: HomeAssistantClient, network: ZwaveNetwork, params: dict[str, Any]
) -> None:
    with sent_messages(core_client) as spy, pytest.raises(ToolError):
        await RadioTools(core_client).ha_manage_radio(
            radio="zwave",
            action="get_config_param",
            device_id=network.device_id,
            params=params,
        )
    spy.assert_not_called()


@pytest.mark.parametrize(
    "parameters",
    [
        pytest.param({}, id="absent"),
        pytest.param({"partial": parameter(2, mask=15)}, id="only-a-partial"),
    ],
)
async def test_missing_cached_parameter_does_not_implicitly_poll(
    core_client: HomeAssistantClient,
    network: ZwaveNetwork,
    parameters: dict[str, Any],
) -> None:
    """A partial parameter is not mistaken for the full one asked for."""
    network.parameters = parameters

    with sent_messages(core_client) as spy, pytest.raises(ToolError, match="not found"):
        await read_param(core_client, network)

    assert spy.await_count == 1


async def test_ha_failure_never_substitutes_a_cached_value(
    core_client: HomeAssistantClient, network: ZwaveNetwork
) -> None:
    network.parameters = {"root": parameter(8)}

    async def node_gone(connection: websocket_api.ActiveConnection, msg: dict) -> Any:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Node not found")

    network.raw_read = node_gone

    body = await failed_read(core_client, network, refresh=True)

    assert body["ha_error_code"] == websocket_api.ERR_NOT_FOUND
    assert "parameter" not in body


async def test_unknown_raw_failure_keeps_details_without_claiming_a_cause(
    core_client: HomeAssistantClient, network: ZwaveNetwork
) -> None:
    """Core answers unknown_error for empty results and unrelated handler failures."""
    network.parameters = {"root": parameter(8)}

    async def driver_asserts(
        connection: websocket_api.ActiveConnection, msg: dict
    ) -> Any:
        raise AssertionError("no value in the driver's reply")

    network.raw_read = driver_asserts

    with sent_messages(core_client) as spy:
        body = await failed_read(core_client, network, refresh=True)

    assert body["ha_error_code"] == websocket_api.ERR_UNKNOWN_ERROR
    assert body["device_id"] == network.device_id
    assert body["property"] == 3
    assert "no cached fallback" in body["error"]["message"]
    assert "returned no value" not in body["error"]["message"]
    assert "Unknown error" in body["error"]["details"]
    assert "parameter" in body["error"]["suggestion"].lower()
    assert spy.await_count == 2


async def test_unknown_cached_value_remains_unknown(
    core_client: HomeAssistantClient, network: ZwaveNetwork
) -> None:
    network.parameters = {"root": parameter(None)}

    result = await read_param(core_client, network)

    assert result["parameter"]["value"] is None
    assert result["warnings"]


async def test_list_cannot_silently_accept_a_refresh_request(
    core_client: HomeAssistantClient, network: ZwaveNetwork
) -> None:
    with (
        sent_messages(core_client) as spy,
        pytest.raises(ToolError, match="cache-only"),
    ):
        await RadioTools(core_client).ha_manage_radio(
            radio="zwave",
            action="get_config_params",
            device_id=network.device_id,
            params={"refresh": True},
        )
    spy.assert_not_called()


async def test_refresh_preserves_zero_and_marks_cached_metadata(
    core_client: HomeAssistantClient, network: ZwaveNetwork
) -> None:
    network.parameters = {"root": parameter(8)}
    network.raw_read = answers(0)

    result = await read_param(
        core_client, network, endpoint=0, property_key=None, refresh=True
    )

    assert result["parameter"]["value"] == 0
    assert result["parameter"]["metadata"] == parameter(8)["metadata"]
    assert result["metadata_source"] == "cache"


@pytest.fixture
async def socket(core_client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    """The pooled socket the client's WebSocket bridge sends on."""
    return await websocket_manager.get_client(
        url=core_client.base_url, token=core_client.token, verify_ssl=False
    )


@pytest.fixture
async def asleep(network: ZwaveNetwork) -> AsyncIterator[None]:
    """The node is asleep: the raw read waits until the test ends."""
    woke = asyncio.Event()

    async def queued(connection: websocket_api.ActiveConnection, msg: dict) -> Any:
        await woke.wait()

    network.raw_read = queued
    try:
        yield
    finally:
        woke.set()


async def test_refresh_timeout_explains_the_possible_wakeup_queue(
    core_client: HomeAssistantClient,
    network: ZwaveNetwork,
    socket: HomeAssistantWebSocketClient,
    asleep: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real bridge wraps the command deadline in a connection error."""
    network.parameters = {"root": parameter(8)}
    # The server's own command deadline, cut so the test does not wait 30 s.
    monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", 0.2)

    with patch.object(socket, "send_command", wraps=socket.send_command) as spy:
        body = await failed_read(core_client, network, refresh=True)

    assert body["error"]["code"] == "TIMEOUT_WEBSOCKET"
    assert "no cached fallback" in body["error"]["message"]
    assert "queued" in body["error"]["message"]
    assert "wake" in body["error"]["suggestion"].lower()
    assert body["device_id"] == network.device_id
    assert body["property"] == 3
    assert body["refresh_requested"] is True
    assert body["ws_type"] == f"{_DOMAIN}/get_raw_config_parameter"
    assert spy.await_count == 2


async def test_refresh_connection_loss_does_not_claim_a_sleeping_node(
    core_client: HomeAssistantClient,
    network: ZwaveNetwork,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """A dropped socket is distinct from a command deadline expiring."""
    network.parameters = {"root": parameter(8)}

    async def connection_drops(
        connection: websocket_api.ActiveConnection, msg: dict
    ) -> Any:
        # The connection goes away while the read waits for its answer.
        await socket.disconnect()

    network.raw_read = connection_drops

    body = await failed_read(core_client, network, refresh=True)

    assert body["error"]["code"] == "CONNECTION_FAILED"
    assert "queued" not in body["error"]["message"]
    assert body["device_id"] == network.device_id
