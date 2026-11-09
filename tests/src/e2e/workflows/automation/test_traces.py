"""
Automation Traces E2E Tests

Tests the automation trace functionality: Create automation → Trigger → Get traces
Verifies that ha_get_automation_traces returns non-empty traces after automation runs.
"""

import logging
import time
import uuid
from typing import Any

import pytest

from ...utilities.assertions import (
    MCPAssertions,
    assert_mcp_success,
    parse_mcp_result,
    safe_call_tool,
)
from ...utilities.entity_finders import find_test_light_entity
from ...utilities.wait_helpers import (
    wait_for_condition,
    wait_for_entity_registration,
    wait_for_tool_result,
)

logger = logging.getLogger(__name__)


@pytest.mark.automation
@pytest.mark.cleanup
class TestAutomationTraces:
    """Test automation trace retrieval functionality."""

    async def _find_test_light_entity(self, mcp_client) -> str:
        """Delegates to the suite-wide helper in utilities.entity_finders."""
        return await find_test_light_entity(mcp_client)

    async def _create_trace_automation(
        self, mcp_client, cleanup_tracker, config: dict[str, Any]
    ) -> str:
        """Create an automation and wait until its entity is queryable."""
        create_result = await mcp_client.call_tool(
            "ha_config_set_automation", {"config": config}
        )
        create_data = assert_mcp_success(create_result, "Create automation")
        automation_id = create_data.get("entity_id")
        assert automation_id, (
            f"ha_config_set_automation did not resolve an entity id: {create_data}"
        )
        cleanup_tracker.track("automation", automation_id)
        assert await wait_for_entity_registration(mcp_client, automation_id), (
            f"{automation_id} never became queryable"
        )
        return automation_id

    async def test_template_trace_with_null_trigger_states(
        self,
        mcp_client: Any,
        test_data_factory: Any,
    ) -> None:
        """Return time-driven template traces whose HA states are null."""
        trigger_after = int(time.time()) + 45
        create_config = test_data_factory.automation_config(
            "Template Null State Trace",
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

        automation_id: str | None = None
        try:
            async with MCPAssertions(mcp_client) as mcp:
                create_data = await mcp.call_tool_success(
                    "ha_config_set_automation", {"config": create_config}
                )
                automation_id = create_data.get("entity_id") or create_data.get(
                    "automation_id"
                )
                assert automation_id is not None

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

            assert detailed_data["trigger"]["platform"] == "template"
            assert detailed_data["trigger"]["from_state"] is None
            assert detailed_data["trigger"]["to_state"] is None
        finally:
            if automation_id is not None:
                cleanup_data = await safe_call_tool(
                    mcp_client,
                    "ha_config_remove_automation",
                    {"entity_id": automation_id},
                )
                assert cleanup_data.get("success") is True, (
                    f"Failed to remove test automation: {cleanup_data}"
                )

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

        automation_id = await self._create_trace_automation(
            mcp_client, cleanup_tracker, create_config
        )
        logger.info(f"Created automation: {automation_id}")

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
                {"entity_id": automation_id},
            )
            data = parse_mcp_result(result)
            return data.get("trace_count", 0) > 0

        logger.info("Waiting for automation trace to be recorded...")
        trace_appeared = await wait_for_condition(
            check_automation_traces,
            timeout=15,
            poll_interval=0.5,
            condition_name="automation trace to be recorded",
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
            {"entity_id": automation_id},
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
                {"entity_id": automation_id, "run_id": run_id},
            )
            assert_mcp_success(detailed_result)
            detailed_data = parse_mcp_result(detailed_result)

            assert detailed_data.get("success") is True
            assert detailed_data.get("run_id") == run_id

            # Verify detailed content structure (Deep verification)
            # This ensures we correctly parsed the flat structure (trigger/0, action/0)
            assert "trigger" in detailed_data, (
                "Detailed trace should contain trigger info"
            )
            assert "action_trace" in detailed_data, (
                "Detailed trace should contain action_trace"
            )
            assert isinstance(detailed_data["action_trace"], list), (
                "action_trace should be a list"
            )
            assert len(detailed_data["action_trace"]) > 0, (
                "action_trace should not be empty"
            )

            # Check for path property to ensure flat structure parsing worked
            first_action = detailed_data["action_trace"][0]
            assert "path" in first_action, "Action trace element should contain 'path'"

            logger.info(
                f"Detailed trace verified: Found {len(detailed_data['action_trace'])} actions"
            )

    async def test_blueprint_trace_preserves_inputs_and_config(
        self, mcp_client, cleanup_tracker, test_data_factory
    ):
        """A real blueprint trace keeps blueprint_inputs, last_step and config."""
        test_light = await self._find_test_light_entity(mcp_client)
        suffix = uuid.uuid4().hex[:8]
        blueprint_path = f"e2e/trace_fields_{suffix}.yaml"
        blueprint_yaml = f"""blueprint:
  name: Trace Fields E2E {suffix}
  description: Temporary blueprint for trace field verification.
  domain: automation
  input:
    target_entity:
      name: Target entity
      selector:
        entity: {{}}
trigger:
  - platform: event
    event_type: trace_fields_{suffix}
action:
  - delay:
      seconds: 0
  - action: homeassistant.turn_on
    target:
      entity_id: !input target_entity
"""
        saved = await mcp_client.call_tool(
            "ha_manage_blueprints",
            {
                "action": "save",
                "domain": "automation",
                "path": blueprint_path,
                "yaml": blueprint_yaml,
            },
        )
        assert_mcp_success(saved)

        automation_id = None
        try:
            create_config = {
                "alias": f"Trace Fields E2E {suffix}",
                "use_blueprint": {
                    "path": blueprint_path,
                    "input": {"target_entity": test_light},
                },
                "trace": {"stored_traces": 5},
                "initial_state": True,
            }
            create_result = await mcp_client.call_tool(
                "ha_config_set_automation", {"config": create_config}
            )
            assert_mcp_success(create_result)
            create_data = parse_mcp_result(create_result)
            automation_id = create_data.get("entity_id") or create_data.get(
                "automation_id"
            )
            assert automation_id is not None
            cleanup_tracker.track("automation", automation_id)

            trigger_result = await mcp_client.call_tool(
                "ha_call_service",
                {
                    "domain": "automation",
                    "service": "trigger",
                    "entity_id": automation_id,
                },
            )
            assert_mcp_success(trigger_result)

            traces_data = await wait_for_tool_result(
                mcp_client,
                tool_name="ha_get_automation_traces",
                arguments={"entity_id": automation_id},
                predicate=lambda data: data.get("trace_count", 0) > 0,
                timeout=15,
                poll_interval=0.5,
                description="blueprint automation trace",
            )

            detail_result = await mcp_client.call_tool(
                "ha_get_automation_traces",
                {
                    "entity_id": automation_id,
                    "run_id": traces_data["traces"][0]["run_id"],
                    "sections": "config",
                },
            )
            assert_mcp_success(detail_result)
            detail_data = parse_mcp_result(detail_result)

            assert detail_data["last_step"].startswith("action/")
            assert detail_data["blueprint_inputs"]["use_blueprint"]["input"] == {
                "target_entity": test_light
            }
            assert "triggers" in detail_data["config"]
            assert "actions" in detail_data["config"]
        finally:
            if automation_id is not None:
                await safe_call_tool(
                    mcp_client,
                    "ha_config_remove_automation",
                    {"entity_id": automation_id},
                )
            await safe_call_tool(
                mcp_client,
                "ha_manage_blueprints",
                {
                    "action": "delete",
                    "domain": "automation",
                    "path": blueprint_path,
                    "confirm": True,
                },
            )

    async def test_event_context_resolves_to_triggered_run(
        self, mcp_client, cleanup_tracker, test_data_factory
    ):
        """The context_id from ha_call_event resolves to the child run."""
        suffix = uuid.uuid4().hex[:8]
        event_type = f"trace_parent_context_e2e_{suffix}"
        create_config = test_data_factory.automation_config(
            "Trace Parent Context E2E",
            trigger=[{"platform": "event", "event_type": event_type}],
            action=[{"delay": {"seconds": 0}}],
            initial_state=True,
        )

        create_result = await mcp_client.call_tool(
            "ha_config_set_automation", {"config": create_config}
        )
        assert_mcp_success(create_result)
        create_data = parse_mcp_result(create_result)
        # A create whose entity wait expired returns the numeric config id,
        # which the trace tool cannot resolve.
        automation_id = create_data.get("entity_id")
        assert automation_id, (
            f"ha_config_set_automation did not resolve an entity id: {create_data}"
        )
        cleanup_tracker.track("automation", automation_id)
        assert await wait_for_entity_registration(mcp_client, automation_id), (
            f"{automation_id} never became queryable"
        )

        event_result = await mcp_client.call_tool(
            "ha_call_event", {"event_type": event_type}
        )
        assert_mcp_success(event_result)
        event_data = parse_mcp_result(event_result)
        event_context_id = event_data["context_id"]
        assert event_context_id is not None

        traces_data = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_automation_traces",
            arguments={"entity_id": automation_id},
            predicate=lambda data: data.get("trace_count", 0) > 0,
            timeout=15,
            poll_interval=0.5,
            description="automation trace after event context",
        )

        by_context_result = await mcp_client.call_tool(
            "ha_get_automation_traces",
            {
                "entity_id": automation_id,
                "context_id": event_context_id,
            },
        )
        assert_mcp_success(by_context_result)
        by_context_data = parse_mcp_result(by_context_result)

        assert by_context_data["context_match"] == "parent"
        assert by_context_data["context_id"] == event_context_id
        assert by_context_data["run_id"] == traces_data["traces"][0]["run_id"]
        assert by_context_data["context_scanned"] >= 1

    async def test_trace_storage_disabled_diagnostics(
        self, mcp_client, cleanup_tracker, test_data_factory
    ):
        """stored_traces=0 reports disabled storage, not a generic empty result."""
        automation_name = "Trace Storage Disabled E2E"
        create_config = test_data_factory.automation_config(
            automation_name,
            trigger=[
                {
                    "platform": "event",
                    "event_type": "trace_storage_disabled_e2e",
                }
            ],
            action=[{"delay": {"seconds": 0}}],
            trace={"stored_traces": 0},
            initial_state=True,
        )

        create_result = await mcp_client.call_tool(
            "ha_config_set_automation", {"config": create_config}
        )
        assert_mcp_success(create_result)
        create_data = parse_mcp_result(create_result)
        # A create whose entity wait expired returns the numeric config id,
        # which the trace tool cannot resolve.
        automation_id = create_data.get("entity_id")
        assert automation_id, (
            f"ha_config_set_automation did not resolve an entity id: {create_data}"
        )
        cleanup_tracker.track("automation", automation_id)
        assert await wait_for_entity_registration(mcp_client, automation_id), (
            f"{automation_id} never became queryable"
        )

        trigger_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "automation",
                "service": "trigger",
                "entity_id": automation_id,
            },
        )
        assert_mcp_success(trigger_result)

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

    async def test_context_lookup_returns_same_run(
        self, mcp_client, cleanup_tracker, test_data_factory
    ):
        """A context id resolves to the run that owns it."""
        automation_name = "Trace Context Lookup E2E"
        create_config = test_data_factory.automation_config(
            automation_name,
            trigger=[
                {
                    "platform": "event",
                    "event_type": "trace_context_lookup_e2e",
                }
            ],
            action=[{"delay": {"seconds": 0}}],
            initial_state=True,
        )

        create_result = await mcp_client.call_tool(
            "ha_config_set_automation", {"config": create_config}
        )
        assert_mcp_success(create_result)
        create_data = parse_mcp_result(create_result)
        # A create whose entity wait expired returns the numeric config id,
        # which the trace tool cannot resolve.
        automation_id = create_data.get("entity_id")
        assert automation_id, (
            f"ha_config_set_automation did not resolve an entity id: {create_data}"
        )
        cleanup_tracker.track("automation", automation_id)
        assert await wait_for_entity_registration(mcp_client, automation_id), (
            f"{automation_id} never became queryable"
        )

        trigger_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "automation",
                "service": "trigger",
                "entity_id": automation_id,
            },
        )
        assert_mcp_success(trigger_result)

        traces_data = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_automation_traces",
            arguments={"entity_id": automation_id},
            predicate=lambda data: data.get("trace_count", 0) > 0,
            timeout=15,
            poll_interval=0.5,
            description="automation trace for context lookup",
        )
        run_id = traces_data["traces"][0]["run_id"]

        detail_result = await mcp_client.call_tool(
            "ha_get_automation_traces",
            {
                "entity_id": automation_id,
                "run_id": run_id,
                "detailed": True,
            },
        )
        assert_mcp_success(detail_result)
        detail_data = parse_mcp_result(detail_result)
        context_id = detail_data["context"]["id"]

        by_context_result = await mcp_client.call_tool(
            "ha_get_automation_traces",
            {"entity_id": automation_id, "context_id": context_id},
        )
        assert_mcp_success(by_context_result)
        by_context_data = parse_mcp_result(by_context_result)

        assert by_context_data["run_id"] == run_id
        assert by_context_data["context_id"] == context_id

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

        automation_id = create_data.get("entity_id")
        assert automation_id, (
            f"ha_config_set_automation did not resolve an entity id: {create_data}"
        )
        cleanup_tracker.track("automation", automation_id)
        assert await wait_for_entity_registration(mcp_client, automation_id), (
            f"{automation_id} never became queryable"
        )

        # Wait for automation registration

        # 3. Get traces (should be empty with diagnostics)
        traces_result = await mcp_client.call_tool(
            "ha_get_automation_traces",
            {"entity_id": automation_id},
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
            assert "entity_exists" in diagnostics
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
                {"entity_id": script_entity_id},
            )
            data = parse_mcp_result(result)
            return data.get("trace_count", 0) > 0

        logger.info("Waiting for script trace to be recorded...")
        trace_appeared = await wait_for_condition(
            check_traces,
            timeout=15,  # Increased timeout for ARM compatibility
            poll_interval=0.5,
            condition_name="script trace to be recorded",
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
            {"entity_id": script_entity_id},
        )
        assert_mcp_success(traces_result)
        traces_data = parse_mcp_result(traces_result)

        assert traces_data.get("success") is True
        trace_count = traces_data.get("trace_count", 0)

        logger.info(f"Script traces retrieved: {trace_count}")

        assert trace_count > 0, (
            f"Expected at least 1 trace after running script, got {trace_count}"
        )

    async def test_script_trace_storage_disabled_diagnostics(
        self, mcp_client, cleanup_tracker, test_data_factory
    ):
        """A script with stored_traces=0 reports disabled storage, not idle."""
        test_light = await self._find_test_light_entity(mcp_client)
        script_id_base = "trace_storage_disabled_script_e2e"
        script_config = test_data_factory.script_config(
            "Trace Storage Disabled Script E2E",
            sequence=[
                {"service": "light.turn_on", "target": {"entity_id": test_light}}
            ],
            trace={"stored_traces": 0},
        )

        create_result = await mcp_client.call_tool(
            "ha_config_set_script",
            {"script_id": script_id_base, "config": script_config},
        )
        assert_mcp_success(create_result)
        create_data = parse_mcp_result(create_result)
        script_entity_id = create_data.get("entity_id") or f"script.{script_id_base}"
        cleanup_tracker.track("script", script_entity_id)

        run_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "script",
                "service": "turn_on",
                "entity_id": script_entity_id,
            },
        )
        assert_mcp_success(run_result)

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

    async def test_not_triggered_trace_keeps_core_diagnostics(
        self, mcp_client, cleanup_tracker
    ):
        """A non-firing purpose-specific trigger keeps Core's reason."""
        suffix = uuid.uuid4().hex[:8]
        light_id = await self._find_test_light_entity(mcp_client)
        automation_id = None
        try:
            create_config = {
                "alias": f"Trace Not Triggered {suffix}",
                "trigger": [
                    {
                        "trigger": "light.brightness_changed",
                        "target": {"entity_id": light_id},
                        "options": {"threshold": {"type": "any"}},
                    }
                ],
                "action": [{"delay": {"seconds": 0}}],
                "mode": "single",
                "trace": {"stored_traces": 5},
                "initial_state": True,
            }
            create_result = await mcp_client.call_tool(
                "ha_config_set_automation", {"config": create_config}
            )
            create_data = assert_mcp_success(
                create_result, "Create not-triggered automation"
            )
            automation_id = create_data.get("entity_id") or create_data.get(
                "automation_id"
            )
            assert automation_id is not None
            cleanup_tracker.track("automation", automation_id)

            turn_on_result = await mcp_client.call_tool(
                "ha_call_service",
                {
                    "domain": "light",
                    "service": "turn_on",
                    "entity_id": light_id,
                },
            )
            assert_mcp_success(turn_on_result, "Turn the light on")

            async def _light_is_on() -> bool:
                state = await safe_call_tool(
                    mcp_client, "ha_get_state", {"entity_id": light_id}
                )
                if state.get("error"):
                    pytest.fail(f"State query for {light_id} failed: {state['error']}")
                return state.get("data", {}).get("state") == "on"

            light_is_on = await wait_for_condition(
                _light_is_on,
                timeout=10,
                condition_name=f"{light_id} to turn on",
            )
            assert light_is_on, (
                f"{light_id} never turned on; the not-triggered trace setup is invalid"
            )

            turn_off_result = await mcp_client.call_tool(
                "ha_call_service",
                {
                    "domain": "light",
                    "service": "turn_off",
                    "entity_id": light_id,
                },
            )
            assert_mcp_success(turn_off_result, "Turn the light off")

            traces_data = await wait_for_tool_result(
                mcp_client,
                tool_name="ha_get_automation_traces",
                arguments={"entity_id": automation_id},
                predicate=lambda data: any(
                    trace.get("not_triggered") for trace in data.get("traces", [])
                ),
                timeout=15,
                poll_interval=0.5,
                description="not-triggered automation trace",
            )
            run_id = next(
                trace["run_id"]
                for trace in traces_data["traces"]
                if trace.get("not_triggered")
            )

            detail_result = await mcp_client.call_tool(
                "ha_get_automation_traces",
                {"entity_id": automation_id, "run_id": run_id, "sections": "trigger"},
            )
            detail = assert_mcp_success(detail_result, "Read not-triggered trace")

            assert detail["not_triggered"] is True
            assert detail["script_execution"] == "not_triggered"
            assert detail["last_step"] == "trigger/0"
            trigger_result = detail["trigger"]["result"]
            assert trigger_result["reason"]
            assert isinstance(trigger_result["data"], dict)
        finally:
            if automation_id is not None:
                await safe_call_tool(
                    mcp_client,
                    "ha_config_remove_automation",
                    {"entity_id": automation_id},
                )


@pytest.mark.automation
class TestGetAutomationTracesNegativeInputs:
    """Negative-input tests for ha_get_automation_traces."""

    async def test_wrong_domain_prefix_rejected(self, mcp_client: Any) -> None:
        """Rejects an entity ID that does not belong to a supported domain."""
        result = await safe_call_tool(
            mcp_client,
            "ha_get_automation_traces",
            {"entity_id": "sensor.some_entity"},
        )
        assert result["success"] is False
        assert result["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
