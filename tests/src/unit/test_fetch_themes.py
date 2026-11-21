"""The themes section of ``ha_get_system_health`` moved to ``ha_manage_theme``.

The tool runs on the server's own client against a real ``hass``.
"""

from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_system import SystemTools


async def test_themes_point_to_ha_manage_theme(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    assert await async_setup_component(hass, "system_health", {})
    config = {"frontend": {"development_repo": str(tmp_path)}}
    assert await async_setup_component(hass, "frontend", config)

    result = await SystemTools(core_client).ha_get_system_health(include="themes")

    assert "themes" not in result
    assert "'themes' moved to ha_manage_theme(action='list')" in result["warnings"]
