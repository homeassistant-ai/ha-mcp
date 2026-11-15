"""Call defaults and Core setup shared by the log tool unit tests."""

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component


def call_kwargs(**overrides: Any) -> dict[str, Any]:
    """Full keyword set for ``LogTools.get_logs`` with sensible defaults."""
    base: dict[str, Any] = {
        "source": "logbook",
        "limit": None,
        "search": None,
        "hours_back": None,
        "entity_id": None,
        "device_id": None,
        "context_id": None,
        "end_time": None,
        "offset": 0,
        "compact": True,
        "level": None,
        "slug": None,
    }
    base.update(overrides)
    return base


async def set_up_core_logs(hass: HomeAssistant) -> None:
    """Core's ``system_log`` and ``logger``, which the ``system`` and ``logger`` sources read."""
    assert await async_setup_component(hass, "system_log", {})
    assert await async_setup_component(hass, "logger", {})
