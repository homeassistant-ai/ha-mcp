"""Unit tests for tools_traces: listing, paging and trace-key resolution.

The rows come from Home Assistant Core itself: real ``AutomationTrace`` runs
stored through Core's ``async_store_trace``, read back over Core's WebSocket
``trace/list``, and the server's own client talking to the real ``hass``.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any, Literal
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time
from homeassistant.components.automation.trace import AutomationTrace
from homeassistant.components.trace import async_store_trace
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant.typing import WebSocketGenerator

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.tools_traces import TraceTools
from ha_mcp.tools.trace_lookup import resolve_context_trace

from ._component_core import ws_result

_STORED_TRACES = 10


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


def _runs_a_minute_apart(*runs: tuple[str, bool]) -> list[AutomationTrace]:
    """Runs whose start timestamps are a minute apart, oldest first.

    Only their creation is frozen: the event loop reads the same clock, and the
    client's connection waits never wake while it stands still.
    """
    built = []
    with freeze_time(dt_util.utcnow() - timedelta(hours=1)) as clock:
        for run_id, not_triggered in runs:
            built.append(_run(run_id, not_triggered=not_triggered))
            clock.tick(timedelta(minutes=1))
    return built


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
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        registry_api: None,
    ) -> None:
        """Diagnostics explain why an automation has no traces, and never
        accompany a populated list or a list asked for by item id."""
        er.async_get(hass).async_get_or_create(
            "automation", "automation", "uid_1", suggested_object_id="test"
        )
        assert await async_setup_component(hass, "trace", {})
        tools = TraceTools(core_client)

        empty = await tools.ha_get_automation_traces(entity_id="automation.test")
        by_item = await tools.ha_get_automation_traces(
            domain="automation", item_id="uid_1"
        )
        await _store(hass, _run("r1"))
        populated = await tools.ha_get_automation_traces(entity_id="automation.test")

        assert empty["trace_count"] == 0
        assert empty["diagnostics"]["entity_exists"] is True
        assert by_item["trace_count"] == 0
        assert "diagnostics" not in by_item
        assert populated["trace_count"] == 1
        assert "diagnostics" not in populated

    @pytest.mark.parametrize(
        ("limit", "offset", "order", "expected_run_ids", "has_more"),
        [
            pytest.param(
                3, 0, "newest", ["run_4", "run_3", "run_2"], True, id="newest-window"
            ),
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
        ],
    )
    async def test_pages_traces(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        limit: int,
        offset: int,
        order: Literal["newest", "oldest"],
        expected_run_ids: list[str],
        has_more: bool,
    ) -> None:
        """limit/offset/order select one window of Core's sorted rows.

        The runs start a minute apart, so Core's own start timestamps order
        them ``run_0``..``run_4`` regardless of the order ``trace/list``
        returns.
        """
        await _store(
            hass, *_runs_a_minute_apart(*((f"run_{i}", False) for i in range(5)))
        )

        result = await TraceTools(core_client).ha_get_automation_traces(
            domain="automation",
            item_id="uid_1",
            limit=limit,
            offset=offset,
            order=order,
        )

        assert [trace["run_id"] for trace in result["traces"]] == expected_run_ids
        assert result["trace_count"] == len(expected_run_ids)
        assert result["total_available"] == 5
        assert result["offset"] == offset
        assert result["order"] == order
        assert result["has_more"] is has_more

    async def test_list_preserves_core_short_dict_fields(
        self,
        hass: HomeAssistant,
        hass_ws_client: WebSocketGenerator,
        core_client: HomeAssistantClient,
    ) -> None:
        """Every field Core's trace/list returns reaches the caller unchanged."""
        await _store(hass, _run("r1"))
        core_rows = await _core_rows(hass, hass_ws_client)

        result = await TraceTools(core_client).ha_get_automation_traces(
            domain="automation", item_id="uid_1"
        )

        assert result["traces"] == core_rows

    async def test_list_orders_mixed_not_triggered_by_core_start_time(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
    ) -> None:
        """Run and not-triggered traces page in Core's timestamp order."""
        await _store(
            hass,
            *_runs_a_minute_apart(
                ("run_oldest", False),
                ("not_triggered_middle", True),
                ("run_newest", False),
            ),
        )

        tools = TraceTools(core_client)
        newest = await tools.ha_get_automation_traces(
            domain="automation", item_id="uid_1", limit=2
        )
        oldest = await tools.ha_get_automation_traces(
            domain="automation", item_id="uid_1", limit=2, order="oldest"
        )

        assert [trace["run_id"] for trace in newest["traces"]] == [
            "run_newest",
            "not_triggered_middle",
        ]
        assert [trace["run_id"] for trace in oldest["traces"]] == [
            "run_oldest",
            "not_triggered_middle",
        ]


class TestTraceToolOptions:
    """Tool-level option handling that does not change Core's answers."""

    @pytest.mark.parametrize("sections", ["", "   "])
    async def test_an_empty_sections_string_lists_normally(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        sections: str,
    ) -> None:
        """A blank sections string is omitted, not a detail-only option."""
        await _store(hass, _run("r1"))

        result = await TraceTools(core_client).ha_get_automation_traces(
            domain="automation", sections=sections
        )

        assert result["success"] is True
        assert [trace["run_id"] for trace in result["traces"]] == ["r1"]

    async def test_an_unknown_item_id_warns_it_may_be_wrong(
        self, hass: HomeAssistant, core_client: HomeAssistantClient
    ) -> None:
        """A domain+item_id list Core has no runs for says the id may be wrong."""
        await _store(hass, _run("r1"))

        result = await TraceTools(core_client).ha_get_automation_traces(
            domain="automation", item_id="evening_lights"
        )

        assert result["success"] is True
        assert result["trace_count"] == 0
        assert any(
            "No traces found" in warning and "evening_lights" in warning
            for warning in result["warnings"]
        )


class TestTraceItemResolution:
    """An entity id is resolved through Core's entity registry to the key
    Core stores its traces under."""

    async def test_the_registry_unique_id_selects_the_stored_runs(
        self,
        hass: HomeAssistant,
        entity_registry: er.EntityRegistry,
        core_client: HomeAssistantClient,
        registry_api: None,
    ) -> None:
        """Core stores an automation's runs under its unique_id, not under the
        entity id's object id."""
        entry = entity_registry.async_get_or_create(
            "automation", "automation", "uid_1", suggested_object_id="renamed"
        )
        await _store(hass, _run("r1"))

        result = await TraceTools(core_client).ha_get_automation_traces(
            entity_id=entry.entity_id
        )

        assert [trace["run_id"] for trace in result["traces"]] == ["r1"]

    async def test_an_entity_core_does_not_know_falls_back_to_the_object_id(
        self, hass: HomeAssistant, core_client: HomeAssistantClient, registry_api: None
    ) -> None:
        """A registry miss reads the runs stored under the entity's object id."""
        run = AutomationTrace(
            "uid_1", {"alias": "Trace capture"}, None, Context(id="ctx-fallback")
        )
        run.run_id = "fallback_run"
        run.finished()
        await _store(hass, run)

        result = await TraceTools(core_client).ha_get_automation_traces(
            entity_id="automation.uid_1"
        )

        assert [trace["run_id"] for trace in result["traces"]] == ["fallback_run"]


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
        ("trace_store", "call", "context"),
        [
            pytest.param(
                False,
                {"domain": "automation", "item_id": "uid_1"},
                {"domain": "automation", "item_id": "uid_1"},
                id="list-without-a-trace-store",
            ),
            pytest.param(
                True,
                {"domain": "automation", "item_id": "uid_1", "run_id": "no_such_run"},
                {"domain": "automation", "item_id": "uid_1", "run_id": "no_such_run"},
                id="detail-of-a-missing-run",
            ),
            pytest.param(
                False,
                {"domain": "automation", "item_id": "uid_1", "context_id": "ctx-1"},
                {"domain": "automation", "item_id": "uid_1", "context_id": "ctx-1"},
                id="context-without-a-trace-store",
            ),
        ],
    )
    async def test_a_trace_command_core_refuses_keeps_its_context(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        trace_store: bool,
        call: dict[str, Any],
        context: dict[str, Any],
    ) -> None:
        """Core's error answer to a trace command is a service failure that
        names what was asked for."""
        if trace_store:
            await _store(hass)

        with pytest.raises(ToolError) as exc_info:
            await TraceTools(core_client).ha_get_automation_traces(**call)

        payload = json.loads(str(exc_info.value))
        assert payload["success"] is False
        assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
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
