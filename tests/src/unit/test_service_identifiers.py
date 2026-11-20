"""Service names must be unambiguous before selecting a dispatch route."""

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_service import ServiceTools

from ._tool_errors import error_body


@pytest.mark.parametrize("service", ["wake-up", "wake?up"])
async def test_tool_dispatches_exact_registered_name(
    hass: HomeAssistant, core_client: HomeAssistantClient, service: str
) -> None:
    called: list[str] = []

    async def handle(call: ServiceCall) -> None:
        called.append(call.service)

    hass.services.async_register("python_script", service, handle)
    result = await ServiceTools(core_client, None).ha_call_service(
        "python_script", service, wait=False
    )
    assert result["success"] is True
    assert called == [service]


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
        ("light", "turn/on", "service"),
        ("light", ".", "service"),
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


async def test_tool_reports_both_invalid_arguments(
    core_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ToolError) as exc:
        await ServiceTools(core_client, None).ha_call_service("light/group", "TURN_ON")
    assert set(error_body(exc)["parameters"]) == {"domain", "service"}


async def test_rest_client_rejects_invalid_path_segment(
    core_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ValueError, match="domain"):
        await core_client.call_service("test/group", "turn_on")
