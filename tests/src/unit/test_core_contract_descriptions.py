"""Schema completeness must reflect strict serialization, including nested unions.

The commands are registered on a real ``hass`` through Core's own
``websocket_api.async_register_command``, so ``describe_contract`` reads Core's
own registry.
"""

from typing import Any

from homeassistant.components import websocket_api
from homeassistant.components.websocket_api.messages import (
    BASE_COMMAND_MESSAGE_SCHEMA,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient

HISTORY_COMMAND = "history/history_during_period"


@callback
def _noop_handler(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    connection.send_result(msg["id"], {})


def _register(hass: HomeAssistant, command: str, schema: Any) -> None:
    websocket_api.async_register_command(hass, command, _noop_handler, schema)


async def test_successful_strict_description_has_no_loss_warning(
    hass: HomeAssistant,
) -> None:
    from custom_components.ha_mcp_tools import core_contract as module

    assert await async_setup_component(hass, "websocket_api", {})
    _register(
        hass,
        HISTORY_COMMAND,
        BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {"type": HISTORY_COMMAND, "start_time": str}
        ),
    )
    result = module.describe_contract(hass, HISTORY_COMMAND)
    assert result["status"] == "available"
    assert result["schema"]
    assert result["description_complete"] is True
    assert "warnings" not in result


async def test_nested_unsupported_validator_cannot_be_marked_complete(
    hass: HomeAssistant,
) -> None:
    from custom_components.ha_mcp_tools import core_contract as module

    def opaque(value: Any) -> Any:
        """A validator function no serializer knows how to describe."""
        return value

    inner = BASE_COMMAND_MESSAGE_SCHEMA.extend({"type": "energy/save_prefs"}).extend(
        {"future": opaque}
    )
    nested: Any = cv.key_value_schemas("sub", {"inner": inner})
    validator = cv.key_value_schemas("kind", {"outer": nested})
    assert await async_setup_component(hass, "websocket_api", {})
    _register(hass, "energy/save_prefs", validator)
    result = module.describe_contract(hass, "energy/save_prefs")
    assert result["status"] == "available"
    assert result["description_complete"] is False
    assert result["warnings"]


async def test_info_advertises_contract_for_the_server_consumer(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    from ha_mcp.tools import core_contract

    _register(
        hass,
        HISTORY_COMMAND,
        BASE_COMMAND_MESSAGE_SCHEMA.extend(
            {"type": HISTORY_COMMAND, "start_time": str}
        ),
    )
    result = await core_contract.core_contract(component_core_client, HISTORY_COMMAND)
    assert result["status"] == "available"
    assert result["command"] == HISTORY_COMMAND
    assert result["schema"]
