"""Search modes use the component without exposing legacy-only tuning.

``ha_search`` runs on the server's own client against a real ``hass`` that
also answers ``ha_mcp_tools/*``, so the component's capabilities, search and
location resolution are the real handlers.
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import register_search_tools
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config


def _build_ha_search(client: HomeAssistantClient) -> Any:
    mcp = MagicMock()
    registered: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        name = (
            method.__fastmcp__.name
            if hasattr(method, "__fastmcp__")
            else method.__name__
        )
        registered[name] = method

    mcp.add_tool = capture_add_tool
    register_search_tools(mcp, client, smart_tools=SmartSearchTools(client=client))
    return registered["ha_search"]


def _setup_visibility_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save_visibility_config(tmp_path, VisibilityConfig(enabled=False))
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)


@pytest.mark.parametrize(
    ("arguments", "mode"),
    [
        ({"domain_filter": "light"}, "domain_listing"),
        ({"state_filter": "on"}, "state_listing"),
        ({"area_filter": "Kitchen"}, "area_only"),
        ({"area_filter": " Kitchen "}, "area_only"),
        ({"query": "kitchen", "area_filter": "Kitchen"}, "area_filtered_query"),
    ],
)
async def test_unified_component_serves_listings_and_locations(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    mode: str,
) -> None:
    _setup_visibility_disabled(tmp_path, monkeypatch)
    kitchen = area_registry.async_create("Kitchen")
    entry = entity_registry.async_get_or_create(
        "light", "test", "kitchen-light", suggested_object_id="kitchen"
    )
    entity_registry.async_update_entity(entry.entity_id, area_id=kitchen.id)
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen"})

    response = await _build_ha_search(component_core_client)(**arguments)

    assert response["entities"][0]["entity_id"] == "light.kitchen"
    assert response["search_type"] == mode
    assert not any("legacy path" in w for w in response.get("warnings", []))
    if "area_filter" in arguments:
        assert response["area_filter"] == arguments["area_filter"].strip()
        assert response["area_names"] == ["Kitchen"]


async def test_explicit_config_pin_explains_skipped_entities(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_visibility_disabled(tmp_path, monkeypatch)
    assert await async_setup_component(hass, "automation", {})

    response = await _build_ha_search(component_core_client)(
        query="sensi", domain_filter="switch", search_types=["automation"]
    )

    assert response["entities"] == []
    assert any("entity search skipped" in warning for warning in response["warnings"])


async def test_unified_component_large_dashboard_window_is_serviceable(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_visibility_disabled(tmp_path, monkeypatch)
    assert await async_setup_component(hass, "automation", {})
    assert await async_setup_component(hass, "lovelace", {})

    response = await _build_ha_search(component_core_client)(
        query="kitchen",
        search_types=["automation", "dashboard"],
        limit=100,
        offset=600,
        exact_match=True,
        include_hidden=True,
    )

    # A window past the 500-result ceiling is still served, with no
    # component-failure fallback warning and no partial dashboards leg.
    assert response["success"] is True
    assert not response.get("partial")
    assert not any("legacy path" in w for w in response.get("warnings", []))
