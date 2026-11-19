"""Service names must be unambiguous before selecting a dispatch route."""

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools.tools_service import ServiceTools

from ._tool_errors import error_body


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
