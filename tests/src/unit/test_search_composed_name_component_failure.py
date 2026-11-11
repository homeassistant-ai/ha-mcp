"""A component search that fails is not credited with the composed name.

When a current component's search errors, the legacy path answers and its
records carry no ``name``. A caller who asked for that field still has to be
told why it is missing.

The server's own client runs on a real ``hass`` with the component's commands
registered; Core answers the capability probe and the legacy reads, and the
component's entity search raises Core's own error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient

from ._search_tool import _build_ha_search, _setup_visibility_disabled


@pytest.mark.asyncio
async def test_a_requested_name_is_explained_when_a_current_component_fails(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_visibility_disabled(tmp_path, monkeypatch)
    # ``config`` serves the registry reads of the legacy path.
    assert await async_setup_component(hass, "config", {})
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen"})
    hass.states.async_set(
        "sensor.kitchen_temp", "21", {"friendly_name": "Kitchen Temp"}
    )

    def _fails(*args: Any, **kwargs: Any) -> None:
        raise HomeAssistantError("boom")

    monkeypatch.setattr(
        "custom_components.ha_mcp_tools.websocket_api.search._search_entities",
        _fails,
    )
    ha_search = _build_ha_search(component_core_client)

    resp = await ha_search(query="kitchen", result_fields=["entity_id", "name"])

    assert resp["entities"]
    assert all("name" not in row for row in resp["entities"])
    assert any("composed name" in w for w in resp["warnings"])
