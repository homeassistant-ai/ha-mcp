"""Home Assistant reporting a time zone this server cannot resolve."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.tools import response_helpers


async def unresolvable_zone(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Home Assistant reports America/New_York, which this server's tzdata lacks."""
    await hass.config.async_set_time_zone("America/New_York")

    def zone(key: str) -> ZoneInfo:
        if key == "America/New_York":
            raise ZoneInfoNotFoundError(key)
        return ZoneInfo(key)

    monkeypatch.setattr(response_helpers, "ZoneInfo", zone)
