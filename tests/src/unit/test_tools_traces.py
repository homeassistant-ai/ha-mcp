"""Unit tests for tools_traces: listing, paging and trace-key resolution.

The rows come from Home Assistant Core itself: real ``AutomationTrace`` runs
stored through Core's ``async_store_trace``, read back over Core's WebSocket
``trace/list``, and the server's own client talking to the real ``hass``.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Literal
from unittest.mock import AsyncMock, MagicMock

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.automation.trace import AutomationTrace
from homeassistant.components.trace import async_store_trace
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import WebSocketGenerator

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.tools_traces import (
    TraceTools,
    _format_trace_list,
    _resolve_trace_item_id,
)
from ha_mcp.tools.trace_lookup import _raise_trace_ws_failure, resolve_context_trace

from ._component_core import ws_result

_STORED_TRACES = 10

# The context each trace command's failure carries into its structured error,
# as the list, detail and context call sites build it.
_COMMAND_CONTEXTS: dict[str, dict[str, Any]] = {
    "trace/list": {
        "entity_id": "automation.test",
        "domain": "automation",
        "item_id": "uid_1",
    },
    "trace/get": {
        "entity_id": "automation.test",
        "domain": "automation",
        "item_id": "uid_1",
        "run_id": "r1",
    },
    "trace/contexts": {
        "context_id": "ctx-1",
        "domain": "automation",
        "item_id": "uid_1",
    },
}


@pytest.fixture
async def registry_api(hass: HomeAssistant) -> None:
    """Core's config component, which serves ``config/entity_registry/get``."""
    assert await async_setup_component(hass, "config", {})


def _run(run_id: str, *, not_triggered: bool = False) -> AutomationTrace:
    """One run Core stores for ``automation.uid_1``, created in call order."""
    trace = AutomationTrace(
        "uid_1",
        {"alias": "Trace capture"},
        None,
        Context(id=f"ctx-{run_id}"),
        not_triggered=not_triggered,
    )
    # A chosen run id keeps the rows addressable; Core's own would be random.
    trace.run_id = run_id
    trace.set_trigger_description("state change")
    trace.finished()
    return trace


async def _store(hass: HomeAssistant, *traces: AutomationTrace) -> None:
    """Set up Core's trace store and save ``traces`` through its own helper."""
    assert await async_setup_component(hass, "trace", {})
    for trace in traces:
        async_store_trace(hass, trace, stored_traces=_STORED_TRACES)


async def _core_rows(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> list[dict[str, Any]]:
    """Core's own ``trace/list`` answer for ``automation.uid_1``."""
    client = await hass_ws_client(hass)
    rows = await ws_result(client, "trace/list", domain="automation", item_id="uid_1")
    assert isinstance(rows, list)
    return rows


class TestFormatTraceList:
    """The list response Core's rows page into."""

    async def test_diagnostics_accompany_only_an_empty_list(
        self, hass: HomeAssistant, hass_ws_client: WebSocketGenerator
    ) -> None:
        """Diagnostics describe an empty list and never a populated one."""
        diagnostics = {
            "entity_exists": True,
            "entity_enabled": True,
            "trace_storage_enabled": True,
            "last_triggered": "2025-11-30T15:00:00Z",
            "suggestion": "Traces may have been cleared.",
        }
        await _store(hass, _run("r1"))
        rows = await _core_rows(hass, hass_ws_client)

        empty = _format_trace_list("automation.test", [], 10, diagnostics)
        no_diagnostics = _format_trace_list("automation.test", [], 10)
        populated = _format_trace_list("automation.test", rows, 10, diagnostics)

        assert empty["diagnostics"] == diagnostics
        assert no_diagnostics["trace_count"] == 0
        assert "diagnostics" not in no_diagnostics
        assert populated["trace_count"] == 1
        assert "diagnostics" not in populated

    @pytest.mark.parametrize(
        ("limit", "offset", "order", "expected_run_ids", "has_more"),
        [
            pytest.param(
                3, 0, "newest", ["run_4", "run_3", "run_2"], True, id="newest-window"
            ),
            pytest.param(2, 0, "newest", ["run_4", "run_3"], True, id="small-page"),
            pytest.param(
                10,
                0,
                "newest",
                ["run_4", "run_3", "run_2", "run_1", "run_0"],
                False,
                id="all-newest-first",
            ),
            pytest.param(
                3, 0, "oldest", ["run_0", "run_1", "run_2"], True, id="oldest-window"
            ),
            pytest.param(2, 2, "newest", ["run_2", "run_1"], True, id="offset-newest"),
            pytest.param(2, 2, "oldest", ["run_2", "run_3"], True, id="offset-oldest"),
            pytest.param(10, 5, "newest", [], False, id="offset-equals-total"),
            pytest.param(10, 10, "newest", [], False, id="offset-beyond-total"),
        ],
    )
    async def test_pages_traces(
        self,
        hass: HomeAssistant,
        hass_ws_client: WebSocketGenerator,
        freezer: FrozenDateTimeFactory,
        limit: int,
        offset: int,
        order: Literal["newest", "oldest"],
        expected_run_ids: list[str],
        has_more: bool,
    ) -> None:
        """limit/offset/order select one window of Core's sorted rows.

        The clock moves a minute between runs, so Core's own start timestamps
        order them ``run_0``..``run_4`` regardless of the order ``trace/list``
        returns.
        """
        traces = []
        for i in range(5):
            traces.append(_run(f"run_{i}"))
            freezer.tick(timedelta(minutes=1))
        await _store(hass, *traces)
        rows = await _core_rows(hass, hass_ws_client)

        result = _format_trace_list(
            "automation.test", rows, limit, offset=offset, order=order
        )

        assert [trace["run_id"] for trace in result["traces"]] == expected_run_ids
        assert result["trace_count"] == len(expected_run_ids)
        assert result["total_available"] == 5
        assert result["offset"] == offset
        assert result["order"] == order
        assert result["has_more"] is has_more

    async def test_list_preserves_core_short_dict_fields(
        self, hass: HomeAssistant, hass_ws_client: WebSocketGenerator
    ) -> None:
        """Every field Core's trace/list returns reaches the caller unchanged."""
        await _store(hass, _run("r1"))
        core_rows = await _core_rows(hass, hass_ws_client)

        result = _format_trace_list("automation.test", core_rows, 10)

        assert result["traces"] == core_rows

    async def test_list_orders_mixed_not_triggered_by_core_start_time(
        self,
        hass: HomeAssistant,
        hass_ws_client: WebSocketGenerator,
        freezer: FrozenDateTimeFactory,
    ) -> None:
        """Run and not-triggered traces page in Core's timestamp order."""
        traces = []
        for run_id, not_triggered in (
            ("run_oldest", False),
            ("not_triggered_middle", True),
            ("run_newest", False),
        ):
            traces.append(_run(run_id, not_triggered=not_triggered))
            freezer.tick(timedelta(minutes=1))
        await _store(hass, *traces)
        rows = await _core_rows(hass, hass_ws_client)

        newest = _format_trace_list("automation.test", rows, 2)
        oldest = _format_trace_list("automation.test", rows, 2, order="oldest")

        assert [trace["run_id"] for trace in newest["traces"]] == [
            "run_newest",
            "not_triggered_middle",
        ]
        assert [trace["run_id"] for trace in oldest["traces"]] == [
            "run_oldest",
            "not_triggered_middle",
        ]


# Still imported by the trace context, logbook, diagnostics and rename tests;
# it leaves with them when they move to real Core.
def _make_pooled_client(dispatch):
    """Build a mock REST client whose ``send_websocket_message`` dispatches on
    message type via ``dispatch`` (``(type, message) -> response dict``).

    Mirrors the pooled client tools_traces now drives (issue #1813 item #5):
    a single ``send_websocket_message`` owns every trace WS command, so there is
    no dedicated connect/auth/disconnect per call.
    """
    client = MagicMock()
    client.base_url = "http://test.local:8123"
    client.token = "test-token"
    client.verify_ssl = True

    async def _send(message):
        return dispatch(message["type"], message)

    client.send_websocket_message = AsyncMock(side_effect=_send)
    client.get_entity_state = AsyncMock(return_value={"state": "on", "attributes": {}})
    return client


def _dispatch(*, list_result=None, detail_result=None, context_result=None):
    """Return a pooled-client dispatcher for trace commands."""

    def _d(mtype, message):
        if mtype == "config/entity_registry/get":
            return {"success": True, "result": {"unique_id": "uid_1"}}
        if mtype == "trace/list":
            return (
                list_result
                if list_result is not None
                else {"success": True, "result": []}
            )
        if mtype == "trace/get":
            return detail_result
        if mtype == "trace/contexts":
            return context_result
        if mtype in ("automation/config", "script/config"):
            return {"success": True, "result": {}}
        return {"success": False, "error": f"unexpected {mtype}"}

    return _d


class TestResolveTraceItemId:
    """``_resolve_trace_item_id`` reads Core's entity registry."""

    async def test_resolves_the_registry_unique_id(
        self,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        registry_api: None,
    ) -> None:
        """Core's registry row holds the unique_id traces are stored under."""
        entry = entity_registry.async_get_or_create("automation", "test", "uid_42")

        item_id = await _resolve_trace_item_id(
            core_client, entry.entity_id, "fallback_obj"
        )

        assert item_id == "uid_42"

    async def test_an_entity_core_does_not_know_falls_back_to_the_object_id(
        self, core_client: HomeAssistantClient, registry_api: None
    ) -> None:
        """A registry miss leaves the object_id so the trace fetch reports it."""
        item_id = await _resolve_trace_item_id(
            core_client, "automation.missing", "fallback_obj"
        )

        assert item_id == "fallback_obj"

    async def test_a_dead_transport_falls_back_to_the_object_id(
        self,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A client that gets no answer also leaves the object_id."""
        monkeypatch.setattr(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("down")),
        )

        item_id = await _resolve_trace_item_id(
            core_client, "automation.test", "fallback_obj"
        )

        assert item_id == "fallback_obj"


class TestResolveContextTrace:
    """A run's own context is resolved from Core's ``trace/contexts`` map."""

    async def test_own_context_resolves_to_the_run(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """Core's own-context map holds the stored run, and answers it."""
        await _store(hass, _run("r1"))

        match = await resolve_context_trace(
            core_client, "automation", "uid_1", "ctx-r1"
        )

        assert match.run_id == "r1"
        assert match.match == "trace"
        assert match.domain == "automation"
        assert match.item_id == "uid_1"
        assert match.scanned == 0


class TestTraceFetchFailures:
    """The failures the trace commands surface as structured errors."""

    async def test_a_dead_transport_maps_to_connection_failed(
        self,
        core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The client getting no answer is the one failure Core cannot answer."""
        monkeypatch.setattr(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("no answer")),
        )

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(core_client).ha_get_automation_traces(
                entity_id="automation.test"
            )

        error = json.loads(str(exc_info.value))
        assert error["success"] is False
        assert error["error"]["code"] == "CONNECTION_FAILED"

    @pytest.mark.parametrize(
        ("command", "error", "code"),
        [
            pytest.param(
                "trace/list",
                "Invalid command",
                "SERVICE_CALL_FAILED",
                id="list-command-error",
            ),
            pytest.param(
                "trace/get",
                "The trace could not be found",
                "SERVICE_CALL_FAILED",
                id="detail-not-found",
            ),
            pytest.param(
                "trace/contexts",
                "Failed to connect to Home Assistant",
                "CONNECTION_FAILED",
                id="connection-shaped-answer",
            ),
            pytest.param(
                "trace/contexts",
                "WebSocket connection to Home Assistant closed while waiting for a response",
                "CONNECTION_FAILED",
                id="transport-shaped-answer",
            ),
            pytest.param(
                "trace/contexts",
                "Unknown command.",
                "SERVICE_CALL_FAILED",
                id="contexts-command-error",
            ),
        ],
    )
    def test_ws_failure_is_classified_by_its_message(
        self, command: str, error: str, code: str
    ) -> None:
        """A command's error answer keeps its context and picks its error code."""
        context = _COMMAND_CONTEXTS[command]

        with pytest.raises(ToolError) as exc_info:
            _raise_trace_ws_failure(error, context)

        payload = json.loads(str(exc_info.value))
        assert payload["success"] is False
        assert payload["error"]["code"] == code
        assert {key: payload[key] for key in context} == context

    @pytest.mark.parametrize(
        "options",
        [
            pytest.param({"sections": "conditions"}, id="sections"),
            pytest.param({"detailed": True}, id="detailed"),
        ],
    )
    async def test_detail_options_need_a_resolved_run(
        self,
        core_client: HomeAssistantClient,
        registry_api: None,
        options: dict[str, Any],
    ) -> None:
        """sections and detailed are only meaningful when a run is resolved."""
        with pytest.raises(ToolError) as exc_info:
            await TraceTools(core_client).ha_get_automation_traces(
                entity_id="automation.test", **options
            )

        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
