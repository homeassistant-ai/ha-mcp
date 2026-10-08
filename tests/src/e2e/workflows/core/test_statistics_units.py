"""Exercise real Core conversion and statistics without a current entity (#2682)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from ha_mcp._vendor.fastmcp import Client
from ha_mcp.client import HomeAssistantClient

from ...utilities.assertions import assert_mcp_success
from ...utilities.topology import component_surface_available
from ...utilities.wait_helpers import wait_for_tool_result


@pytest.mark.asyncio
@pytest.mark.core
@pytest.mark.parametrize(
    "stored,display,unit_class,expected",
    [
        ("MWh", "kWh", "energy", [1000.0, 1250.0, 1500.0]),
        ("%", None, "unitless", [0.01, 0.0125, 0.015]),
        ("kWh", "Wh", None, [1000.0, 1250.0, 1500.0]),
    ],
)
async def test_core_display_conversion_labels_the_converted_values(
    mcp_client: Client,
    ha_client: HomeAssistantClient,
    stored: str,
    display: str | None,
    unit_class: str | None,
    expected: list[float],
) -> None:
    """Conversion changes numeric values and units, never reset timestamps."""
    entity_id = f"sensor.e2e_statistics_{uuid4().hex}"
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(
        days=2
    )
    args = {
        "source": "statistics",
        "entity_ids": entity_id,
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=3)).isoformat(),
        "period": "hour",
    }
    reset = start - timedelta(days=1)
    state_created = False
    try:
        imported = await ha_client.send_websocket_message(
            {
                "type": "recorder/import_statistics",
                "metadata": {
                    "statistic_id": entity_id,
                    "source": "recorder",
                    "name": "E2E energy conversion",
                    "unit_of_measurement": stored,
                    "unit_class": unit_class,
                    "mean_type": 0,
                    "has_sum": True,
                },
                "stats": [
                    {
                        "start": (start + timedelta(hours=i)).isoformat(),
                        "last_reset": reset.isoformat(),
                        "state": value,
                        "sum": value,
                    }
                    for i, value in enumerate((1.0, 1.25, 1.5))
                ],
            }
        )
        assert imported["success"], imported
        # Core acknowledges the queued import before it is committed. Metadata
        # is read before rows, so the first response can contain only the rows.
        ready = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_history",
            arguments=args,
            predicate=lambda d: (
                len(
                    (entity := d.get("data", d).get("entities", [{}])[0]).get(
                        "statistics", []
                    )
                )
                == 3
                and entity.get("statistics_metadata") is not None
            ),
            description="imported recorder statistics and metadata visible",
            timeout=30,
        )
        entity = ready.get("data", ready)["entities"][0]
        assert entity["unit_of_measurement"] == stored, entity
        assert [r["sum"] for r in entity["statistics"]] == [1.0, 1.25, 1.5]

        await ha_client._request(
            "POST",
            f"/states/{entity_id}",
            json={
                "state": "1500",
                "attributes": {
                    "unit_of_measurement": display,
                    "state_class": "total",
                },
            },
        )
        state_created = True
        result = assert_mcp_success(await mcp_client.call_tool("ha_get_history", args))
        entity = result.get("data", result)["entities"][0]
        assert entity["unit_of_measurement"] == display, entity
        assert entity["unit_source"] == "recorder_metadata"
        assert entity["statistics_metadata"]["statistics_unit_of_measurement"] == stored
        assert [r["sum"] for r in entity["statistics"]] == pytest.approx(expected)
        assert entity["statistics"][1]["change"] == pytest.approx(
            expected[1] - expected[0]
        )
        can_recover_reset = unit_class is not None or component_surface_available()
        if can_recover_reset:
            assert [r["last_reset"] for r in entity["statistics"]] == [
                int(reset.timestamp() * 1000)
            ] * 3
        else:
            assert all("last_reset" not in row for row in entity["statistics"])
            assert any(
                "last_reset omitted" in warning for warning in result["warnings"]
            )
        explicit = assert_mcp_success(
            await mcp_client.call_tool(
                "ha_get_history",
                {
                    **args,
                    "statistic_types": ["last_reset"],
                    "core_options": {"units": {unit_class or "energy": display}},
                },
            )
        )
        explicit_entity = explicit.get("data", explicit)["entities"][0]
        if can_recover_reset:
            assert [r["last_reset"] for r in explicit_entity["statistics"]] == [
                int(reset.timestamp() * 1000)
            ] * 3
        else:
            assert all("last_reset" not in row for row in explicit_entity["statistics"])
            assert any(
                "last_reset omitted" in warning for warning in explicit["warnings"]
            )
        if component_surface_available():
            assert explicit_entity["unit_of_measurement"] == display
            assert explicit_entity["unit_source"] == "core_converter"
        else:
            assert explicit_entity["unit_source"] == "unknown"
    finally:
        try:
            if state_created:
                await ha_client._request("DELETE", f"/states/{entity_id}")
        finally:
            cleared = await ha_client.send_websocket_message(
                {
                    "type": "recorder/clear_statistics",
                    "statistic_ids": [entity_id],
                }
            )
            assert cleared["success"], cleared


@pytest.mark.asyncio
@pytest.mark.core
async def test_mixed_energy_cost_and_water_keep_unconverted_metadata(
    mcp_client: Client,
    ha_client: HomeAssistantClient,
) -> None:
    """An energy-only conversion preserves cost resets and unrelated water units."""
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(
        days=2
    )
    reset = start - timedelta(days=1)
    samples = [("kWh", "energy"), ("USD", None), ("m³", "volume"), ("kWh", None)]
    ids = [f"sensor.e2e_mixed_{uuid4().hex}" for _ in samples]
    args = {
        "source": "statistics",
        "entity_ids": ids,
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(hours=1)).isoformat(),
        "period": "hour",
        "statistic_types": ["sum", "last_reset"],
    }
    try:
        for statistic_id, (unit, unit_class) in zip(ids, samples, strict=True):
            imported = await ha_client.send_websocket_message(
                {
                    "type": "recorder/import_statistics",
                    "metadata": {
                        "statistic_id": statistic_id,
                        "source": "recorder",
                        "name": "E2E mixed energy dashboard statistics",
                        "unit_of_measurement": unit,
                        "unit_class": unit_class,
                        "mean_type": 0,
                        "has_sum": True,
                    },
                    "stats": [
                        {
                            "start": start.isoformat(),
                            "last_reset": reset.isoformat(),
                            "state": 55,
                            "sum": 55,
                        }
                    ],
                }
            )
            assert imported["success"], imported
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_history",
            arguments=args,
            predicate=lambda d: (
                len(entities := d.get("data", d).get("entities", [])) == len(ids)
                and all(
                    e.get("statistics") and e.get("statistics_metadata")
                    for e in entities
                )
            ),
            description="mixed statistics rows and metadata committed",
            timeout=30,
        )
        raw = assert_mcp_success(
            await mcp_client.call_tool(
                "ha_get_history",
                {**args, "core_options": {"units": {"energy": "MWh"}}},
            )
        )
        entities = raw.get("data", raw)["entities"]
        for entity, expected_sum in zip(entities, (0.055, 55, 55, 0.055), strict=True):
            assert entity["statistics"][0]["last_reset"] == int(
                reset.timestamp() * 1000
            )
            assert entity["statistics"][0]["sum"] == pytest.approx(expected_sum)
        assert entities[2]["unit_of_measurement"] == "m³"
        if component_surface_available():
            assert [e["unit_of_measurement"] for e in entities] == [
                "MWh",
                "USD",
                "m³",
                "MWh",
            ]
        else:
            assert entities[0]["unit_source"] == "unknown"
    finally:
        cleared = await ha_client.send_websocket_message(
            {
                "type": "recorder/clear_statistics",
                "statistic_ids": ids,
            }
        )
        assert cleared["success"], cleared
