"""
E2E tests for ha_set_entity device_class (Show As) and per-domain options round-trips.
"""

import logging

import pytest
from fastmcp.exceptions import ToolError

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
                except ToolError:
                    # ha_get_entity raises ToolError when the entity is missing —
                    # that's the expected state for a clean run. Anything else
                    # (network, auth) we want to bubble.
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


@pytest.mark.asyncio
@pytest.mark.registry
class TestShowAsOnIntegrationEntity:
    """Round-trip on an INTEGRATION-PROVIDED entity (a YAML-configured
    template sensor), not a helper.

    Helpers can already be (re)configured with a device_class via
    ha_config_set_helper for the template binary_sensor / template sensor
    subtypes — so the helper-based tests above don't *literally* prove the
    new ha_set_entity capability for the original-issue case (an integration
    entity like envisalink's binary_sensor.alarm_zone_10). This test does:
    sensor.demo_temperature is declared in
    tests/initial_test_state/configuration.yaml under a `template:` block
    (the bare `demo:` line above it is a no-op in modern HA — the demo
    integration is config-flow-only). YAML-template entities are owned by
    the template integration and have NO config entry, so
    ha_config_set_helper (which routes through config_entries / the
    config-flow API) cannot manage them. Z-Wave / Zigbee / envisalink user
    paths go through the same `config/entity_registry/update` WS call we're
    exercising here, so this is a legitimate stand-in.
    """

    TARGET = "sensor.demo_temperature"

    async def test_set_show_as_on_integration_provided_sensor(self, mcp_client):
        pre = await mcp_client.call_tool("ha_get_entity", {"entity_id": self.TARGET})
        pre_data = assert_mcp_success(pre, "Read pre-state")
        original_override = pre_data["entity_entry"]["device_class"]
        original_device_class = pre_data["entity_entry"]["original_device_class"]
        # Capture the entity's pre-test sensor sub-options so the restore can
        # put them back exactly, rather than blindly clearing the dict.
        original_sensor_options = pre_data["entity_entry"]["options"].get("sensor", {})
        # The YAML fixture declares device_class: temperature — guarantee
        # the override slot and the integration's own slot are distinct so
        # we know the test is exercising what we think it is.
        assert original_device_class == "temperature"

        try:
            # Override Show As to a different device class
            set_result = await mcp_client.call_tool(
                "ha_set_entity",
                {"entity_id": self.TARGET, "device_class": "humidity"},
            )
            set_data = assert_mcp_success(set_result, "Set Show As=humidity")
            assert set_data["entity_entry"]["device_class"] == "humidity"
            # original_device_class must NOT be touched by the override
            assert set_data["entity_entry"]["original_device_class"] == "temperature"

            get_result = await mcp_client.call_tool(
                "ha_get_entity", {"entity_id": self.TARGET}
            )
            get_data = assert_mcp_success(get_result, "Read overridden device_class")
            assert get_data["entity_entry"]["device_class"] == "humidity"

            # Set per-domain options on the same integration entity (proves
            # ha_config_set_helper isn't even a possible alternative path here)
            await mcp_client.call_tool(
                "ha_set_entity",
                {
                    "entity_id": self.TARGET,
                    "options": {"sensor": {"display_precision": 3}},
                },
            )
            opts_result = await mcp_client.call_tool(
                "ha_get_entity", {"entity_id": self.TARGET}
            )
            opts_data = assert_mcp_success(opts_result, "Read options")
            assert (
                opts_data["entity_entry"]["options"]
                .get("sensor", {})
                .get("display_precision")
                == 3
            )
        finally:
            # Restore the override slot to its pre-test value (None or whatever
            # it was) so we don't leak state to other tests sharing the demo
            # entity inside this test container.
            restore_dc = "" if original_override is None else original_override
            await mcp_client.call_tool(
                "ha_set_entity",
                {"entity_id": self.TARGET, "device_class": restore_dc},
            )
            # Restore the sensor sub-options to their pre-test value so any
            # demo-platform default we overwrote is put back intact. Skip the
            # round-trip when there was nothing captured — sending an empty
            # sub-dict would otherwise emit a no-op options={"sensor": {}} call.
            if original_sensor_options:
                await mcp_client.call_tool(
                    "ha_set_entity",
                    {
                        "entity_id": self.TARGET,
                        "options": {"sensor": dict(original_sensor_options)},
                    },
                )
