"""Classify answers from Core and the File & YAML Tools service by their codes."""

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantCommandError
from ha_mcp.tools.helpers import exception_to_structured_error

from ._command_faults import pooled_socket
from ._file_services import make_file_service


async def test_core_schema_refusal_preserves_its_code(
    core_client: HomeAssistantClient,
) -> None:
    ws = await pooled_socket(core_client)
    with pytest.raises(HomeAssistantCommandError) as refused:
        await ws.send_command("get_states", unexpected=True)

    assert refused.value.code == "invalid_format"
    result = exception_to_structured_error(refused.value, raise_error=False)
    assert result["error"]["code"] == "VALIDATION_FAILED"
    assert result["ha_error_code"] == "invalid_format"


async def test_translated_service_refusal_survives_both_client_paths(
    core_client: HomeAssistantClient,
) -> None:
    target = {"domain": "counter", "service": "missing_action"}
    ws = await pooled_socket(core_client)
    with pytest.raises(HomeAssistantCommandError) as refused:
        await ws.send_command("call_service", **target)
    reply = await core_client.send_websocket_message({"type": "call_service", **target})

    assert refused.value.translation_key == reply["translation_key"] == "service_not_found"
    assert refused.value.translation_domain == reply["translation_domain"] == "homeassistant"
    assert refused.value.translation_placeholders == reply["translation_placeholders"] == target


async def test_file_service_identifies_its_own_missing_file(
    hass: HomeAssistant,
) -> None:
    service = await make_file_service(hass)
    result = await service("read_file", {"path": "www/missing.css"})

    assert result["success"] is False
    assert result["error_code"] == "not_found"
