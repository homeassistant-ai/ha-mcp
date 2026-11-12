"""ha_search says so when it cannot use the name Home Assistant shows.

Only a current ha_mcp_tools component can ask Core for an entity's composed
name. A search answered without one knows an entity by its id, legacy
friendly_name and aliases and leaves ``name`` out of every record, so an empty
result and a requested ``name`` both have to say why.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools.websocket_api import system
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.component_api import ENTITY_COMPOSED_NAME
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools

from ._search_tool import _setup_visibility_disabled


@pytest.fixture
async def search_tools(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> SearchTools:
    """The search tools on a ``hass`` with lights and no ha_mcp_tools commands."""
    # ``config`` serves the registry commands.
    assert await async_setup_component(hass, "config", {})
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen Light"})
    hass.states.async_set("light.bedroom", "off", {"friendly_name": "Bedroom Light"})
    hass.states.async_set("switch.fan", "on", {"friendly_name": "Fan"})
    return SearchTools(core_client, SmartSearchTools(core_client))


async def test_a_requested_composed_name_is_not_dropped_without_a_word(
    search_tools: SearchTools,
) -> None:
    data = await search_tools.ha_search(
        query="light", result_fields=["entity_id", "name"]
    )

    assert data["entities"]
    assert all("name" not in row for row in data["entities"])
    assert any("composed name" in w for w in data.get("warnings", []))


async def test_a_search_that_finds_nothing_says_the_component_is_missing(
    search_tools: SearchTools,
) -> None:
    data = await search_tools.ha_search(query="nothing is called this")

    assert data["count"] == 0
    assert any(
        "No ha_mcp_tools component detected" in w for w in data.get("warnings", [])
    )


async def test_a_search_that_finds_something_carries_no_component_hint(
    search_tools: SearchTools,
) -> None:
    data = await search_tools.ha_search(query="light")

    assert data["entities"]
    assert not any(
        "No ha_mcp_tools component detected" in w for w in data.get("warnings", [])
    )


async def test_a_component_that_predates_composed_names_is_told_apart_from_none(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outdated component is installed: the fix is an update, not an install."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    # A component from before composed names: it does not advertise them.
    monkeypatch.setattr(
        system,
        "CAPABILITIES",
        [cap for cap in system.CAPABILITIES if cap != ENTITY_COMPOSED_NAME],
    )
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen Light"})
    tools = SearchTools(
        component_core_client, SmartSearchTools(client=component_core_client)
    )

    data = await tools.ha_search(
        query="nothing is called this", result_fields=["entity_id", "name"]
    )

    warnings = data.get("warnings", [])
    assert sum("update it" in w for w in warnings) == 2, warnings
    assert not any("No ha_mcp_tools component detected" in w for w in warnings)


async def test_an_empty_search_with_a_current_component_carries_no_hint(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy path answering beside a current component is not a missing one."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    # ``config`` serves the registry reads of the legacy path.
    assert await async_setup_component(hass, "config", {})
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen Light"})

    def _fails(*args: Any, **kwargs: Any) -> None:
        raise HomeAssistantError("boom")

    # The current component's search fails, so the legacy path answers.
    monkeypatch.setattr(
        "custom_components.ha_mcp_tools.websocket_api.search._search_entities",
        _fails,
    )
    tools = SearchTools(
        component_core_client, SmartSearchTools(client=component_core_client)
    )

    data = await tools.ha_search(query="nothing is called this")

    assert data["count"] == 0
    warnings = data.get("warnings", [])
    assert not any("No ha_mcp_tools component detected" in w for w in warnings)
    assert not any("update it" in w for w in warnings)
