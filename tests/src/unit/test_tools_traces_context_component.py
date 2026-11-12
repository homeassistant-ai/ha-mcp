"""Component trace-store tests for ha_get_automation_traces, on real Core.

Core's ``trace/contexts`` map holds a run's own context, never the context that
triggered it, so a triggering (parent) context is resolved only through the
component's ``ha_mcp_tools/trace_context`` command, which scans Core's live
trace store in process. These tests run the tool's real ``resolve_global_context``
against a real ``hass`` whose ``ha_mcp_tools/*`` commands are registered, with
runs stored through Core's own ``async_store_trace``.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_traces import TraceTools

from ._trace_fixtures import automation_run, script_run, store_runs


@pytest.fixture(autouse=True)
async def trace_store(hass: HomeAssistant) -> None:
    """Set up Core's trace store, whose buckets the component reads."""
    assert await async_setup_component(hass, "trace", {})


class TestComponentContextLookup:
    """The component's live-store match under the server's resolver."""

    async def test_component_context_matches_without_a_public_scan(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
    ) -> None:
        """A parent context resolves from the live store, earliest match first."""
        # The stores run under a frozen clock so Core's own start timestamps
        # order the two runs; the tool call runs on real time because the
        # server's WebSocket client needs a live clock for its handshake.
        with freeze_time("2026-11-08T16:00:00+00:00") as clock:
            await store_runs(
                hass,
                automation_run("auto-run", context_id="own-auto", parent_id="parent-1"),
            )
            clock.tick(timedelta(minutes=1))
            await store_runs(
                hass,
                script_run("script-run", context_id="own-script", parent_id="parent-1"),
            )

        result = await TraceTools(component_core_client).ha_get_automation_traces(
            context_id="parent-1"
        )

        assert result["success"] is True
        assert result["context_match"] == "parent"
        assert result["context_id"] == "parent-1"
        assert result["context_scanned"] == 2
        assert (result["domain"], result["item_id"], result["run_id"]) == (
            "automation",
            "uid_1",
            "auto-run",
        )
        assert result["context_other_matches"] == [
            {
                "domain": "script",
                "item_id": "uid_2",
                "run_id": "script-run",
                "match": "parent",
            }
        ]
        assert result["trace"]["action/0"][0]["path"] == "action/0"
        assert "entity_id" not in result

    async def test_component_own_context_returns_a_trace_match(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
    ) -> None:
        """A run's own context is a ``trace`` match, not a ``parent`` one."""
        await store_runs(hass, automation_run("auto-run", context_id="own-auto"))

        result = await TraceTools(component_core_client).ha_get_automation_traces(
            context_id="own-auto"
        )

        assert result["context_match"] == "trace"
        assert result["context_scanned"] == 1
        assert result["run_id"] == "auto-run"

    async def test_component_no_match_is_authoritative(
        self,
        hass: HomeAssistant,
        component_core_client: HomeAssistantClient,
    ) -> None:
        """A component miss reports its scan and does not fall back to Core."""
        await store_runs(hass, automation_run("auto-run", context_id="own-auto"))

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(component_core_client).ha_get_automation_traces(
                context_id="missing"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error["context_id"] == "missing"
        assert error["scanned"] == 1
        hints = " ".join(error["error"]["suggestions"])
        # The component already checked the run's own context, so it must not
        # offer to install itself or to retry with a context id.
        assert "Install the ha_mcp_tools component" not in hints
        assert "Pass the trace's own `context.id`" not in hints
        assert "component checked" in hints
