"""Parameter reads must work independently of Home Assistant entities."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_radio import RadioTools


def client_with_parameters(parameters: dict[str, Any]) -> Any:
    client = MagicMock()

    async def send(message: dict[str, Any]) -> dict[str, Any]:
        if message["type"] == "zwave_js/get_config_parameters":
            return {"success": True, "result": parameters}
        if message["type"] == "zwave_js/get_raw_config_parameter":
            return {"success": True, "result": {"value": 12}}
        if message["type"] == "config/entity_registry/get":
            return {
                "success": True,
                "result": {"device_id": "node-device", "disabled_by": "user"},
            }
        raise AssertionError(f"Unexpected command: {message}")

    client.send_websocket_message = AsyncMock(side_effect=send)
    return client


def parameter(
    value: Any, *, endpoint: int = 0, mask: int | None = None
) -> dict[str, Any]:
    return {
        "property": 3,
        "property_key": mask,
        "endpoint": endpoint,
        "configuration_value_type": "full" if mask is None else "partial",
        "value": value,
        "metadata": {
            "label": "Ramp rate",
            "min": 0,
            "max": 99,
            "default": 0,
            "states": {"0": "Immediate"},
        },
    }


async def test_parameters_need_no_config_entities_and_preserve_metadata() -> None:
    parameters = {"root": parameter(0), "endpoint": parameter(5, endpoint=1)}
    client = client_with_parameters(parameters)
    result = await RadioTools(client).ha_manage_radio(
        radio="zwave", action="get_config_params", device_id="node-device"
    )
    assert result["parameters"] == parameters
    assert result["source"] == "cache"
    assert result["refresh_requested"] is False
    assert [
        call.args[0]["type"] for call in client.send_websocket_message.call_args_list
    ] == ["zwave_js/get_config_parameters"]
    client.call_service.assert_not_called()


async def test_cached_read_selects_endpoint_and_bit_mask_without_polling() -> None:
    client = client_with_parameters(
        {
            "root": parameter(0),
            "other": parameter(2, endpoint=1, mask=15),
            "selected": parameter(3, endpoint=1, mask=240),
        }
    )
    result = await RadioTools(client).ha_manage_radio(
        radio="zwave",
        action="get_config_param",
        device_id="node-device",
        params={"property": 3, "endpoint": 1, "property_key": 240},
    )
    assert result["parameter"]["value"] == 3
    assert result["parameter"]["property_key"] == 240
    assert result["source"] == "cache"
    assert client.send_websocket_message.await_count == 1


async def test_disabled_entity_is_only_an_optional_device_resolver() -> None:
    client = client_with_parameters({"root": parameter(0)})
    result = await RadioTools(client).ha_manage_radio(
        radio="zwave",
        action="get_config_param",
        entity_id="number.example_disabled",
        params={"property": 3},
    )
    assert result["parameter"]["value"] == 0
    assert result["device_id"] == "node-device"
    assert "warnings" not in result
    assert [
        call.args[0]["type"] for call in client.send_websocket_message.call_args_list
    ] == ["config/entity_registry/get", "zwave_js/get_config_parameters"]


async def test_refresh_reads_raw_value_without_substituting_cached_partial() -> None:
    client = client_with_parameters({"partial": parameter(2, mask=15)})
    result = await RadioTools(client).ha_manage_radio(
        radio="zwave",
        action="get_config_param",
        device_id="node-device",
        params={"property": 3, "refresh": True},
    )
    assert result["parameter"]["value"] == 12
    assert result["parameter"]["property_key"] is None
    assert result["parameter"]["metadata"] is None
    assert "metadata_source" not in result
    assert result["source"] == "device_request"
    assert result["refresh_requested"] is True
    assert [call.args[0] for call in client.send_websocket_message.call_args_list] == [
        {"type": "zwave_js/get_config_parameters", "device_id": "node-device"},
        {
            "type": "zwave_js/get_raw_config_parameter",
            "device_id": "node-device",
            "property": 3,
        },
    ]
    client.call_service.assert_not_called()


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
    params: dict[str, Any],
) -> None:
    client = client_with_parameters({})
    with pytest.raises(ToolError):
        await RadioTools(client).ha_manage_radio(
            radio="zwave",
            action="get_config_param",
            device_id="node-device",
            params=params,
        )
    client.send_websocket_message.assert_not_called()


async def test_missing_cached_parameter_does_not_implicitly_poll() -> None:
    client = client_with_parameters({})
    with pytest.raises(ToolError, match="not found"):
        await RadioTools(client).ha_manage_radio(
            radio="zwave",
            action="get_config_param",
            device_id="node-device",
            params={"property": 3},
        )
    assert client.send_websocket_message.await_count == 1


async def test_ha_failure_never_substitutes_a_cached_value() -> None:
    client = client_with_parameters({"root": parameter(8)})
    client.send_websocket_message.side_effect = [
        {"success": True, "result": {"root": parameter(8)}},
        {"success": False, "error": "Command failed", "error_code": "not_found"},
    ]
    with pytest.raises(ToolError):
        await RadioTools(client).ha_manage_radio(
            radio="zwave",
            action="get_config_param",
            device_id="node-device",
            params={"property": 3, "refresh": True},
        )


async def test_unknown_raw_failure_keeps_details_without_claiming_a_cause() -> None:
    """HA uses unknown_error for empty results and unrelated handler failures."""
    client = client_with_parameters({"root": parameter(8)})
    client.send_websocket_message.side_effect = [
        {"success": True, "result": {"root": parameter(8)}},
        {
            "success": False,
            "error": "Command failed: Unknown error",
            "error_code": "unknown_error",
        },
    ]
    with pytest.raises(ToolError, match="failed without a value") as exc:
        await RadioTools(client).ha_manage_radio(
            radio="zwave",
            action="get_config_param",
            device_id="node-device",
            params={"property": 3, "refresh": True},
        )
    body = json.loads(str(exc.value))
    assert body["ha_error_code"] == "unknown_error"
    assert body["device_id"] == "node-device"
    assert body["property"] == 3
    assert "no cached fallback" in body["error"]["message"]
    assert "returned no value" not in body["error"]["message"]
    assert body["error"]["details"] == "Command failed: Unknown error"
    assert "parameter" in body["error"]["suggestion"].lower()
    assert client.send_websocket_message.await_count == 2


async def test_unknown_cached_value_remains_unknown() -> None:
    client = client_with_parameters({"root": parameter(None)})
    result = await RadioTools(client).ha_manage_radio(
        radio="zwave",
        action="get_config_param",
        device_id="node-device",
        params={"property": 3},
    )
    assert result["parameter"]["value"] is None
    assert result["warnings"]


async def test_list_cannot_silently_accept_a_refresh_request() -> None:
    client = client_with_parameters({})
    with pytest.raises(ToolError, match="cache-only"):
        await RadioTools(client).ha_manage_radio(
            radio="zwave",
            action="get_config_params",
            device_id="node-device",
            params={"refresh": True},
        )
    client.send_websocket_message.assert_not_called()


async def test_refresh_preserves_zero_and_marks_cached_metadata() -> None:
    client = client_with_parameters({})
    original = parameter(8)
    client.send_websocket_message.side_effect = [
        {"success": True, "result": {"root": original}},
        {"success": True, "result": {"value": 0}},
    ]
    result = await RadioTools(client).ha_manage_radio(
        radio="zwave",
        action="get_config_param",
        device_id="node-device",
        params={"property": 3, "endpoint": 0, "property_key": None, "refresh": True},
    )
    assert result["parameter"]["value"] == 0
    assert result["parameter"]["metadata"] == original["metadata"]
    assert result["metadata_source"] == "cache"
    assert original["value"] == 8


async def test_refresh_timeout_explains_the_possible_wakeup_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real bridge's command-timeout to connection-error wrapper."""
    ws = MagicMock()
    ws.send_command = AsyncMock(
        side_effect=[
            {"success": True, "result": {"root": parameter(8)}},
            HomeAssistantCommandTimeout("Command timeout"),
        ]
    )
    monkeypatch.setattr(
        "ha_mcp.client.websocket_client.get_websocket_client",
        AsyncMock(return_value=ws),
    )
    client = HomeAssistantClient(base_url="http://ha.test:8123", token="test-token")
    with pytest.raises(ToolError) as exc:
        await RadioTools(client).ha_manage_radio(
            radio="zwave",
            action="get_config_param",
            device_id="node-device",
            params={"property": 3, "refresh": True},
        )
    body = json.loads(str(exc.value))
    assert body["error"]["code"] == "TIMEOUT_WEBSOCKET"
    assert "no cached fallback" in body["error"]["message"]
    assert "queued" in body["error"]["message"]
    assert "wake" in body["error"]["suggestion"].lower()
    assert body["device_id"] == "node-device"
    assert body["property"] == 3
    assert body["refresh_requested"] is True
    assert body["ws_type"] == "zwave_js/get_raw_config_parameter"
    assert ws.send_command.await_count == 2


async def test_refresh_connection_loss_does_not_claim_a_sleeping_node() -> None:
    """A disconnected socket is distinct from a command deadline expiring."""
    client = client_with_parameters({"root": parameter(8)})
    client.send_websocket_message.side_effect = [
        {"success": True, "result": {"root": parameter(8)}},
        HomeAssistantConnectionError("WebSocket disconnected"),
    ]
    with pytest.raises(ToolError) as exc:
        await RadioTools(client).ha_manage_radio(
            radio="zwave",
            action="get_config_param",
            device_id="node-device",
            params={"property": 3, "refresh": True},
        )
    body = json.loads(str(exc.value))
    assert body["error"]["code"] == "CONNECTION_FAILED"
    assert "queued" not in body["error"]["message"]
    assert body["device_id"] == "node-device"


async def test_partial_parameters_are_not_mistaken_for_a_full_cached_value() -> None:
    client = client_with_parameters({"partial": parameter(2, mask=15)})
    with pytest.raises(ToolError, match="not found"):
        await RadioTools(client).ha_manage_radio(
            radio="zwave",
            action="get_config_param",
            device_id="node-device",
            params={"property": 3},
        )
    assert client.send_websocket_message.await_count == 1
