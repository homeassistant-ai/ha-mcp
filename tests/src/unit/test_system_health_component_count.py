from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_system import SystemTools


async def test_component_count_matches_the_components_core_reports(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    assert await async_setup_component(hass, "system_health", {})
    assert await async_setup_component(hass, "homeassistant", {})

    result = await SystemTools(core_client).ha_get_system_health()

    components = result["health_info"]["data"]
    assert components
    assert result["component_count"] == len(components)
