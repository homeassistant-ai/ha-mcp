"""The withdrawn config_get command stays absent from the real command surface."""

from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import MockHAClientWebSocket

from ._component_core import ws_call, ws_result


async def test_config_get_is_neither_advertised_nor_registered(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    info = await ws_result(component_ws, "ha_mcp_tools/info")
    assert "config_get" not in info["capabilities"]
    reply = await ws_call(
        component_ws, "ha_mcp_tools/config_get", entity_id="automation.old"
    )
    assert reply["success"] is False
    assert reply["error"]["code"] == "unknown_command"
