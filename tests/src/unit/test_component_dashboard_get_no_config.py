"""The component's dashboard get for a dashboard with no saved config (#2632)."""

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ._component_core import ws_result


async def test_get_dashboard_without_saved_config_is_not_missing(
    hass: HomeAssistant, component_ws: Any
) -> None:
    """A dashboard Core loads with ``ConfigNotFound`` exists; it has no saved
    config yet, so the get must not report it missing."""
    assert await async_setup_component(hass, "lovelace", {})
    await ws_result(
        component_ws,
        "lovelace/dashboards/create",
        url_path="fresh-dash",
        title="Fresh",
        mode="storage",
    )
    res = await ws_result(
        component_ws, "ha_mcp_tools/dashboards", mode="get", url_path="fresh-dash"
    )
    assert res["status"] == "no_config"
    assert res["config"] is None
