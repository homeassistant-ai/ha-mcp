"""Automation Traces E2E Tests

Tests the automation trace functionality: Create automation → Trigger → Get traces
Verifies that ha_get_automation_traces returns non-empty traces after automation runs.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import pytest

from ...utilities.assertions import (
    MCPAssertions,
    parse_mcp_result,
)
from ...utilities.entity_finders import find_test_light_entity
from ...utilities.wait_helpers import wait_for_condition, wait_for_tool_result
from .conftest import create_trace_automation, create_trace_script

logger = logging.getLogger(__name__)


@pytest.mark.automation
@pytest.mark.cleanup
class TestAutomationTraces:
    """Test automation trace retrieval functionality."""

    async def test_template_trace_with_null_trigger_states(
        self,
        mcp_client: Any,
        trace_entity_cleanup: list[tuple[str, str]],
        test_data_factory: Any,
    ) -> None:
        """Return time-driven template traces whose HA states are null."""
        suffix = uuid.uuid4().hex[:8]
        trigger_after = int(time.time()) + 45
        create_config = test_data_factory.automation_config(
            f"Template Null State Trace {suffix}",
            trigger=[
                {
                    "platform": "template",
                    "value_template": (
                        f"{{{{ as_timestamp(now()) >= {trigger_after} }}}}"
                    ),
                }
            ],
            action=[{"delay": {"seconds": 0}}],
            initial_state=True,
        )

        automation_id = await create_trace_automation(
            mcp_client, trace_entity_cleanup, create_config
        )
        async with MCPAssertions(mcp_client) as mcp:
            traces_data = await wait_for_tool_result(
                mcp_client,
                tool_name="ha_get_automation_traces",
                arguments={"entity_id": automation_id},
                predicate=lambda data: data.get("trace_count", 0) > 0,
                timeout=120,
                poll_interval=1,
                description="time-driven template automation trace",
            )
            run_id = traces_data["traces"][0]["run_id"]
            detailed_data = await mcp.call_tool_success(
                "ha_get_automation_traces",
                {"entity_id": automation_id, "run_id": run_id},
            )

        trigger_vars = detailed_data["trace"]["trigger/0"][0]["changed_variables"][
            "trigger"
        ]
        assert trigger_vars["platform"] == "template"
        assert trigger_vars["from_state"] is None
        assert trigger_vars["to_state"] is None

    async def test_automation_trace_after_trigger(
        self, mcp_client, trace_entity_cleanup, test_data_factory
    ):
        """
        Test: Create automation → Trigger → Get traces → Verify non-empty

        This test validates that:
        1. An automation can be created
        2. It can be triggered manually
        3. Traces are recorded and retrievable
        4. Traces contain expected fields (run_id, timestamp, state)
        """
        suffix = uuid.uuid4().hex[:8]

        # 1. Find a test light entity
        test_light = await find_test_light_entity(mcp_client)
        logger.info(f"Using test light entity: {test_light}")

        # 2. Create an automation that can be manually triggered
        automation_name = f"Trace Test Automation E2E {suffix}"
        create_config = test_data_factory.automation_config(
            automation_name,
            # Use event trigger so we can manually trigger it
            trigger=[{"platform": "event", "event_type": f"test_trace_event_{suffix}"}],
            action=[{"service": "light.turn_on", "target": {"entity_id": test_light}}],
        )

        automation_id = await create_trace_automation(
            mcp_client, trace_entity_cleanup, create_config
        )
        logger.info(f"Created automation: {automation_id}")

        # A reused config id can still carry runs from an earlier test, so the
        # assertion below has to prove the selected run is new.
        before = await MCPAssertions(mcp_client).call_tool_success(
            "ha_get_automation_traces", {"entity_id": automation_id}
        )
        runs_before = {trace.get("run_id") for trace in before.get("traces", [])}

        # 3. Trigger the automation manually using automation.trigger service
        await MCPAssertions(mcp_client).call_tool_success(
            "ha_call_service",
            {
                "domain": "automation",
                "service": "trigger",
                "entity_id": automation_id,
            },
        )
        logger.info("Triggered automation")

        # Wait for the new run to be recorded
        async def check_automation_traces():
            result = await mcp_client.call_tool(
                "ha_get_automation_traces",
                {"entity_id": automation_id},
            )
            data = parse_mcp_result(result)
            return any(
                trace["run_id"] not in runs_before for trace in data.get("traces", [])
            )

        logger.info("Waiting for automation trace to be recorded...")
        new_trace_appeared = await wait_for_condition(
            check_automation_traces,
            timeout=15,
            poll_interval=0.5,
            condition_name="new automation trace to be recorded",
        )

        assert new_trace_appeared, "New automation trace did not appear within timeout"

        # 4. Get traces for the automation
        traces_data = await MCPAssertions(mcp_client).call_tool_success(
            "ha_get_automation_traces",
            {"entity_id": automation_id},
        )

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

        # 6. Verify trace structure for the run created by this trigger
        first_trace = next(
            trace for trace in traces if trace["run_id"] not in runs_before
        )
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
            detailed_data = await MCPAssertions(mcp_client).call_tool_success(
                "ha_get_automation_traces",
                {"entity_id": automation_id, "run_id": run_id},
            )

            assert detailed_data.get("success") is True
            assert detailed_data.get("run_id") == run_id

            # Verify Core's raw step dict is passed through unchanged.
            assert "trace" in detailed_data, (
                "Detailed trace should contain Core's raw step trace"
            )
            actions = [
                step
                for path, steps in detailed_data["trace"].items()
                if path.startswith(("action/", "sequence/"))
                for step in steps
            ]
            assert actions, "raw action steps should not be empty"

            # Check for path property to prove the raw step dict was preserved.
            first_action = actions[0]
            assert "path" in first_action, "Action step should contain 'path'"

            logger.info(
                f"Detailed trace verified: Found {len(actions)} raw action steps"
            )

    async def test_trace_storage_disabled_diagnostics(
        self, mcp_client, trace_entity_cleanup, test_data_factory
    ):
        """stored_traces=0 reports disabled storage, not a generic empty result."""
        suffix = uuid.uuid4().hex[:8]
        automation_name = f"Trace Storage Disabled E2E {suffix}"
        create_config = test_data_factory.automation_config(
            automation_name,
            trigger=[
                {
                    "platform": "event",
                    "event_type": f"trace_storage_disabled_e2e_{suffix}",
                }
            ],
            action=[{"delay": {"seconds": 0}}],
            trace={"stored_traces": 0},
            initial_state=True,
        )

        mcp = MCPAssertions(mcp_client)
        automation_id = await create_trace_automation(
            mcp_client, trace_entity_cleanup, create_config
        )

        await mcp.call_tool_success(
            "ha_call_service",
            {
                "domain": "automation",
                "service": "trigger",
                "entity_id": automation_id,
            },
        )

        traces_data = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_automation_traces",
            arguments={"entity_id": automation_id},
            predicate=lambda data: (
                data.get("diagnostics", {}).get("trace_storage_enabled") is False
            ),
            timeout=15,
            poll_interval=0.5,
            description="trace storage disabled diagnostic",
        )

        assert traces_data["trace_count"] == 0
        assert traces_data["diagnostics"]["trace_storage_enabled"] is False
        assert "trace storage is disabled" in (
            traces_data["diagnostics"]["suggestion"].lower()
        )

    async def test_script_traces(
        self, mcp_client, trace_entity_cleanup, test_data_factory
    ):
        """
        Test: Create script → Run → Get traces → Verify non-empty

        This test validates that trace retrieval also works for scripts.
        """
        suffix = uuid.uuid4().hex[:8]

        # 1. Find a test light entity
        test_light = await find_test_light_entity(mcp_client)

        # 2. Create a simple script
        script_name = f"Trace Test Script E2E {suffix}"
        script_id_base = f"trace_test_script_e2e_{suffix}"
        script_config = test_data_factory.script_config(
            script_name,
            sequence=[
                {"service": "light.turn_on", "target": {"entity_id": test_light}}
            ],
        )

        mcp = MCPAssertions(mcp_client)
        script_entity_id = await create_trace_script(
            mcp_client, trace_entity_cleanup, script_id_base, script_config
        )
        logger.info(f"Created script: {script_entity_id}")

        before = await mcp.call_tool_success(
            "ha_get_automation_traces", {"entity_id": script_entity_id}
        )
        runs_before = {trace.get("run_id") for trace in before.get("traces", [])}

        # 3. Run the script
        await mcp.call_tool_success(
            "ha_call_service",
            {
                "domain": "script",
                "service": "turn_on",
                "entity_id": script_entity_id,
            },
        )
        logger.info("Script executed")

        # Wait for trace to be recorded
        async def check_traces():
            result = await mcp_client.call_tool(
                "ha_get_automation_traces",
                {"entity_id": script_entity_id},
            )
            data = parse_mcp_result(result)
            return any(
                trace["run_id"] not in runs_before for trace in data.get("traces", [])
            )

        logger.info("Waiting for script trace to be recorded...")
        trace_appeared = await wait_for_condition(
            check_traces,
            timeout=15,  # Increased timeout for ARM compatibility
            poll_interval=0.5,
            condition_name="script trace to be recorded",
        )

        assert trace_appeared, "Script trace did not appear within timeout"

        # 4. Get traces for the script
        traces_data = await MCPAssertions(mcp_client).call_tool_success(
            "ha_get_automation_traces",
            {"entity_id": script_entity_id},
        )

        assert traces_data.get("success") is True
        trace_count = traces_data.get("trace_count", 0)

        logger.info(f"Script traces retrieved: {trace_count}")

        assert trace_count > 0, (
            f"Expected at least 1 trace after running script, got {trace_count}"
        )

    async def test_script_trace_storage_disabled_diagnostics(
        self, mcp_client, trace_entity_cleanup, test_data_factory
    ):
        """A script with stored_traces=0 reports disabled storage, not idle."""
        suffix = uuid.uuid4().hex[:8]
        test_light = await find_test_light_entity(mcp_client)
        script_id_base = f"trace_storage_disabled_script_e2e_{suffix}"
        script_config = test_data_factory.script_config(
            f"Trace Storage Disabled Script E2E {suffix}",
            sequence=[
                {"service": "light.turn_on", "target": {"entity_id": test_light}}
            ],
            trace={"stored_traces": 0},
        )

        mcp = MCPAssertions(mcp_client)
        script_entity_id = await create_trace_script(
            mcp_client, trace_entity_cleanup, script_id_base, script_config
        )

        await mcp.call_tool_success(
            "ha_call_service",
            {
                "domain": "script",
                "service": "turn_on",
                "entity_id": script_entity_id,
            },
        )

        traces_data = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_automation_traces",
            arguments={"entity_id": script_entity_id},
            predicate=lambda data: (
                data.get("diagnostics", {}).get("trace_storage_enabled") is False
            ),
            timeout=15,
            poll_interval=0.5,
            description="script trace storage disabled diagnostic",
        )

        assert traces_data["trace_count"] == 0
        assert traces_data["diagnostics"]["entity_enabled"] is True
        assert traces_data["diagnostics"]["trace_storage_enabled"] is False
        assert "trace storage is disabled for this script" in (
            traces_data["diagnostics"]["suggestion"].lower()
        )
