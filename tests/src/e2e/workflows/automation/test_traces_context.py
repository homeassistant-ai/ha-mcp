"""Automation trace context, logbook, blueprint and not-triggered E2E tests.

These tests exercise trace lookup by context_id, logbook sections, blueprint
input preservation, and Core's not-triggered diagnostics.
"""

from __future__ import annotations

import uuid

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.cleanup import CleanupSteps
from ...utilities.entity_finders import find_test_light_entity
from ...utilities.wait_helpers import wait_for_entity_state, wait_for_tool_result
from .conftest import create_trace_automation, create_trace_script


@pytest.mark.automation
@pytest.mark.cleanup
class TestAutomationTracesContext:
    """Test automation trace context, logbook, blueprint and not-triggered paths."""

    async def test_blueprint_trace_preserves_inputs_and_config(
        self, mcp_client, trace_entity_cleanup, test_data_factory
    ):
        """A real blueprint trace keeps blueprint_inputs, last_step and config."""
        test_light = await find_test_light_entity(mcp_client)
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
            automation_id = await create_trace_automation(
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
            with CleanupSteps() as cleanup:
                with cleanup.step():
                    # Core refuses to delete a blueprint an automation still
                    # uses, so the automation goes first.
                    if automation_id is not None:
                        await MCPAssertions(mcp_client).call_tool_success(
                            "ha_config_remove_automation",
                            {"entity_id": automation_id},
                        )
                        cleanup_entry = ("automation", automation_id)
                        if cleanup_entry in trace_entity_cleanup:
                            trace_entity_cleanup.remove(cleanup_entry)
                with cleanup.step():
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

    @pytest.mark.requires_component_surface
    async def test_event_context_resolves_to_triggered_run(
        self, mcp_client, trace_entity_cleanup, test_data_factory
    ):
        """The event's context resolves to the child run, and that run resolves
        its own context and logbook.

        Parent-context resolution needs the component: Core's ``trace/contexts``
        map only holds each run's own context.
        """
        suffix = uuid.uuid4().hex[:8]
        event_type = f"trace_parent_context_e2e_{suffix}"
        create_config = test_data_factory.automation_config(
            f"Trace Parent Context E2E {suffix}",
            trigger=[{"platform": "event", "event_type": event_type}],
            action=[{"delay": {"seconds": 0}}],
            initial_state=True,
        )

        mcp = MCPAssertions(mcp_client)
        automation_id = await create_trace_automation(
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

        # The run's own context is distinct from the triggering event context
        # and resolves back to this same run.
        detail_data = await mcp.call_tool_success(
            "ha_get_automation_traces",
            {"entity_id": automation_id, "run_id": run_id},
        )
        own_context_id = detail_data["context"]["id"]
        assert own_context_id != event_context_id
        by_own_context = await mcp.call_tool_success(
            "ha_get_automation_traces",
            {"entity_id": automation_id, "context_id": own_context_id},
        )
        assert by_own_context["context_match"] == "trace"
        assert by_own_context["run_id"] == run_id
        assert by_own_context["context_id"] == own_context_id

        # The component serves this run's logbook rows; the REST fallback is
        # unit-tested for the no-component lanes.
        logbook_data = await wait_for_tool_result(
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
        assert logbook_data["logbook_transport"] == "component"

    @pytest.mark.requires_component_surface
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
        script_entity_id = await create_trace_script(
            mcp_client, trace_entity_cleanup, script_id, script_config
        )

        create_config = test_data_factory.automation_config(
            f"Trace Shared Context E2E {suffix}",
            trigger=[{"platform": "event", "event_type": event_type}],
            action=[
                {"service": "script.turn_on", "target": {"entity_id": script_entity_id}}
            ],
            initial_state=True,
        )
        automation_id = await create_trace_automation(
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
        test_light = await find_test_light_entity(mcp_client)

        # 2. Create an automation that won't be triggered
        automation_name = f"Empty Trace Test E2E {suffix}"
        create_config = test_data_factory.automation_config(
            automation_name,
            # Use a trigger that won't fire
            trigger=[{"platform": "time", "at": "03:00:00"}],
            action=[{"service": "light.turn_on", "target": {"entity_id": test_light}}],
        )

        mcp = MCPAssertions(mcp_client)
        automation_id = await create_trace_automation(
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

    async def test_not_triggered_trace_keeps_core_diagnostics(
        self, mcp_client, trace_entity_cleanup
    ):
        """A non-firing purpose-specific trigger keeps Core's reason."""
        suffix = uuid.uuid4().hex[:8]
        light_id = await find_test_light_entity(mcp_client)
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
            automation_id = await create_trace_automation(
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

            light_is_on = await wait_for_entity_state(
                mcp_client, light_id, "on", timeout=10
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
