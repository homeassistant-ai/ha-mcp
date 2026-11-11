"""Context-id lookup tests for ha_get_automation_traces, on real Core.

The runs are real ``AutomationTrace`` objects stored through Core's
``async_store_trace`` and read back over the server's own client. Without the
component, Core's ``trace/contexts`` map is the only source: it holds each run's
own ``context.id``, never the context that triggered it, so a triggering
(parent) context is a not-found naming the component. The component path is
covered in ``test_tools_traces_context_component.py``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.tools_traces import TraceTools
from ha_mcp.tools.trace_lookup import resolve_global_context

from ._trace_fixtures import automation_run, store_runs


@pytest.fixture(autouse=True)
async def trace_store(hass: HomeAssistant) -> None:
    """Set up Core's trace store, which serves the ``trace/*`` commands."""
    assert await async_setup_component(hass, "trace", {})


class TestNoComponentContextLookup:
    """Core's own-context map, with the component absent."""

    async def test_the_own_context_map_resolves_the_run(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """Core's map holds each run's own context id and its trace key."""
        await store_runs(hass, automation_run("r1", context_id="ctx-1"))

        match = await resolve_global_context(core_client, "ctx-1")

        assert match.run_id == "r1"
        assert match.match == "trace"
        assert match.scanned == 0
        assert (match.domain, match.item_id) == ("automation", "uid_1")

    async def test_a_parent_context_is_not_found_with_a_component_hint(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """Core cannot resolve a triggering context, and no scan guesses it."""
        await store_runs(
            hass, automation_run("r1", context_id="own-1", parent_id="parent-1")
        )

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(core_client).ha_get_automation_traces(
                context_id="parent-1"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error["scanned"] == 0
        hints = " ".join(error["error"]["suggestions"])
        assert "Install the ha_mcp_tools component" in hints
        assert "parent" in hints

    @pytest.mark.parametrize(
        "selectors",
        [
            pytest.param(
                {"entity_id": "automation.test", "context_id": "missing"},
                id="entity",
            ),
            pytest.param(
                {"domain": "automation", "item_id": "uid_1", "context_id": "missing"},
                id="domain-and-item",
            ),
            pytest.param({"context_id": "missing"}, id="global"),
        ],
    )
    async def test_an_unknown_context_is_not_found(
        self, core_client: HomeAssistantClient, selectors: dict[str, Any]
    ) -> None:
        """Every selector entry point reports the same not-found hint."""
        with pytest.raises(ToolError) as exc_info:
            await TraceTools(core_client).ha_get_automation_traces(**selectors)

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        hints = " ".join(error["error"]["suggestions"])
        assert "component" in hints and "parent" in hints

    async def test_a_context_from_the_other_domain_is_rejected(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """Core's map says which domain owns the run, so ``script`` is refused."""
        await store_runs(hass, automation_run("r1", context_id="ctx-1"))

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(core_client).ha_get_automation_traces(
                context_id="ctx-1", domain="script"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert error["context_id"] == "ctx-1"
        assert error["domain"] == "script"

    async def test_domain_and_item_scope_the_context_to_that_item(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """domain+item_id+context_id asks Core only about that trace key."""
        await store_runs(hass, automation_run("r1", context_id="ctx-1"))

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(core_client).ha_get_automation_traces(
                domain="automation", item_id="uid_9", context_id="ctx-1"
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert error["item_id"] == "uid_9"


class TestTraceSelectorValidation:
    """Invalid selectors are refused before the tool reads anything."""

    @pytest.mark.parametrize(
        "selectors",
        [
            pytest.param(
                {
                    "entity_id": "automation.test",
                    "run_id": "r1",
                    "context_id": "ctx-1",
                },
                id="run-and-context",
            ),
            pytest.param(
                {"entity_id": "automation.test", "context_id": "   "},
                id="empty-context",
            ),
            pytest.param(
                {"item_id": "uid_1", "run_id": "r1"},
                id="item-without-domain",
            ),
            pytest.param(
                {
                    "entity_id": "automation.test",
                    "domain": "automation",
                    "item_id": "uid_1",
                },
                id="entity-and-item",
            ),
            pytest.param({"run_id": "r1"}, id="run-without-target"),
            pytest.param({}, id="no-selector"),
            pytest.param({"entity_id": "sensor.test"}, id="invalid-entity-prefix"),
        ],
    )
    async def test_a_rejected_selector_never_reaches_the_client(
        self,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
        selectors: dict[str, Any],
    ) -> None:
        """The client would report a dead transport, so a validation error
        proves no request was made."""
        monkeypatch.setattr(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("no answer")),
        )

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(core_client).ha_get_automation_traces(**selectors)

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"


class TestDetailLogbook:
    """A resolved run keeps its trace when the optional logbook read misbehaves."""

    @pytest.mark.parametrize(
        "error",
        [
            ToolError("logbook timed out"),
            HomeAssistantConnectionError("REST logbook unavailable"),
        ],
    )
    async def test_a_failed_logbook_read_keeps_the_trace_with_a_warning(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        error: Exception,
    ) -> None:
        """The run's trace is already in hand, so a logbook failure is a warning."""
        await store_runs(hass, automation_run("r1", context_id="ctx-1"))

        with patch(
            "ha_mcp.tools.tools_traces.fetch_trace_logbook",
            AsyncMock(side_effect=error),
        ):
            result = await TraceTools(core_client).ha_get_automation_traces(
                domain="automation",
                item_id="uid_1",
                run_id="r1",
                sections="trace,logbook",
            )

        assert result["trace"]["action/0"][0]["path"] == "action/0"
        assert result["logbook_entries"] == []
        assert any("logbook unavailable" in warning for warning in result["warnings"])
