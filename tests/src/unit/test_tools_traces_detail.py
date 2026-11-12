"""A detailed trace is Core's own extended dict for the run.

``ha_get_automation_traces`` reads a stored ``AutomationTrace`` from Core;
the run's extended dict holds the automation short dict (including
``trigger``), the raw step trace and the run's config/context.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.components.automation.trace import AutomationTrace
from homeassistant.components.trace import async_store_trace
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.trace import TraceElement
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import WebSocketGenerator

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_traces import TraceTools

from ._component_core import ws_result


@pytest.fixture
async def trace(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> dict[str, Any]:
    """Core's extended dict for one stored automation run, read over its API."""
    assert await async_setup_component(hass, "trace", {})
    # Core's config component serves the registry lookup of automation.test.
    assert await async_setup_component(hass, "config", {})
    er.async_get(hass).async_get_or_create(
        "automation", "automation", "uid_1", suggested_object_id="test"
    )
    run = AutomationTrace(
        "uid_1",
        {"alias": "Trace capture", "mode": "single"},
        {"target": "light.one"},
        Context(id="ctx-1"),
    )
    run.set_trigger_description("event 'go'")
    trigger = TraceElement({"trigger": {"event": {"data": {"go": False}}}}, "trigger/0")
    condition = TraceElement({"trigger": {"id": "evt_trig"}}, "condition/0")
    condition.set_result(result=False, state="below", wanted_state="above")
    action = TraceElement({"trigger": {"id": "evt_trig"}}, "action/0")
    action.set_result(params={"domain": "light"})
    action.set_error(RuntimeError("service failed"))
    run.set_trace(
        {"trigger/0": [trigger], "condition/0": [condition], "action/0": [action]}
    )
    run.finished()
    async_store_trace(hass, run, stored_traces=5)

    client = await hass_ws_client(hass)
    return await ws_result(
        client,
        "trace/get",
        domain="automation",
        item_id="uid_1",
        run_id=run.run_id,
    )


async def _detail(
    client: HomeAssistantClient, run_id: str, **options: Any
) -> dict[str, Any]:
    """``ha_get_automation_traces`` for one run of automation.test."""
    options.setdefault("entity_id", "automation.test")
    result: dict[str, Any] = await TraceTools(client).ha_get_automation_traces(
        run_id=run_id, **options
    )
    return result


class TestDetailedTrace:
    async def test_extended_dict_passes_through_unchanged(
        self, trace: dict[str, Any], core_client: HomeAssistantClient
    ) -> None:
        result = await _detail(core_client, trace["run_id"])

        assert result["success"] is True
        assert result["entity_id"] == "automation.test"
        assert result["run_id"] == trace["run_id"]
        assert result["trace"] == trace["trace"]
        assert result["config"] == trace["config"]
        assert result["blueprint_inputs"] == trace["blueprint_inputs"]
        assert result["trigger"] == trace["trigger"]
        # Core keeps a condition's state/wanted_state inside the step's result.
        condition = result["trace"]["condition/0"][0]
        assert condition["result"] == {
            "result": False,
            "state": "below",
            "wanted_state": "above",
        }
        assert "state" not in condition
        assert "condition_results" not in result
        assert "action_trace" not in result
        assert "config_summary" not in result

    async def test_sections_keep_top_level_keys(
        self, trace: dict[str, Any], core_client: HomeAssistantClient
    ) -> None:
        result = await _detail(core_client, trace["run_id"], sections="trace,config")

        assert set(result) == {
            "success",
            "entity_id",
            "run_id",
            "domain",
            "item_id",
            "trace",
            "config",
        }

    async def test_a_section_the_run_lacks_is_a_warning(
        self, trace: dict[str, Any], core_client: HomeAssistantClient
    ) -> None:
        """A requested name this run does not have is reported, not rejected."""
        result = await _detail(core_client, trace["run_id"], sections="unique_id")

        assert "unique_id" not in result
        assert len(result["warnings"]) == 1
        warning = result["warnings"][0]
        assert "unique_id" in warning
        listed = set(warning.split("This run has: ", 1)[1].split(", "))
        assert listed == set(trace)

    async def test_a_present_metadata_key_is_not_a_warning(
        self, trace: dict[str, Any], core_client: HomeAssistantClient
    ) -> None:
        """Core and tool keys the run carries satisfy their own section request."""
        result = await _detail(core_client, trace["run_id"], sections="success,run_id")

        assert result["success"] is True
        assert result["run_id"] == trace["run_id"]
        assert "warnings" not in result

    async def test_a_conditional_core_key_the_run_lacks_is_a_warning(
        self, trace: dict[str, Any], core_client: HomeAssistantClient
    ) -> None:
        """Core adds ``error`` only to a failed run; a successful run warns."""
        assert "error" not in trace

        result = await _detail(core_client, trace["run_id"], sections="trace,error")

        assert result["trace"] == trace["trace"]
        assert "error" not in result
        assert "error" in result["warnings"][0]

    @pytest.mark.parametrize("sections", ["", "   "])
    async def test_whitespace_sections_returns_the_full_dict(
        self, trace: dict[str, Any], core_client: HomeAssistantClient, sections: str
    ) -> None:
        result = await _detail(core_client, trace["run_id"], sections=sections)

        assert result["trace"] == trace["trace"]
        assert result["config"] == trace["config"]
        assert result["blueprint_inputs"] == trace["blueprint_inputs"]
        assert "warnings" not in result

    async def test_sections_keep_domain_and_item_metadata(
        self, trace: dict[str, Any], core_client: HomeAssistantClient
    ) -> None:
        result = await _detail(
            core_client,
            trace["run_id"],
            entity_id=None,
            domain="automation",
            item_id="uid_1",
            sections="trace",
        )

        assert set(result) == {
            "success",
            "run_id",
            "domain",
            "item_id",
            "trace",
        }
        assert "entity_id" not in result
