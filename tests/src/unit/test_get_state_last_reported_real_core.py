"""``last_reported`` in ha_get_state on a real ``hass`` (issue #3086).

An entity that keeps reporting an unchanged state moves ``last_reported``
on Core's live ``State`` only; Core's cached serialization keeps the first
value it rendered (home-assistant/core#181392).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools

ENTITY_ID = "sensor.plug_power"

_SHAPES = {"single": ENTITY_ID, "bulk": [ENTITY_ID]}


def _record(result: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = result["data"]
    return data["states"][ENTITY_ID] if "states" in data else data


async def _read_after_a_repeated_report(
    hass: HomeAssistant, client: HomeAssistantClient, shape: str, **kwargs: Any
) -> tuple[dict[str, Any], datetime]:
    tools = SearchTools(client, SmartSearchTools(client=client))
    hass.states.async_set(ENTITY_ID, "0", timestamp=1_800_000_000.0)
    await tools.ha_get_state(entity_id=_SHAPES[shape])
    hass.states.async_set(ENTITY_ID, "0", timestamp=1_800_000_060.0)
    result: dict[str, Any] = await tools.ha_get_state(
        entity_id=_SHAPES[shape], **kwargs
    )
    live = hass.states.get(ENTITY_ID)
    assert live is not None
    return result, live.last_reported


@pytest.mark.parametrize("shape", list(_SHAPES))
async def test_last_reported_follows_an_unchanged_report(
    hass: HomeAssistant, component_core_client: HomeAssistantClient, shape: str
) -> None:
    """A sensor re-reporting the same value must not read as silent for hours."""
    result, live = await _read_after_a_repeated_report(
        hass, component_core_client, shape
    )

    assert datetime.fromisoformat(_record(result)["last_reported"]) == live
    assert "warnings" not in result


@pytest.mark.parametrize("shape", list(_SHAPES))
async def test_without_the_component_a_stale_last_reported_is_named(
    hass: HomeAssistant, core_client: HomeAssistantClient, shape: str
) -> None:
    """Core's REST value can lag the live one; the reply says so and how to fix it."""
    result, _ = await _read_after_a_repeated_report(hass, core_client, shape)

    assert any("last_reported" in w and "ha_mcp_tools" in w for w in result["warnings"])


async def test_without_the_component_no_warning_when_last_reported_is_not_returned(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    result, _ = await _read_after_a_repeated_report(
        hass, core_client, "single", fields=["state"]
    )

    assert "warnings" not in result
