"""The unit environment runs the QuickJS build the component installs."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import card_runtime


async def test_the_dev_group_installs_the_quickjs_the_component_accepts(
    hass: HomeAssistant,
) -> None:
    """A dev pin that drifts from the component's makes its own runtime check
    refuse the installed build, so the card tests would run a QuickJS no user gets."""
    assert await card_runtime.async_ensure_runtime(hass)
