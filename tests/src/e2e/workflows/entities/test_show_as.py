"""
E2E tests for ha_set_entity device_class (Show As) and per-domain options round-trips.
"""

import logging

import pytest

from tests.src.e2e.utilities.assertions import assert_mcp_success

logger = logging.getLogger(__name__)

ORPHAN_NAME_PREFIXES = (
    "e2e_show_as_test",
    "e2e_options_test",
    "e2e_multi_options_test",
)


async def _delete_template_helper(mcp_client, entity_id: str) -> None:
    """Best-effort cleanup for a template helper (no built-in cleaner support)."""
    try:
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"target": entity_id, "helper_type": "template", "confirm": True},
        )
    except Exception as e:  # pragma: no cover - cleanup best-effort
        logger.warning(f"Cleanup of {entity_id} failed: {e}")


@pytest.fixture
async def template_orphan_sweep(mcp_client):
    """Remove any leftover template helpers from prior failed runs of this file.

    Template helpers go through HA's config-flow wizard; if a previous run
    crashed mid-flow the helper can be left behind, polluting later runs.
    Yields nothing — purely a teardown-style sweep run before each test.
    """

    async def sweep():
        for prefix in ORPHAN_NAME_PREFIXES:
            for domain in ("binary_sensor", "sensor"):
                eid = f"{domain}.{prefix}"
                try:
                    res = await mcp_client.call_tool(
                        "ha_get_entity", {"entity_id": eid}
                    )
                    parsed = res if isinstance(res, dict) else {}
                    if parsed.get("success"):
                        await _delete_template_helper(mcp_client, eid)
                except Exception:
                    # ha_get_entity raises ToolError when the entity is missing —
                    # that's the expected state, swallow and move on.
                    pass

    await sweep()
    yield
    await sweep()


@pytest.mark.asyncio
@pytest.mark.registry
@pytest.mark.usefixtures("template_orphan_sweep")
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
                "name": "e2e_show_as_test",
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
                "name": "e2e_options_test",
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

    async def test_set_multi_domain_options_round_trip(self, mcp_client):
        """Multi-domain options must be applied as separate WS calls and the final
        registry entry must reflect every domain — exercises the loop end-to-end.
        """
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "template",
                "name": "e2e_multi_options_test",
                "config": {
                    "next_step_id": "sensor",
                    "state": "{{ 9.87 }}",
                    "unit_of_measurement": "kWh",
                },
            },
        )
        data = assert_mcp_success(create_result, "Create template sensor")
        entity_ids = data.get("entity_ids") or []
        assert entity_ids, f"helper response missing entity_ids: {data}"
        entity_id = entity_ids[0]

        try:
            await mcp_client.call_tool(
                "ha_set_entity",
                {
                    "entity_id": entity_id,
                    "options": {
                        "sensor": {"display_precision": 1},
                        "conversation": {"should_expose": False},
                    },
                },
            )
            get_result = await mcp_client.call_tool(
                "ha_get_entity", {"entity_id": entity_id}
            )
            get_data = assert_mcp_success(get_result, "Read back multi-domain options")
            opts = get_data["entity_entry"]["options"]
            assert opts.get("sensor", {}).get("display_precision") == 1
            assert opts.get("conversation", {}).get("should_expose") is False
        finally:
            await _delete_template_helper(mcp_client, entity_id)
