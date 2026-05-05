"""
E2E tests for ha_set_entity device_class (Show As) and per-domain options round-trips.
"""

import logging

import pytest

from tests.src.e2e.utilities.assertions import assert_mcp_success

logger = logging.getLogger(__name__)


async def _delete_template_helper(mcp_client, entity_id: str) -> None:
    """Best-effort cleanup for a template helper (no built-in cleaner support)."""
    try:
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"target": entity_id, "helper_type": "template", "confirm": True},
        )
    except Exception as e:  # pragma: no cover - cleanup best-effort
        logger.warning(f"Cleanup of {entity_id} failed: {e}")


@pytest.mark.asyncio
@pytest.mark.registry
class TestShowAs:
    """Round-trip ha_set_entity / ha_get_entity for device_class + options."""

    async def test_set_show_as_device_class(self, mcp_client):
        """ha_set_entity(device_class='window') lands on the top-level field
        (the slot HA's UI Show As dropdown writes); ha_get_entity reads it back.
        """
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "template",
                "name": "E2E Show As Test",
                "config": {
                    "next_step_id": "binary_sensor",
                    "state": "{{ true }}",
                },
            },
        )
        data = assert_mcp_success(create_result, "Create template binary_sensor")
        entity_ids = data.get("entity_ids") or []
        assert entity_ids, f"helper response missing entity_ids: {data}"
        entity_id = entity_ids[0]

        try:
            set_result = await mcp_client.call_tool(
                "ha_set_entity",
                {"entity_id": entity_id, "device_class": "window"},
            )
            set_data = assert_mcp_success(set_result, "Set Show As=window")
            assert set_data["entity_entry"]["device_class"] == "window"
            assert "device_class='window'" in str(set_data["updates"])

            get_result = await mcp_client.call_tool(
                "ha_get_entity", {"entity_id": entity_id}
            )
            get_data = assert_mcp_success(get_result, "Read back device_class")
            assert get_data["entity_entry"]["device_class"] == "window"

            cleared = await mcp_client.call_tool(
                "ha_set_entity",
                {"entity_id": entity_id, "device_class": ""},
            )
            cleared_data = assert_mcp_success(cleared, "Clear Show As")
            assert cleared_data["entity_entry"]["device_class"] is None
        finally:
            await _delete_template_helper(mcp_client, entity_id)

    async def test_set_per_domain_options_display_precision(self, mcp_client):
        """ha_set_entity(options={'sensor': {'display_precision': 2}}) splits into
        an options_domain+options paired WS update and persists on the registry entry.
        """
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "template",
                "name": "E2E Options Test",
                "config": {
                    "next_step_id": "sensor",
                    "state": "{{ 1.234 }}",
                    "unit_of_measurement": "kWh",
                },
            },
        )
        data = assert_mcp_success(create_result, "Create template sensor")
        entity_ids = data.get("entity_ids") or []
        assert entity_ids, f"helper response missing entity_ids: {data}"
        entity_id = entity_ids[0]

        try:
            set_result = await mcp_client.call_tool(
                "ha_set_entity",
                {
                    "entity_id": entity_id,
                    "options": {"sensor": {"display_precision": 2}},
                },
            )
            set_data = assert_mcp_success(set_result, "Set sensor display_precision")
            sensor_opts = set_data["entity_entry"]["options"].get("sensor", {})
            assert sensor_opts.get("display_precision") == 2

            get_result = await mcp_client.call_tool(
                "ha_get_entity", {"entity_id": entity_id}
            )
            get_data = assert_mcp_success(get_result, "Read back options")
            assert (
                get_data["entity_entry"]["options"]
                .get("sensor", {})
                .get("display_precision")
                == 2
            )
        finally:
            await _delete_template_helper(mcp_client, entity_id)
