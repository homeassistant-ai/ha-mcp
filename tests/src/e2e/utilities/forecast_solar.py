"""A Forecast.Solar config entry with one plane, for config-subentry tests.

Forecast.Solar is in-tree and needs no credentials, but Home Assistant sets the
entry up inside the request that creates it, and that setup fetches a forecast
from api.forecast.solar with no timeout of its own (aiohttp's 300 s default
applies). The step therefore waits past that default instead of the client's
30 s, so a slow API delays the test instead of failing it.
"""

from __future__ import annotations

from typing import Any

_PLANE = {
    "latitude": 52.0,
    "longitude": 5.0,
    "declination": 30,
    "azimuth": 180,
    "modules_power": 1000,
}
_SETUP_WAIT_SECONDS = 330


async def create_forecast_solar_entry(ha_client: Any) -> dict[str, Any]:
    """Run the user flow; return its final step (``create_entry`` on success)."""
    flow = await ha_client.start_config_flow("forecast_solar")
    assert flow.get("type") == "form", f"Unexpected forecast_solar flow: {flow}"
    # submit_config_flow_step's request with a per-call httpx timeout.
    return await ha_client._request(
        "POST",
        f"/config/config_entries/flow/{flow['flow_id']}",
        json=dict(_PLANE),
        timeout=_SETUP_WAIT_SECONDS,
    )
