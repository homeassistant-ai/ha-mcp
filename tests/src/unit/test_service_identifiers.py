"""Service names must be unambiguous before selecting a dispatch route."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools.tools_service import ServiceTools

from ._tool_errors import error_body


async def test_tool_dispatches_only_the_exact_service_name(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"test_lamp": {"initial": False}}}
    )
    tools = ServiceTools(core_client, None)
    entity = "input_boolean.test_lamp"
    with pytest.raises(ToolError):
        await tools.ha_call_service(
            "input_boolean", "TURN_ON", entity_id=entity, wait=False
        )
    state = hass.states.get(entity)
    assert state is not None and state.state == "off"

    result = await tools.ha_call_service(
        "input_boolean", "turn_on", entity_id=entity, wait=False
    )
    assert result["success"] is True
    state = hass.states.get(entity)
    assert state is not None and state.state == "on"


@pytest.mark.parametrize(
    ("domain", "service", "parameter"),
    [
        ("light/group", "turn_on", "domain"),
        ("light", "turn on", "service"),
        ("Light", "turn_on", "domain"),
        ("light", "turn_on\n", "service"),
    ],
)
async def test_tool_rejects_invalid_identifier(
    core_client: HomeAssistantClient, domain: str, service: str, parameter: str
) -> None:
    tools = ServiceTools(core_client, None)
    with pytest.raises(ToolError) as exc:
        await tools.ha_call_service(domain=domain, service=service, wait=False)
    body = error_body(exc)
    assert body["error"]["code"] == "VALIDATION_FAILED"
    assert body["parameter"] == parameter


async def test_clients_reject_invalid_identifier_before_dispatch(
    core_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ValueError, match="domain"):
        await core_client.call_service("test.group", "turn_on")

    ws = HomeAssistantWebSocketClient(core_client.base_url, core_client.token)
    with pytest.raises(ValueError, match="service"):
        await ws.call_service("test", "turn on")
