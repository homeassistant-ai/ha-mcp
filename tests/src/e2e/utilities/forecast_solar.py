"""A Forecast.Solar config entry with one plane, for config-subentry tests.

Forecast.Solar is in-tree and needs no credentials, but Home Assistant sets the
entry up inside the request that creates it, and that setup fetches a forecast
from api.forecast.solar with no timeout of its own (aiohttp's 300 s default
applies). The step therefore waits past that default instead of the client's
30 s, so a slow API delays the test instead of failing it.

Core 2026.11 split the flow into a sources step and a plane step that nests
the coordinates; each step gets the answers its own form asks for, and a
choice left out keeps its default (fixed location, fixed angles).
"""

from __future__ import annotations

from typing import Any

_ANSWERS: dict[str, Any] = {
    "latitude": 52.0,
    "longitude": 5.0,
    "coordinates": {"latitude": 52.0, "longitude": 5.0},
    "declination": 30,
    "azimuth": 180,
    "modules_power": 1000,
}
_SETUP_WAIT_SECONDS = 330
_MAX_STEPS = 4


async def create_forecast_solar_entry(ha_client: Any) -> dict[str, Any]:
    """Run the user flow; return its final step (``create_entry`` on success)."""
    step = await ha_client.start_config_flow("forecast_solar")
    for _ in range(_MAX_STEPS):
        if step.get("type") != "form":
            return step
        fields = [f["name"] for f in step.get("data_schema") or []]
        # submit_config_flow_step's request with a per-call httpx timeout.
        step = await ha_client._request(
            "POST",
            f"/config/config_entries/flow/{step['flow_id']}",
            json={name: _ANSWERS[name] for name in fields if name in _ANSWERS},
            timeout=_SETUP_WAIT_SECONDS,
        )
    raise AssertionError(f"forecast_solar flow did not finish: {step}")
