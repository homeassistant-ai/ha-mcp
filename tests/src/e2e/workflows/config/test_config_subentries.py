"""E2E coverage for config subentry create/list/delete.

The test uses Forecast.Solar because it is an in-tree HA integration with a
deterministic ``plane`` subentry flow. No external service is contacted by the
flow itself; a syntactically valid fake API key is enough to allow multiple
planes.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest

from ...utilities.assertions import assert_mcp_success, safe_call_tool

LOG = logging.getLogger(__name__)


def _find_forecast_plane(
    subentries: list[dict[str, Any]],
    *,
    modules_power: int,
) -> dict[str, Any] | None:
    """Return the Forecast.Solar plane subentry with the expected module power."""
    for subentry in subentries:
        if subentry.get("subentry_type") != "plane":
            continue
        data = subentry.get("data")
        if isinstance(data, dict) and data.get("modules_power") == modules_power:
            return subentry
        title = subentry.get("title")
        if isinstance(title, str) and f"{modules_power}W" in title:
            return subentry
    return None


@pytest.mark.asyncio
@pytest.mark.config
@pytest.mark.slow
@pytest.mark.flaky(reruns=2, reruns_delay=10)
async def test_forecast_solar_config_subentry_create_list_delete(
    mcp_client: Any,
    ha_client: Any,
) -> None:
    """Create, list, and delete a Forecast.Solar plane config subentry.

    Flaky on the inaddon HAOS runner: ``submit_config_flow_step`` for
    the initial entry create has been observed hitting the 30s client
    timeout. Retried up to 3x to absorb the transient; a genuine
    regression will still fail all attempts.
    """
    unique = uuid.uuid4().hex[:8]
    entry_id: str | None = None
    subentry_id: str | None = None
    modules_power = 1200 + int(unique[:2], 16)

    try:
        flow_init = await ha_client.start_config_flow("forecast_solar")
        assert flow_init.get("type") == "form", (
            f"Unexpected forecast_solar flow init shape: {flow_init}"
        )
        flow_done = await ha_client.submit_config_flow_step(
            flow_init["flow_id"],
            {
                "latitude": 52.0,
                "longitude": 5.0,
                "declination": 30,
                "azimuth": 180,
                "modules_power": 1000,
            },
        )
        assert flow_done.get("type") == "create_entry", (
            f"forecast_solar flow did not create an entry: {flow_done}"
        )
        entry_id = flow_done["result"]["entry_id"]

        # Forecast.Solar requires an API key before adding a second plane.
        options_init = await ha_client.start_options_flow(entry_id)
        assert options_init.get("type") == "form", (
            f"Unexpected forecast_solar options flow shape: {options_init}"
        )
        options_done = await ha_client.submit_options_flow_step(
            options_init["flow_id"],
            {"api_key": f"{unique.upper():0<16}"[:16]},
        )
        assert options_done.get("type") == "create_entry", (
            f"forecast_solar options flow did not complete: {options_done}"
        )

        create_raw = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "config_subentry",
                "entry_id": entry_id,
                "subentry_type": "plane",
                "config": {
                    "declination": 35,
                    "azimuth": 190,
                    "modules_power": modules_power,
                },
            },
        )
        create_data = assert_mcp_success(create_raw, "Create forecast_solar plane")
        assert create_data.get("operation") == "created"

        list_raw = await mcp_client.call_tool(
            "ha_get_integration",
            {"entry_id": entry_id, "include_subentries": True},
        )
        list_data = assert_mcp_success(list_raw, "List forecast_solar subentries")
        subentries = list_data.get("subentries")
        assert isinstance(subentries, list), (
            f"Expected subentries list in response: {list_data}"
        )
        created = _find_forecast_plane(subentries, modules_power=modules_power)
        assert created is not None, (
            f"Created plane not found in subentries: {subentries}"
        )
        subentry_id = created.get("subentry_id")
        assert isinstance(subentry_id, str) and subentry_id, (
            f"Created subentry missing subentry_id: {created}"
        )

        delete_raw = await mcp_client.call_tool(
            "ha_remove_helpers_integrations",
            {
                "target": entry_id,
                "helper_type": "config_subentry",
                "subentry_id": subentry_id,
                "confirm": True,
            },
        )
        delete_data = assert_mcp_success(delete_raw, "Delete forecast_solar plane")
        assert delete_data.get("method") == "config_subentry_delete"

        subentry_id = None
        after_raw = await mcp_client.call_tool(
            "ha_get_integration",
            {"entry_id": entry_id, "include_subentries": True},
        )
        after_data = assert_mcp_success(after_raw, "List subentries after delete")
        after_subentries = after_data.get("subentries")
        assert isinstance(after_subentries, list), (
            f"Expected subentries list after delete: {after_data}"
        )
        assert (
            _find_forecast_plane(after_subentries, modules_power=modules_power) is None
        )
    finally:
        if entry_id and subentry_id:
            await safe_call_tool(
                mcp_client,
                "ha_remove_helpers_integrations",
                {
                    "target": entry_id,
                    "helper_type": "config_subentry",
                    "subentry_id": subentry_id,
                    "confirm": True,
                },
            )
        if entry_id:
            await safe_call_tool(
                mcp_client,
                "ha_remove_helpers_integrations",
                {"target": entry_id, "confirm": True},
            )
            LOG.info("Cleaned up forecast_solar entry %s", entry_id)
