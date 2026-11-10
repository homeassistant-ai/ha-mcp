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
    looks_like_success,
    parse_mcp_result,
    safe_call_tool,
)
from ...utilities.cleanup import CleanupSteps
from ...utilities.entity_finders import find_test_light_entity
from ...utilities.topology import component_surface_available
from ...utilities.wait_helpers import (
    wait_for_condition,
    wait_for_entity_registration,
    wait_for_tool_result,
)

logger = logging.getLogger(__name__)


@pytest.fixture(scope="module")
async def trace_entity_cleanup(mcp_client):
    """Yield a list that tracks automations/scripts created by trace tests.

    Every entry is a ``(domain, entity_id)`` tuple. At teardown each tracked
    automation/script is removed with the config tools so tests do not leak
    entities on the shared HA instance.
    """
    created: list[tuple[str, str]] = []
    yield created

    logger.info(f"🧹 Cleaning up {len(created)} trace test entities")
    failed: list[tuple[str, str]] = []
    for domain, entity_id in created:
        if domain == "automation":
            result = await safe_call_tool(
                mcp_client,
                "ha_config_remove_automation",
                {"entity_id": entity_id},
            )
            if not looks_like_success(result):
                failed.append((domain, entity_id))
        elif domain == "script":
            script_id = entity_id.split(".", 1)[-1]
            result = await safe_call_tool(
                mcp_client,
                "ha_config_remove_script",
                {"script_id": script_id},
            )
            if not looks_like_success(result):
                failed.append((domain, entity_id))
        else:
            logger.warning(f"⚠️ Unknown trace entity domain: {domain} {entity_id}")
    if failed:
        logger.warning(f"⚠️ Failed to remove {len(failed)} trace entities: {failed}")


@pytest.mark.automation
@pytest.mark.cleanup
class TestAutomationTraces:
    """Test automation trace retrieval functionality."""

    async def _find_test_light_entity(self, mcp_client) -> str:
        """Delegates to the suite-wide helper in utilities.entity_finders."""
        return await find_test_light_entity(mcp_client)

    async def _create_trace_automation(
        self, mcp_client, cleanup: list[tuple[str, str]], config: dict[str, Any]
    ) -> str:
        """Create an automation and wait until its entity is queryable."""
        create_data = await MCPAssertions(mcp_client).call_tool_success(
            "ha_config_set_automation", {"config": config}
        )
        automation_id = create_data.get("entity_id")
        leftover_error = None
        if not automation_id:
            # The config exists even though entity discovery timed out; remove
            # it before failing so it cannot pollute later tests on this HA.
            config_id = create_data.get("automation_id")
            if config_id:
                remove_result = await safe_call_tool(
                    mcp_client,
                    "ha_config_remove_automation",
                    {"entity_id": config_id},
                )
                if not looks_like_success(remove_result):
                    cleanup.append(("automation", config_id))
                leftover_error = remove_result.get("error")
        assert automation_id, (
            f"ha_config_set_automation did not resolve an entity id: {create_data}"
            + (
                f"; removing the leftover config also failed: {leftover_error}"
                if leftover_error
                else ""
            )
        )
        cleanup.append(("automation", automation_id))
        if not await wait_for_entity_registration(mcp_client, automation_id):
            remove_result = await safe_call_tool(
                mcp_client,
                "ha_config_remove_automation",
                {"entity_id": automation_id},
            )
            if looks_like_success(remove_result):
                cleanup.pop()
            raise AssertionError(f"{automation_id} never became queryable")
        return automation_id

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

        automation_id = await self._create_trace_automation(
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
        test_light = await self._find_test_light_entity(mcp_client)
        logger.info(f"Using test light entity: {test_light}")

        # 2. Create an automation that can be manually triggered
        automation_name = f"Trace Test Automation E2E {suffix}"
        create_config = test_data_factory.automation_config(
            automation_name,
            # Use event trigger so we can manually trigger it
            trigger=[{"platform": "event", "event_type": f"test_trace_event_{suffix}"}],
            action=[{"service": "light.turn_on", "target": {"entity_id": test_light}}],
        )

        automation_id = await self._create_trace_automation(
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

    async def test_logbook_rows_for_a_new_run(
        self, mcp_client, trace_entity_cleanup, test_data_factory
    ):
        """A new run's logbook rows arrive through the component, not a script."""
        suffix = uuid.uuid4().hex[:8]
        create_config = test_data_factory.automation_config(
            f"Trace Logbook E2E {suffix}",
            trigger=[
                {
                    "platform": "event",
                    "event_type": f"test_trace_logbook_event_{suffix}",
                }
            ],
            action=[{"delay": {"seconds": 0.1}}],
        )
        automation_id = await self._create_trace_automation(
            mcp_client, trace_entity_cleanup, create_config
        )
        mcp = MCPAssertions(mcp_client)

        before = await mcp.call_tool_success(
            "ha_get_automation_traces", {"entity_id": automation_id}
        )
        runs_before = {trace.get("run_id") for trace in before.get("traces", [])}

        await mcp.call_tool_success(
            "ha_call_service",
            {
                "domain": "automation",
                "service": "trigger",
                "entity_id": automation_id,
            },
        )
        traces = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_automation_traces",
            arguments={"entity_id": automation_id},
            predicate=lambda data: any(
                trace["run_id"] not in runs_before for trace in data.get("traces", [])
            ),
            timeout=30,
            description="logbook test trace",
        )
        run_id = next(
            trace["run_id"]
            for trace in traces["traces"]
            if trace["run_id"] not in runs_before
        )

        detail = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_automation_traces",
            arguments={
                "entity_id": automation_id,
                "run_id": run_id,
                "sections": "logbook",
            },
            predicate=lambda data: bool(data.get("logbook_entries")),
            timeout=30,
            poll_interval=1,
            description="trace logbook rows",
        )

        assert detail["logbook_entries"], "the new run has no logbook rows"
        expected_transport = "component" if component_surface_available() else "rest"
        assert detail["logbook_transport"] == expected_transport

    async def test_blueprint_trace_preserves_inputs_and_config(
        self, mcp_client, trace_entity_cleanup, test_data_factory
    ):
        """A real blueprint trace keeps blueprint_inputs, last_step and config."""
        test_light = await self._find_test_light_entity(mcp_client)
        suffix = uuid.uuid4().hex[:8]
        blueprint_path = f"e2e/trace_fields_{suffix}.yaml"
        event_type = f"trace_fields_{suffix}"
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
    event_type: {event_type}
action:
  - delay:
      seconds: 0
  - action: homeassistant.turn_on
    target:
      entity_id: !input target_entity
"""
        await MCPAssertions(mcp_client).call_tool_success(
            "ha_manage_blueprints",
            {
                "action": "save",
                "domain": "automation",
                "path": blueprint_path,
                "yaml": blueprint_yaml,
            },
        )

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
            mcp = MCPAssertions(mcp_client)
            automation_id = await self._create_trace_automation(
                mcp_client, trace_entity_cleanup, create_config
            )

            before = await mcp.call_tool_success(
                "ha_get_automation_traces", {"entity_id": automation_id}
            )
            runs_before = {trace.get("run_id") for trace in before.get("traces", [])}

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
                predicate=lambda data: any(
                    trace["run_id"] not in runs_before
                    for trace in data.get("traces", [])
                ),
                timeout=15,
                poll_interval=0.5,
                description="blueprint automation trace",
            )

            run_id = next(
                trace["run_id"]
                for trace in traces_data["traces"]
                if trace["run_id"] not in runs_before
            )

            detail_data = await mcp.call_tool_success(
                "ha_get_automation_traces",
                {
                    "entity_id": automation_id,
                    "run_id": run_id,
                    "sections": "config,last_step,blueprint_inputs",
                },
            )

            assert detail_data["last_step"].startswith("action/")
            assert detail_data["blueprint_inputs"]["use_blueprint"]["input"] == {
                "target_entity": test_light
            }
            assert "triggers" in detail_data["config"]
            assert "actions" in detail_data["config"]
        finally:
            with CleanupSteps() as cleanup, cleanup.step():
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
        self, mcp_client, trace_entity_cleanup, test_data_factory
    ):
        """The context_id from ha_call_event resolves to the child run."""
        suffix = uuid.uuid4().hex[:8]
        event_type = f"trace_parent_context_e2e_{suffix}"
        create_config = test_data_factory.automation_config(
            f"Trace Parent Context E2E {suffix}",
            trigger=[{"platform": "event", "event_type": event_type}],
            action=[{"delay": {"seconds": 0}}],
            initial_state=True,
        )

        mcp = MCPAssertions(mcp_client)
        automation_id = await self._create_trace_automation(
            mcp_client, trace_entity_cleanup, create_config
        )

        before = await mcp.call_tool_success(
            "ha_get_automation_traces", {"entity_id": automation_id}
        )
        runs_before = {trace.get("run_id") for trace in before.get("traces", [])}

        event_data = await mcp.call_tool_success(
            "ha_call_event", {"event_type": event_type}
        )
        event_context_id = event_data["context_id"]
        assert event_context_id is not None

        traces_data = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_automation_traces",
            arguments={"entity_id": automation_id},
            predicate=lambda data: any(
                trace["run_id"] not in runs_before for trace in data.get("traces", [])
            ),
            timeout=15,
            poll_interval=0.5,
            description="automation trace after event context",
        )

        run_id = next(
            trace["run_id"]
            for trace in traces_data["traces"]
            if trace["run_id"] not in runs_before
        )

        by_context_data = await mcp.call_tool_success(
            "ha_get_automation_traces",
            {
                "entity_id": automation_id,
                "context_id": event_context_id,
            },
        )

        assert by_context_data["context_match"] == "parent"
        assert by_context_data["context_id"] == event_context_id
        assert by_context_data["run_id"] == run_id
        assert by_context_data["context_scanned"] >= 1

        # A domain list gives Core's domain/item_id, which must address the
        # same run without inventing an entity_id.
        domain_list = await mcp.call_tool_success(
            "ha_get_automation_traces",
            {"domain": "automation", "limit": 50},
        )
        row = next(
            (
                trace
                for trace in domain_list["traces"]
                if trace["run_id"] == by_context_data["run_id"]
            ),
            None,
        )
        assert row is not None, "the new run is missing from the newest 50 runs"
        by_item = await mcp.call_tool_success(
            "ha_get_automation_traces",
            {
                "domain": "automation",
                "item_id": row["item_id"],
                "run_id": row["run_id"],
            },
        )
        assert by_item["run_id"] == row["run_id"]
        assert by_item["item_id"] == row["item_id"]
        assert "entity_id" not in by_item

        context_only = await mcp.call_tool_success(
            "ha_get_automation_traces",
            {"context_id": event_context_id},
        )
        assert context_only["context_match"] == "parent"
        assert context_only["domain"] == "automation"
        assert context_only["item_id"] == row["item_id"]
        assert "entity_id" not in context_only

    async def test_shared_context_lists_automation_and_script_matches(
        self, mcp_client, trace_entity_cleanup, test_data_factory
    ):
        """An automation that calls a script shares its own context with the script."""
        suffix = uuid.uuid4().hex[:8]
        event_type = f"trace_shared_context_e2e_{suffix}"
        script_id = f"trace_shared_context_script_{suffix}"
        script_config = test_data_factory.script_config(
            f"Trace Shared Context Script E2E {suffix}",
            sequence=[{"delay": {"seconds": 0}}],
        )

        mcp = MCPAssertions(mcp_client)
        script_create = await mcp.call_tool_success(
            "ha_config_set_script", {"script_id": script_id, "config": script_config}
        )
        script_entity_id = script_create.get("entity_id") or f"script.{script_id}"
        trace_entity_cleanup.append(("script", script_entity_id))
        assert await wait_for_entity_registration(mcp_client, script_entity_id), (
            f"{script_entity_id} never became queryable"
        )

        create_config = test_data_factory.automation_config(
            f"Trace Shared Context E2E {suffix}",
            trigger=[{"platform": "event", "event_type": event_type}],
            action=[
                {"service": "script.turn_on", "target": {"entity_id": script_entity_id}}
            ],
            initial_state=True,
        )
        automation_id = await self._create_trace_automation(
            mcp_client, trace_entity_cleanup, create_config
        )

        auto_before = await mcp.call_tool_success(
            "ha_get_automation_traces", {"entity_id": automation_id}
        )
        auto_runs_before = {
            trace.get("run_id") for trace in auto_before.get("traces", [])
        }
        script_before = await mcp.call_tool_success(
            "ha_get_automation_traces", {"entity_id": script_entity_id}
        )
        script_runs_before = {
            trace.get("run_id") for trace in script_before.get("traces", [])
        }

        await mcp.call_tool_success("ha_call_event", {"event_type": event_type})

        auto_traces = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_automation_traces",
            arguments={"entity_id": automation_id},
            predicate=lambda data: any(
                trace["run_id"] not in auto_runs_before
                for trace in data.get("traces", [])
            ),
            timeout=15,
            poll_interval=0.5,
            description="automation trace after shared context event",
        )
        script_traces = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_automation_traces",
            arguments={"entity_id": script_entity_id},
            predicate=lambda data: any(
                trace["run_id"] not in script_runs_before
                for trace in data.get("traces", [])
            ),
            timeout=15,
            poll_interval=0.5,
            description="script trace after shared context event",
        )
        auto_row = next(
            trace
            for trace in auto_traces["traces"]
            if trace["run_id"] not in auto_runs_before
        )
        script_row = next(
            trace
            for trace in script_traces["traces"]
            if trace["run_id"] not in script_runs_before
        )
        auto_run_id = auto_row["run_id"]
        script_run_id = script_row["run_id"]

        auto_detail = await mcp.call_tool_success(
            "ha_get_automation_traces",
            {"entity_id": automation_id, "run_id": auto_run_id},
        )
        automation_context_id = auto_detail["context"]["id"]

        by_context_data = await mcp.call_tool_success(
            "ha_get_automation_traces",
            {"context_id": automation_context_id},
        )

        selected_run_id = by_context_data["run_id"]
        assert selected_run_id in {auto_run_id, script_run_id}
        expected_runs = {
            auto_run_id: {
                "domain": auto_row["domain"],
                "item_id": auto_row["item_id"],
            },
            script_run_id: {
                "domain": script_row["domain"],
                "item_id": script_row["item_id"],
            },
        }
        assert expected_runs[selected_run_id]["domain"] == by_context_data["domain"]
        assert expected_runs[selected_run_id]["item_id"] == by_context_data["item_id"]

        assert "context_other_matches" in by_context_data
        other = by_context_data["context_other_matches"]
        other_run_ids = {m["run_id"] for m in other}
        expected_other = {auto_run_id, script_run_id} - {selected_run_id}
        assert other_run_ids == expected_other, (
            f"expected other matches {expected_other}, got {other_run_ids}; "
            f"selected={selected_run_id}, automation={auto_run_id}, script={script_run_id}"
        )
        for match in other:
            expected = expected_runs[match["run_id"]]
            assert match["domain"] == expected["domain"]
            assert match["item_id"] == expected["item_id"]

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
        automation_id = await self._create_trace_automation(
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

    async def test_context_lookup_returns_same_run(
        self, mcp_client, trace_entity_cleanup, test_data_factory
    ):
        """A context id resolves to the run that owns it."""
        suffix = uuid.uuid4().hex[:8]
        automation_name = f"Trace Context Lookup E2E {suffix}"
        create_config = test_data_factory.automation_config(
            automation_name,
            trigger=[
                {
                    "platform": "event",
                    "event_type": f"trace_context_lookup_e2e_{suffix}",
                }
            ],
            action=[{"delay": {"seconds": 0}}],
            initial_state=True,
        )

        mcp = MCPAssertions(mcp_client)
        automation_id = await self._create_trace_automation(
            mcp_client, trace_entity_cleanup, create_config
        )

        before = await mcp.call_tool_success(
            "ha_get_automation_traces", {"entity_id": automation_id}
        )
        runs_before = {trace.get("run_id") for trace in before.get("traces", [])}

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
            predicate=lambda data: any(
                trace["run_id"] not in runs_before for trace in data.get("traces", [])
            ),
            timeout=15,
            poll_interval=0.5,
            description="automation trace for context lookup",
        )
        run_id = next(
            trace["run_id"]
            for trace in traces_data["traces"]
            if trace["run_id"] not in runs_before
        )

        detail_data = await mcp.call_tool_success(
            "ha_get_automation_traces",
            {
                "entity_id": automation_id,
                "run_id": run_id,
                "detailed": True,
            },
        )
        context_id = detail_data["context"]["id"]

        by_context_data = await mcp.call_tool_success(
            "ha_get_automation_traces",
            {"entity_id": automation_id, "context_id": context_id},
        )

        assert by_context_data["run_id"] == run_id
        assert by_context_data["context_id"] == context_id

    async def test_empty_traces_with_diagnostics(
        self, mcp_client, trace_entity_cleanup, test_data_factory
    ):
        """
        Test: Create automation (no trigger) → Get traces → Verify diagnostics

        This test validates that when an automation has no traces:
        1. The response still succeeds
        2. Diagnostics are included
        3. Diagnostics contain helpful information
        """
        suffix = uuid.uuid4().hex[:8]

        # 1. Find a test light entity
        test_light = await self._find_test_light_entity(mcp_client)

        # 2. Create an automation that won't be triggered
        automation_name = f"Empty Trace Test E2E {suffix}"
        create_config = test_data_factory.automation_config(
            automation_name,
            # Use a trigger that won't fire
            trigger=[{"platform": "time", "at": "03:00:00"}],
            action=[{"service": "light.turn_on", "target": {"entity_id": test_light}}],
        )

        mcp = MCPAssertions(mcp_client)
        automation_id = await self._create_trace_automation(
            mcp_client, trace_entity_cleanup, create_config
        )

        # 3. Get traces (should be empty with diagnostics)
        traces_data = await mcp.call_tool_success(
            "ha_get_automation_traces",
            {"entity_id": automation_id},
        )

        assert traces_data.get("success") is True

        # Traces should be empty for a never-triggered automation
        trace_count = traces_data.get("trace_count", 0)
        assert trace_count == 0, (
            f"Expected 0 traces for never-triggered automation, got {trace_count}"
        )

        # Verify diagnostics are present
        diagnostics = traces_data.get("diagnostics")
        assert diagnostics is not None, (
            "Diagnostics should be included when traces are empty"
        )
        assert "entity_exists" in diagnostics
        assert "suggestion" in diagnostics

        logger.info(f"Diagnostics received: {diagnostics.get('suggestion')}")

    async def test_script_traces(
        self, mcp_client, trace_entity_cleanup, test_data_factory
    ):
        """
        Test: Create script → Run → Get traces → Verify non-empty

        This test validates that trace retrieval also works for scripts.
        """
        suffix = uuid.uuid4().hex[:8]

        # 1. Find a test light entity
        test_light = await self._find_test_light_entity(mcp_client)

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
        create_data = await mcp.call_tool_success(
            "ha_config_set_script",
            {"script_id": script_id_base, "config": script_config},
        )

        # Get the entity_id form (script.trace_test_script_e2e)
        script_entity_id = create_data.get("entity_id") or f"script.{script_id_base}"
        trace_entity_cleanup.append(("script", script_entity_id))
        assert await wait_for_entity_registration(mcp_client, script_entity_id), (
            f"{script_entity_id} never became queryable"
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
        test_light = await self._find_test_light_entity(mcp_client)
        script_id_base = f"trace_storage_disabled_script_e2e_{suffix}"
        script_config = test_data_factory.script_config(
            f"Trace Storage Disabled Script E2E {suffix}",
            sequence=[
                {"service": "light.turn_on", "target": {"entity_id": test_light}}
            ],
            trace={"stored_traces": 0},
        )

        mcp = MCPAssertions(mcp_client)
        create_data = await mcp.call_tool_success(
            "ha_config_set_script",
            {"script_id": script_id_base, "config": script_config},
        )
        script_entity_id = create_data.get("entity_id") or f"script.{script_id_base}"
        trace_entity_cleanup.append(("script", script_entity_id))
        assert await wait_for_entity_registration(mcp_client, script_entity_id), (
            f"{script_entity_id} never became queryable"
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

    async def test_not_triggered_trace_keeps_core_diagnostics(
        self, mcp_client, trace_entity_cleanup
    ):
        """A non-firing purpose-specific trigger keeps Core's reason."""
        suffix = uuid.uuid4().hex[:8]
        light_id = await self._find_test_light_entity(mcp_client)
        automation_id = None
        light_prior_state: str | None = None
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
            mcp = MCPAssertions(mcp_client)
            automation_id = await self._create_trace_automation(
                mcp_client, trace_entity_cleanup, create_config
            )

            prior_light = await mcp.call_tool_success(
                "ha_get_state", {"entity_id": light_id}
            )
            light_prior_state = (prior_light.get("data") or {}).get("state")
            assert light_prior_state in {"on", "off"}, (
                f"{light_id} has state {light_prior_state!r}; "
                "test requires a light with a known on/off prior state"
            )

            await mcp.call_tool_success(
                "ha_call_service",
                {
                    "domain": "light",
                    "service": "turn_on",
                    "entity_id": light_id,
                },
            )

            async def _light_is_on() -> bool:
                state = await mcp.call_tool_success(
                    "ha_get_state", {"entity_id": light_id}
                )
                return state.get("data", {}).get("state") == "on"

            light_is_on = await wait_for_condition(
                _light_is_on,
                timeout=10,
                condition_name=f"{light_id} to turn on",
            )
            assert light_is_on, (
                f"{light_id} never turned on; the not-triggered trace setup is invalid"
            )

            await mcp.call_tool_success(
                "ha_call_service",
                {
                    "domain": "light",
                    "service": "turn_off",
                    "entity_id": light_id,
                },
            )

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

            detail = await mcp.call_tool_success(
                "ha_get_automation_traces",
                {
                    "entity_id": automation_id,
                    "run_id": run_id,
                    "sections": "trace,last_step,not_triggered,script_execution",
                },
            )

            assert detail["not_triggered"] is True
            assert detail["script_execution"] == "not_triggered"
            assert detail["last_step"] == "trigger/0"
            trigger_result = detail["trace"]["trigger/0"][0]["result"]
            assert trigger_result["reason"]
            assert isinstance(trigger_result["data"], dict)
        finally:
            with CleanupSteps() as cleanup, cleanup.step():
                if light_prior_state in {"on", "off"}:
                    # The test toggled the light to force the state
                    # trigger; put the instance back the way it was.
                    await MCPAssertions(mcp_client).call_tool_success(
                        "ha_call_service",
                        {
                            "domain": "light",
                            "service": (
                                "turn_on" if light_prior_state == "on" else "turn_off"
                            ),
                            "entity_id": light_id,
                        },
                    )


@pytest.mark.automation
class TestGetAutomationTracesNegativeInputs:
    """Negative-input tests for ha_get_automation_traces."""

    async def test_wrong_domain_prefix_rejected(self, mcp_client: Any) -> None:
        """Rejects an entity ID that does not belong to a supported domain."""
        mcp = MCPAssertions(mcp_client)
        result = await mcp.call_tool_failure(
            "ha_get_automation_traces",
            {"entity_id": "sensor.some_entity"},
        )
        assert result["error"]["code"] == "VALIDATION_INVALID_PARAMETER"

    async def test_sections_rejected_without_resolved_run(
        self, mcp_client: Any
    ) -> None:
        """sections is invalid in list mode."""
        mcp = MCPAssertions(mcp_client)
        result = await mcp.call_tool_failure(
            "ha_get_automation_traces",
            {"domain": "automation", "sections": "conditions"},
        )
        assert result["error"]["code"] == "VALIDATION_INVALID_PARAMETER"

    async def test_detailed_rejected_without_resolved_run(
        self, mcp_client: Any
    ) -> None:
        """detailed is invalid in list mode."""
        mcp = MCPAssertions(mcp_client)
        result = await mcp.call_tool_failure(
            "ha_get_automation_traces",
            {"domain": "automation", "detailed": True},
        )
        assert result["error"]["code"] == "VALIDATION_INVALID_PARAMETER"

    async def test_item_only_empty_list_returns_warning(self, mcp_client: Any) -> None:
        """A domain+item_id list with no traces returns a top-level warning."""
        mcp = MCPAssertions(mcp_client)
        result = await mcp.call_tool_success(
            "ha_get_automation_traces",
            {"domain": "automation", "item_id": "nonexistent_item_task2_e2e"},
        )

        assert result["success"] is True
        assert result["trace_count"] == 0
        assert "warnings" in result
        assert any("No traces found" in warning for warning in result["warnings"])
