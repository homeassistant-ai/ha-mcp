"""The component's integration_commands bridge follows Core's command registry.

The commands are the ones Core's own integrations register on a real ``hass``:
Core's ``knx`` integration set up from its config entry (``_knx_core``) and
Core's ``config`` integration. The bridge is called over a real WebSocket
connection.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ._component_core import ws_result
from ._knx_core import set_up_knx


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """KNX keeps its telegram database in the config directory; this one is the test's."""
    return hass_tmp_config_dir


async def _listing(component_ws: Any, domain: str) -> list[dict[str, Any]]:
    result = await ws_result(
        component_ws, "ha_mcp_tools/integration_commands", domain=domain
    )
    assert result["status"] == "available"
    commands: list[dict[str, Any]] = result["commands"]
    return commands


async def test_lists_only_the_domains_own_commands_with_their_schemas(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, component_ws: Any
) -> None:
    await set_up_knx(hass, monkeypatch)
    assert await async_setup_component(hass, "config", {})

    rows = {row["command"]: row for row in await _listing(component_ws, "knx")}

    # config/* and ha_mcp_tools/* are registered beside KNX's commands.
    assert all(command.startswith("knx/") for command in rows)
    delete = rows["knx/delete_entity"]
    assert set(delete["schema"]["properties"]) == {"id", "type", "entity_id"}
    assert delete["schema"]["properties"]["entity_id"] == {"type": "string"}
    assert delete["description_complete"] is True


async def test_a_domain_sharing_the_prefix_without_the_slash_is_not_listed(
    hass: HomeAssistant, component_ws: Any
) -> None:
    # Core's config integration registers both config/* and config_entries/*.
    assert await async_setup_component(hass, "config", {})

    config = [row["command"] for row in await _listing(component_ws, "config")]
    config_entries = await _listing(component_ws, "config_entries")

    assert config
    assert config_entries
    assert all(command.startswith("config/") for command in config)


async def test_a_command_without_fields_is_described_with_cores_message_schema(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, component_ws: Any
) -> None:
    await set_up_knx(hass, monkeypatch)

    rows = {row["command"]: row for row in await _listing(component_ws, "knx")}

    # KNX registers knx/get_knx_project with no request field.
    schema = rows["knx/get_knx_project"]["schema"]
    assert set(schema["properties"]) == {"id", "type"}
    assert schema["required"] == ["id"]


async def test_info_advertises_integration_commands(component_ws: Any) -> None:
    info = await ws_result(component_ws, "ha_mcp_tools/info")

    assert "integration_commands" in info["capabilities"]
