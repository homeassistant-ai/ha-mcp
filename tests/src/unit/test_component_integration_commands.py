"""The component's integration_commands bridge follows Core's command registry.

The commands are registered on a real ``hass`` with Core's own
``websocket_command`` decorator, as an integration registers them, and the
bridge is called over a real WebSocket connection.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from ._component_core import ws_result


def _register(hass: HomeAssistant, command: str, **fields: Any) -> None:
    """Register ``command`` with the given request fields, as an integration does."""

    schema: dict[Any, Any] = {"type": command}
    schema.update(fields)

    @websocket_api.websocket_command(schema)
    @callback
    def handler(
        hass: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        connection.send_result(msg["id"], None)

    websocket_api.async_register_command(hass, handler)


async def test_lists_only_the_domains_own_commands_with_their_schemas(
    hass: HomeAssistant, component_ws: Any
) -> None:
    _register(hass, "knx/delete_entity", entity_id=str)
    _register(hass, "knx/get_base_data", future=int)
    _register(hass, "knxx/other", other=int)
    _register(hass, "config/core/update", other=int)

    result = await ws_result(
        component_ws, "ha_mcp_tools/integration_commands", domain="knx"
    )

    assert result["status"] == "available"
    assert [row["command"] for row in result["commands"]] == [
        "knx/delete_entity",
        "knx/get_base_data",
    ]
    schema = result["commands"][1]["schema"]
    assert set(schema["properties"]) == {"id", "type", "future"}
    assert schema["properties"]["future"] == {"type": "integer"}
    assert result["commands"][1]["description_complete"] is True


async def test_a_command_without_fields_is_described_with_cores_message_schema(
    hass: HomeAssistant, component_ws: Any
) -> None:
    _register(hass, "knx/get_knx_project")

    listing = await ws_result(
        component_ws, "ha_mcp_tools/integration_commands", domain="knx"
    )

    assert [row["command"] for row in listing["commands"]] == ["knx/get_knx_project"]
    schema = listing["commands"][0]["schema"]
    assert set(schema["properties"]) == {"id", "type"}
    assert schema["required"] == ["id"]


async def test_info_advertises_integration_commands(component_ws: Any) -> None:
    info = await ws_result(component_ws, "ha_mcp_tools/info")

    assert "integration_commands" in info["capabilities"]
