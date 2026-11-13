"""Schema completeness must reflect strict serialization, including nested unions.

The commands are Core's own, registered by the real ``history`` and ``energy``
integrations, so ``describe_contract`` reads the schemas Core validates with.
"""

from typing import Any

from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient

HISTORY_COMMAND = "history/history_during_period"
ENERGY_COMMAND = "energy/save_prefs"


async def test_successful_strict_description_has_no_loss_warning(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Every validator of Core's history request has a JSON Schema form."""
    from custom_components.ha_mcp_tools import core_contract as module

    assert await async_setup_component(hass, "history", {})
    result = module.describe_contract(hass, HISTORY_COMMAND)
    assert result["status"] == "available"
    assert result["schema"]["properties"]["type"] == {"const": HISTORY_COMMAND}
    assert result["description_complete"] is True
    assert "warnings" not in result


async def test_nested_unsupported_validator_cannot_be_marked_complete(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Core's energy sources are a tagged union whose grid alternative ends in
    a plain validator function (its power-config check), which has no JSON
    Schema form: the description keeps the union but is never complete."""
    from custom_components.ha_mcp_tools import core_contract as module

    assert await async_setup_component(hass, "energy", {})
    result = module.describe_contract(hass, ENERGY_COMMAND)
    assert result["status"] == "available"
    assert result["description_complete"] is False
    assert result["warnings"]
    sources: list[dict[str, Any]] = result["schema"]["properties"]["energy_sources"][
        "items"
    ]["anyOf"]
    by_type = {source["properties"]["type"]["const"]: source for source in sources}
    assert len(by_type) > 1
    assert "properties" in by_type["grid"]["properties"]["power_config"]


async def test_info_advertises_contract_for_the_server_consumer(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
) -> None:
    from ha_mcp.tools import core_contract

    assert await async_setup_component(hass, "history", {})
    result = await core_contract.core_contract(component_core_client, HISTORY_COMMAND)
    assert result["status"] == "available"
    assert result["command"] == HISTORY_COMMAND
    assert result["schema"]
