"""Z-Wave parameter reads work without Home Assistant entities.

The server's client talks to a real ``hass`` running Core's real ``zwave_js``
integration, so ``zwave_js/get_config_parameters`` and
``zwave_js/get_raw_config_parameter`` are answered by Core's own handlers. Only
the Z-Wave JS server connection below zwave-js-server-python is a mock
(``_zwave_js``): the node's configuration values are the node state the server
sends, and a raw read is the server's answer to the library's
``endpoint.get_raw_config_parameter_value`` command.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import WebSocketGenerator
from zwave_js_server.const import CommandClass
from zwave_js_server.exceptions import FailedZWaveCommand

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.tools.tools_radio import RadioTools

from ._client_spies import sent_messages
from ._component_core import ws_result
from ._tool_errors import rejected_call_body
from ._zwave_js import (
    HOME_ID,
    NODE_ID,
    node_state,
    set_up_zwave_js,
    zwave_js_server_connection,
)


def config_value(
    value: Any, *, endpoint: int = 0, mask: int | None = None
) -> dict[str, Any]:
    """Parameter 3 (or its ``mask`` bits) as the Z-Wave JS server reports it."""
    data: dict[str, Any] = {
        "commandClass": CommandClass.CONFIGURATION,
        "commandClassName": "Configuration",
        "endpoint": endpoint,
        "property": 3,
        "propertyName": "Ramp rate",
        "ccVersion": 1,
        "metadata": {
            "type": "number",
            "readable": True,
            "writeable": True,
            "label": "Ramp rate",
            "min": 0,
            "max": 99,
            "default": 0,
            "states": {"0": "Immediate"},
            "valueSize": 1,
            "format": 0,
            "allowManualEntry": True,
            "isFromConfig": True,
        },
        "value": value,
    }
    if mask is not None:
        data["propertyKey"] = mask
    return data


@pytest.fixture
def node_values() -> list[dict[str, Any]]:
    """The node's configuration values; a test overrides it by parametrizing."""
    return [config_value(8)]


@pytest.fixture
def server(node_values: list[dict[str, Any]]) -> Iterator[MagicMock]:
    """The Z-Wave JS server connection, holding the node with ``node_values``."""
    with zwave_js_server_connection(node_state(node_values)) as client:
        yield client


@pytest.fixture
async def device_id(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    device_registry: dr.DeviceRegistry,
    server: MagicMock,
) -> str:
    """The device Core's ``zwave_js`` registers for the node."""
    assert await async_setup_component(hass, "config", {})
    entry = await set_up_zwave_js(hass)
    device = device_registry.async_get_device_by_identifier(
        ("zwave_js", f"{HOME_ID}-{NODE_ID}"), entry.entry_id
    )
    assert device is not None
    return device.id


def sent_types(spy: AsyncMock) -> list[str]:
    return [sent.args[0]["type"] for sent in spy.call_args_list]


async def read_param(
    client: HomeAssistantClient, device_id: str, **params: Any
) -> dict[str, Any]:
    return await RadioTools(client).ha_manage_radio(
        radio="zwave",
        action="get_config_param",
        device_id=device_id,
        params={"property": 3, **params},
    )


@pytest.mark.parametrize(
    "node_values", [[config_value(0), config_value(5, endpoint=1)]]
)
async def test_parameters_need_no_config_entities_and_preserve_metadata(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    device_id: str,
) -> None:
    """Core's own reply comes back unchanged with the node's entities removed."""
    for entity in er.async_entries_for_device(
        entity_registry, device_id, include_disabled_entities=True
    ):
        entity_registry.async_remove(entity.entity_id)
    await hass.async_block_till_done()
    assert not er.async_entries_for_device(
        entity_registry, device_id, include_disabled_entities=True
    )
    core_reply = await ws_result(
        await hass_ws_client(hass),
        "zwave_js/get_config_parameters",
        device_id=device_id,
    )
    assert len(core_reply) == 2

    with (
        sent_messages(core_client) as spy,
        patch.object(
            core_client, "call_service", wraps=core_client.call_service
        ) as service,
    ):
        result = await RadioTools(core_client).ha_manage_radio(
            radio="zwave", action="get_config_params", device_id=device_id
        )

    assert result["parameters"] == core_reply
    assert {p["endpoint"] for p in result["parameters"].values()} == {0, 1}
    assert all(
        p["metadata"]["states"] == {"0": "Immediate"}
        for p in result["parameters"].values()
    )
    assert result["source"] == "cache"
    assert result["refresh_requested"] is False
    assert sent_types(spy) == ["zwave_js/get_config_parameters"]
    service.assert_not_called()


@pytest.mark.parametrize(
    "node_values",
    [
        [
            config_value(0),
            config_value(2, endpoint=1, mask=15),
            config_value(3, endpoint=1, mask=240),
        ]
    ],
)
async def test_cached_read_selects_endpoint_and_bit_mask_without_polling(
    core_client: HomeAssistantClient, device_id: str
) -> None:
    with sent_messages(core_client) as spy:
        result = await read_param(core_client, device_id, endpoint=1, property_key=240)

    assert result["parameter"]["value"] == 3
    assert result["parameter"]["property_key"] == 240
    assert result["source"] == "cache"
    assert spy.await_count == 1


async def test_disabled_entity_is_only_an_optional_device_resolver(
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    device_id: str,
) -> None:
    """Core registers parameter 3's number entity disabled by default."""
    numbers = [
        entity
        for entity in er.async_entries_for_device(
            entity_registry, device_id, include_disabled_entities=True
        )
        if entity.domain == "number"
    ]
    assert len(numbers) == 1
    assert numbers[0].disabled
    entity_id = numbers[0].entity_id

    with sent_messages(core_client) as spy:
        result = await RadioTools(core_client).ha_manage_radio(
            radio="zwave",
            action="get_config_param",
            entity_id=entity_id,
            params={"property": 3},
        )

    assert result["parameter"]["value"] == 8
    assert result["device_id"] == device_id
    assert "warnings" not in result
    assert sent_types(spy) == [
        "config/entity_registry/get",
        "zwave_js/get_config_parameters",
    ]


@pytest.mark.parametrize("node_values", [[config_value(2, mask=15)]])
async def test_refresh_reads_raw_value_without_substituting_cached_partial(
    core_client: HomeAssistantClient, server: MagicMock, device_id: str
) -> None:
    server.async_send_command.return_value = {"value": 12}

    with (
        sent_messages(core_client) as spy,
        patch.object(
            core_client, "call_service", wraps=core_client.call_service
        ) as service,
    ):
        result = await read_param(core_client, device_id, refresh=True)

    assert result["parameter"]["value"] == 12
    assert result["parameter"]["property_key"] is None
    assert result["parameter"]["metadata"] is None
    assert "metadata_source" not in result
    assert result["source"] == "device_request"
    assert result["refresh_requested"] is True
    assert [call.args[0] for call in spy.call_args_list] == [
        {"type": "zwave_js/get_config_parameters", "device_id": device_id},
        {
            "type": "zwave_js/get_raw_config_parameter",
            "device_id": device_id,
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
    core_client: HomeAssistantClient, device_id: str, params: dict[str, Any]
) -> None:
    with sent_messages(core_client) as spy, pytest.raises(ToolError):
        await RadioTools(core_client).ha_manage_radio(
            radio="zwave",
            action="get_config_param",
            device_id=device_id,
            params=params,
        )
    spy.assert_not_called()


@pytest.mark.parametrize(
    "node_values",
    [
        pytest.param([], id="absent"),
        pytest.param([config_value(2, mask=15)], id="only-a-partial"),
    ],
)
async def test_missing_cached_parameter_does_not_implicitly_poll(
    core_client: HomeAssistantClient, device_id: str
) -> None:
    """A partial parameter is not mistaken for the full one asked for."""
    with sent_messages(core_client) as spy, pytest.raises(ToolError, match="not found"):
        await read_param(core_client, device_id)

    assert spy.await_count == 1


async def test_ha_failure_never_substitutes_a_cached_value(
    core_client: HomeAssistantClient, server: MagicMock, device_id: str
) -> None:
    """A failed refresh reports Core's error, never the cached value."""
    server.async_send_command.side_effect = FailedZWaveCommand(
        "raw-read", 201, "Timeout while waiting for a response from the node"
    )

    body = await rejected_call_body(read_param(core_client, device_id, refresh=True))

    assert body["ha_error_code"] == "zwave_error"
    assert "parameter" not in body


async def test_unknown_raw_failure_keeps_details_without_claiming_a_cause(
    core_client: HomeAssistantClient, server: MagicMock, device_id: str
) -> None:
    """A server result without a value fails the library's assertion, which
    Core answers as unknown_error, the code it also uses for unrelated
    handler failures."""
    server.async_send_command.return_value = {}

    with sent_messages(core_client) as spy:
        body = await rejected_call_body(
            read_param(core_client, device_id, refresh=True)
        )

    assert body["ha_error_code"] == "unknown_error"
    assert body["device_id"] == device_id
    assert body["property"] == 3
    assert "no cached fallback" in body["error"]["message"]
    assert "returned no value" not in body["error"]["message"]
    assert "Unknown error" in body["error"]["details"]
    assert "parameter" in body["error"]["suggestion"].lower()
    assert spy.await_count == 2


@pytest.mark.parametrize("node_values", [[config_value(None)]])
async def test_unknown_cached_value_remains_unknown(
    core_client: HomeAssistantClient, device_id: str
) -> None:
    result = await read_param(core_client, device_id)

    assert result["parameter"]["value"] is None
    assert result["warnings"]


async def test_list_cannot_silently_accept_a_refresh_request(
    core_client: HomeAssistantClient, device_id: str
) -> None:
    with (
        sent_messages(core_client) as spy,
        pytest.raises(ToolError, match="cache-only"),
    ):
        await RadioTools(core_client).ha_manage_radio(
            radio="zwave",
            action="get_config_params",
            device_id=device_id,
            params={"refresh": True},
        )
    spy.assert_not_called()


async def test_refresh_preserves_zero_and_marks_cached_metadata(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    core_client: HomeAssistantClient,
    server: MagicMock,
    device_id: str,
) -> None:
    cached = await ws_result(
        await hass_ws_client(hass),
        "zwave_js/get_config_parameters",
        device_id=device_id,
    )
    server.async_send_command.return_value = {"value": 0}

    result = await read_param(
        core_client, device_id, endpoint=0, property_key=None, refresh=True
    )

    assert result["parameter"]["value"] == 0
    assert len(cached) == 1
    assert result["parameter"]["metadata"] == next(iter(cached.values()))["metadata"]
    assert result["metadata_source"] == "cache"


@pytest.fixture
async def socket(core_client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    """The pooled socket the client's WebSocket bridge sends on."""
    return await websocket_manager.get_client(
        url=core_client.base_url, token=core_client.token, verify_ssl=False
    )


@pytest.fixture
async def asleep(server: MagicMock, device_id: str) -> AsyncIterator[None]:
    """The node is asleep: once Core is set up, the server holds the raw read
    until the test ends."""
    woke = asyncio.Event()

    async def queued(message: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        await woke.wait()
        return {"value": 8}

    server.async_send_command.side_effect = queued
    try:
        yield
    finally:
        woke.set()


async def test_refresh_timeout_explains_the_possible_wakeup_queue(
    core_client: HomeAssistantClient,
    device_id: str,
    socket: HomeAssistantWebSocketClient,
    asleep: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real bridge wraps the command deadline in a connection error."""
    # ha-mcp's WebSocket command deadline, cut so the test does not wait 30 s.
    monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", 0.2)

    with patch.object(socket, "send_command", wraps=socket.send_command) as spy:
        body = await rejected_call_body(
            read_param(core_client, device_id, refresh=True)
        )

    assert body["error"]["code"] == "TIMEOUT_WEBSOCKET"
    assert "no cached fallback" in body["error"]["message"]
    assert "queued" in body["error"]["message"]
    assert "wake" in body["error"]["suggestion"].lower()
    assert body["device_id"] == device_id
    assert body["property"] == 3
    assert body["refresh_requested"] is True
    assert body["ws_type"] == "zwave_js/get_raw_config_parameter"
    assert spy.await_count == 2


async def test_refresh_connection_loss_does_not_claim_a_sleeping_node(
    core_client: HomeAssistantClient,
    server: MagicMock,
    device_id: str,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """A dropped socket is distinct from a command deadline expiring."""

    async def connection_drops(
        message: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        # The connection goes away while the read waits for the node's answer.
        await socket.disconnect()
        return {"value": 8}

    server.async_send_command.side_effect = connection_drops

    body = await rejected_call_body(read_param(core_client, device_id, refresh=True))

    assert body["error"]["code"] == "CONNECTION_FAILED"
    assert "queued" not in body["error"]["message"]
    assert body["device_id"] == device_id
