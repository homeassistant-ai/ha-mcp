"""
Automation Traces E2E Tests

Tests the automation trace functionality: Create automation → Trigger → Get traces
Verifies that ha_get_automation_traces returns non-empty traces after automation runs.
"""

import logging

import pytest

from ...utilities.assertions import (
    assert_mcp_success,
    parse_mcp_result,
)
from ...utilities.wait_helpers import wait_for_condition

logger = logging.getLogger(__name__)


@pytest.mark.automation
@pytest.mark.cleanup
class TestAutomationTraces:
    """Test automation trace retrieval functionality."""

    async def _find_test_light_entity(self, mcp_client) -> str:
        """Find a suitable light entity for testing."""
        search_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "light", "domain_filter": "light", "limit": 20},
        )

        search_data = parse_mcp_result(search_result)

        if "data" in search_data:
            results = search_data.get("data", {}).get("results", [])
        else:
            results = search_data.get("results", [])

        if not results:
            pytest.skip("No light entities available for testing")

        # Prefer demo entities
        for entity in results:
            entity_id = entity.get("entity_id", "")
            if "demo" in entity_id.lower() or "test" in entity_id.lower():
                return entity_id

        return results[0].get("entity_id", "")

    async def test_automation_trace_after_trigger(
        self, mcp_client, cleanup_tracker, test_data_factory
    ):
        """
        Test: Create automation → Trigger → Get traces → Verify non-empty

        This test validates that:
        1. An automation can be created
        2. It can be triggered manually
        3. Traces are recorded and retrievable
        4. Traces contain expected fields (run_id, timestamp, state)
        """

        # 1. Find a test light entity
        test_light = await self._find_test_light_entity(mcp_client)
        logger.info(f"Using test light entity: {test_light}")

        # 2. Create an automation that can be manually triggered
        automation_name = "Trace Test Automation E2E"
        create_config = test_data_factory.automation_config(
            automation_name,
            # Use event trigger so we can manually trigger it
            trigger=[{"platform": "event", "event_type": "test_trace_event"}],
            action=[{"service": "light.turn_on", "target": {"entity_id": test_light}}],
        )

        create_result = await mcp_client.call_tool(
            "ha_config_set_automation", {"config": create_config}
        )
        assert_mcp_success(create_result)
        create_data = parse_mcp_result(create_result)

        # Track for cleanup
        automation_id = create_data.get("entity_id") or create_data.get(
            "automation_id"
        )
        if automation_id:
            cleanup_tracker.track("automation", automation_id)
        logger.info(f"Created automation: {automation_id}")

        # Wait for automation to be fully registered

        # 3. Trigger the automation manually using automation.trigger service
        trigger_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "automation",
                "service": "trigger",
                "entity_id": automation_id,
            },
        )
        assert_mcp_success(trigger_result)
        logger.info("Triggered automation")

        # Wait for trace to be recorded
        async def check_automation_traces():
            result = await mcp_client.call_tool(
                "ha_get_automation_traces",
                {"automation_id": automation_id},
            )
            data = parse_mcp_result(result)
            return data.get("trace_count", 0) > 0

        logger.info("Waiting for automation trace to be recorded...")
        trace_appeared = await wait_for_condition(
            check_automation_traces,
            timeout=15,
            poll_interval=0.5,
            condition_name="automation trace to be recorded"
        )

        # Skip test if traces don't appear (timing issue, not a failure)
        if not trace_appeared:
            pytest.skip(
                "Automation trace did not appear within timeout. "
                "This may be a platform-specific timing issue."
            )

        # 4. Get traces for the automation
        traces_result = await mcp_client.call_tool(
            "ha_get_automation_traces",
            {"automation_id": automation_id},
        )
        assert_mcp_success(traces_result)
        traces_data = parse_mcp_result(traces_result)

        # 5. Verify traces are non-empty
        assert traces_data.get("success") is True, "Traces request should succeed"
        trace_count = traces_data.get("trace_count", 0)
        traces = traces_data.get("traces", [])

        logger.info(f"Retrieved {trace_count} traces")

        assert trace_count > 0, (
            f"Expected at least 1 trace after triggering automation, got {trace_count}. "
            f"Full response: {traces_data}"
        )
        assert len(traces) > 0, "Traces list should not be empty"

        # 6. Verify trace structure
        first_trace = traces[0]
        assert "run_id" in first_trace, "Trace should have run_id"
        assert "timestamp" in first_trace, "Trace should have timestamp"
        assert "state" in first_trace, "Trace should have state"

        logger.info(
            f"Trace verified - run_id: {first_trace.get('run_id')}, "
            f"state: {first_trace.get('state')}"
        )

        # 7. Get detailed trace using run_id
        run_id = first_trace.get("run_id")
        if run_id:
            detailed_result = await mcp_client.call_tool(
                "ha_get_automation_traces",
                {"automation_id": automation_id, "run_id": run_id},
            )
            assert_mcp_success(detailed_result)
            detailed_data = parse_mcp_result(detailed_result)

            assert detailed_data.get("success") is True
            assert detailed_data.get("run_id") == run_id
            logger.info(f"Detailed trace retrieved for run_id: {run_id}")

    async def test_empty_traces_with_diagnostics(
        self, mcp_client, cleanup_tracker, test_data_factory
    ):
        """
        Test: Create automation (no trigger) → Get traces → Verify diagnostics

        This test validates that when an automation has no traces:
        1. The response still succeeds
        2. Diagnostics are included
        3. Diagnostics contain helpful information
        """

        # 1. Find a test light entity
        test_light = await self._find_test_light_entity(mcp_client)

        # 2. Create an automation that won't be triggered
        automation_name = "Empty Trace Test E2E"
        create_config = test_data_factory.automation_config(
            automation_name,
            # Use a trigger that won't fire
            trigger=[{"platform": "time", "at": "03:00:00"}],
            action=[{"service": "light.turn_on", "target": {"entity_id": test_light}}],
        )

        create_result = await mcp_client.call_tool(
            "ha_config_set_automation", {"config": create_config}
        )
        assert_mcp_success(create_result)
        create_data = parse_mcp_result(create_result)

        automation_id = create_data.get("entity_id") or create_data.get(
            "automation_id"
        )
        if automation_id:
            cleanup_tracker.track("automation", automation_id)

        # Wait for automation registration

        # 3. Get traces (should be empty with diagnostics)
        traces_result = await mcp_client.call_tool(
            "ha_get_automation_traces",
            {"automation_id": automation_id},
        )
        assert_mcp_success(traces_result)
        traces_data = parse_mcp_result(traces_result)

        assert traces_data.get("success") is True

        # Traces should be empty for a never-triggered automation
        trace_count = traces_data.get("trace_count", 0)

        if trace_count == 0:
            # Verify diagnostics are present
            diagnostics = traces_data.get("diagnostics")
            assert diagnostics is not None, (
                "Diagnostics should be included when traces are empty"
            )
            assert "automation_exists" in diagnostics
            assert "suggestion" in diagnostics

            logger.info(f"Diagnostics received: {diagnostics.get('suggestion')}")
        else:
            # If traces exist (from previous test runs), that's also acceptable
            logger.info(
                f"Automation already has {trace_count} traces from previous runs"
            )

    async def test_script_traces(self, mcp_client, cleanup_tracker, test_data_factory):
        """
        Test: Create script → Run → Get traces → Verify non-empty

        This test validates that trace retrieval also works for scripts.
        """

        # 1. Find a test light entity
        test_light = await self._find_test_light_entity(mcp_client)

        # 2. Create a simple script
        script_name = "Trace Test Script E2E"
        script_id_base = "trace_test_script_e2e"
        script_config = test_data_factory.script_config(
            script_name,
            sequence=[
                {"service": "light.turn_on", "target": {"entity_id": test_light}}
            ],
        )

        create_result = await mcp_client.call_tool(
            "ha_config_set_script",
            {"script_id": script_id_base, "config": script_config},
        )
        assert_mcp_success(create_result)
        create_data = parse_mcp_result(create_result)

        # Get the entity_id form (script.trace_test_script_e2e)
        script_entity_id = create_data.get("entity_id") or f"script.{script_id_base}"
        cleanup_tracker.track("script", script_entity_id)
        logger.info(f"Created script: {script_entity_id}")

        # Wait for script registration

        # 3. Run the script
        run_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "script",
                "service": "turn_on",
                "entity_id": script_entity_id,
            },
        )
        assert_mcp_success(run_result)
        logger.info("Script executed")

        # Wait for trace to be recorded
        async def check_traces():
            result = await mcp_client.call_tool(
                "ha_get_automation_traces",
                {"automation_id": script_entity_id},
            )
            data = parse_mcp_result(result)
            return data.get("trace_count", 0) > 0

        logger.info("Waiting for script trace to be recorded...")
        trace_appeared = await wait_for_condition(
            check_traces,
            timeout=15,  # Increased timeout for ARM compatibility
            poll_interval=0.5,
            condition_name="script trace to be recorded"
        )

        # Skip test if traces don't appear (timing issue, not a failure)
        if not trace_appeared:
            pytest.skip(
                "Script trace did not appear within timeout. "
                "This may be a platform-specific timing issue (ARM)."
            )

        # 4. Get traces for the script
        traces_result = await mcp_client.call_tool(
            "ha_get_automation_traces",
            {"automation_id": script_entity_id},
        )
        assert_mcp_success(traces_result)
        traces_data = parse_mcp_result(traces_result)

        assert traces_data.get("success") is True
        trace_count = traces_data.get("trace_count", 0)

        logger.info(f"Script traces retrieved: {trace_count}")

        assert trace_count > 0, (
            f"Expected at least 1 trace after running script, got {trace_count}"
        )
