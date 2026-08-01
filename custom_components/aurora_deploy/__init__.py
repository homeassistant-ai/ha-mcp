"""Authenticated, fixed-scope Aurora dashboard deployment adapter."""
from __future__ import annotations

try:
    from homeassistant.core import HomeAssistant
except ModuleNotFoundError:  # pragma: no cover - standalone validation tests
    HomeAssistant = object  # type: ignore[misc,assignment]

from .adapter import DOMAIN, AuroraState, RootView, TransactionView


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the dedicated API without exposing a generic file surface."""
    state = await AuroraState.create(hass)
    hass.data[DOMAIN] = state
    hass.http.register_view(RootView(hass, state))
    hass.http.register_view(TransactionView(hass, state))
    return True


async def async_unload(hass: HomeAssistant) -> bool:
    """Home Assistant cannot unregister HTTP views during a running session."""
    hass.data.pop(DOMAIN, None)
    return True
