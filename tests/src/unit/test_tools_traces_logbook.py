"""Native logbook lookup tests for trace debugging, on real Core.

The rows come from Core's own logbook/recorder query, and the run is a real
``AutomationTrace`` stored through Core's ``async_store_trace`` and read back
over Core's ``trace/get``. The component path runs in Core's process; without
the component the same window is read through the REST view.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.automation.trace import AutomationTrace
from homeassistant.components.recorder import Recorder
from homeassistant.components.trace import async_store_trace
from homeassistant.core import Context, HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockModule, mock_integration
from pytest_homeassistant.components.recorder.common import async_wait_recording_done
from pytest_homeassistant.typing import WebSocketGenerator

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandTimeout,
)
from ha_mcp.tools.component_api import get_component_caps
from ha_mcp.tools.trace_logbook import LOGBOOK_ROW_LIMIT, fetch_trace_logbook

from ._component_core import ws_result

_STORED_TRACES = 10


@pytest.fixture(autouse=True)
async def core_logbook(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    """Core's logbook integration, backed by the real in-memory recorder.

    ``recorder_mock`` is requested before ``hass`` because its database fixture
    refuses to run once Home Assistant exists; pytest resolves the arguments in
    order. The logbook integration depends on ``frontend``, which the unit test
    install does not ship, so the dependency is a Core ``mock_integration``;
    logbook then registers its own processor, query and REST view.
    """
    mock_integration(hass, MockModule("frontend"))
    assert await async_setup_component(hass, "logbook", {})
    assert await async_setup_component(hass, "trace", {})


async def _trace(
    hass: HomeAssistant,
    hass_ws_client: WebSocketGenerator,
    *,
    context: Context,
) -> dict[str, Any]:
    """Store a run for ``automation.uid_1`` and return Core's extended trace."""
    trace = AutomationTrace("uid_1", {"alias": "Trace capture"}, None, context)
    trace.run_id = "r1"
    trace.finished()
    async_store_trace(hass, trace, stored_traces=_STORED_TRACES)
    extended = await ws_result(
        await hass_ws_client(hass),
        "trace/get",
        domain="automation",
        item_id="uid_1",
        run_id="r1",
    )
    assert isinstance(extended, dict)
    return extended


async def _record(
    hass: HomeAssistant,
    changes: list[tuple[str, str, Context]],
) -> None:
    """Record one visible state change per ``(entity_id, state, context)``.

    Core's logbook shows a state row only when the entity has a previous
    recorded state, so each entity moves once without a chosen context and
    then to the state under the row's context.
    """
    for entity_id, _state, _context in changes:
        hass.states.async_set(entity_id, "initial")
    for entity_id, state, context in changes:
        hass.states.async_set(entity_id, state, context=context)
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)


class TestFetchTraceLogbook:
    @pytest.mark.asyncio
    async def test_component_reads_the_runs_start_and_context(
        self,
        hass: HomeAssistant,
        hass_ws_client: WebSocketGenerator,
        component_core_client: HomeAssistantClient,
    ) -> None:
        """Core's processor answers the run's own context, not another run."""
        context = Context()
        trace = await _trace(hass, hass_ws_client, context=context)
        await _record(
            hass,
            [
                ("light.matched", "on", context),
                ("light.other", "on", Context()),
            ],
        )

        logbook = await fetch_trace_logbook(component_core_client, trace)

        assert logbook.transport == "component"
        assert [(row["entity_id"], row["state"]) for row in logbook.entries] == [
            ("light.matched", "on")
        ]
        assert logbook.warnings == []

    @pytest.mark.asyncio
    async def test_without_the_component_the_rest_view_serves_the_same_rows(
        self,
        hass: HomeAssistant,
        hass_ws_client: WebSocketGenerator,
        core_client: HomeAssistantClient,
    ) -> None:
        """The REST fallback reads the same run's context from Core."""
        context = Context()
        trace = await _trace(hass, hass_ws_client, context=context)
        await _record(hass, [("light.matched", "on", context)])

        logbook = await fetch_trace_logbook(core_client, trace)

        assert logbook.transport == "rest"
        assert [(row["entity_id"], row["state"]) for row in logbook.entries] == [
            ("light.matched", "on")
        ]

    @pytest.mark.asyncio
    async def test_rows_are_capped_and_the_total_is_core_s(
        self,
        hass: HomeAssistant,
        hass_ws_client: WebSocketGenerator,
        component_core_client: HomeAssistantClient,
    ) -> None:
        """Core returns every row; the trace page caps the list it shows."""
        context = Context()
        trace = await _trace(hass, hass_ws_client, context=context)
        changes = [
            (f"light.row_{index}", f"state_{index}", context)
            for index in range(LOGBOOK_ROW_LIMIT + 5)
        ]
        await _record(hass, changes)

        logbook = await fetch_trace_logbook(component_core_client, trace)

        assert len(logbook.entries) == LOGBOOK_ROW_LIMIT
        assert logbook.total == len(changes)
        assert logbook.truncated is True

    @pytest.mark.asyncio
    async def test_a_component_timeout_is_not_replayed_over_rest(
        self,
        hass: HomeAssistant,
        hass_ws_client: WebSocketGenerator,
        component_core_client: HomeAssistantClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A recorder query still running inside Core is not run again."""
        context = Context()
        trace = await _trace(hass, hass_ws_client, context=context)
        await _record(hass, [("light.matched", "on", context)])
        # Cache the real capability answer before the one request loses its
        # transport, so the failure is the logbook query itself.
        await get_component_caps(component_core_client)
        monkeypatch.setattr(
            component_core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantCommandTimeout("timed out")),
        )

        with pytest.raises(ToolError) as exc_info:
            await fetch_trace_logbook(component_core_client, trace)

        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "TIMEOUT_OPERATION"

    @pytest.mark.asyncio
    async def test_a_component_rejection_surfaces_instead_of_falling_back(
        self,
        hass: HomeAssistant,
        hass_ws_client: WebSocketGenerator,
        component_core_client: HomeAssistantClient,
    ) -> None:
        """Core's own rejection is authoritative; REST never answers for it."""
        context = Context()
        trace = await _trace(hass, hass_ws_client, context=context)
        await _record(hass, [("light.matched", "on", context)])
        unparseable = {**trace, "timestamp": {"start": "not-a-time"}}

        with pytest.raises(ToolError) as exc_info:
            await fetch_trace_logbook(component_core_client, unparseable)

        payload = json.loads(str(exc_info.value))
        assert payload["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "Invalid start_time" in payload["error"]["message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "trace",
        [
            pytest.param(
                {"timestamp": {"start": "2026-11-08T16:00:00+00:00"}},
                id="no-context",
            ),
            pytest.param({"context": {"id": "ctx-1"}}, id="no-start"),
        ],
    )
    async def test_a_run_without_its_context_or_start_reads_nothing(
        self, core_client: HomeAssistantClient, trace: dict[str, Any]
    ) -> None:
        """Without both fields Core's query cannot be addressed, so it is skipped."""
        logbook = await fetch_trace_logbook(core_client, trace)

        assert logbook.entries == []
        assert logbook.transport is None
        assert len(logbook.warnings) == 1
