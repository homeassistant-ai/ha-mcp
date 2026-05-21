"""Unit tests for FastMCP Context injection in long-running tools.

Each tool is verified twice:
- legacy path: called with ``ctx=None`` (or omitted) — must work unchanged
- progress path: called with a fake ``Context`` whose ``report_progress`` and
  ``info`` are AsyncMock — those must be awaited at the expected boundaries

A third group of tests exercises the safe-emit wrapper: when ``ctx.report_progress``
raises a transport error, the tool must still return its success payload.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_hacs import HacsTools
from ha_mcp.tools.tools_history import HistoryTools
from ha_mcp.tools.tools_traces import TraceTools


def _make_ctx() -> MagicMock:
    """Build a fake FastMCP Context with the awaitable surface we use."""
    ctx = MagicMock()
    ctx.report_progress = AsyncMock()
    ctx.info = AsyncMock()
    ctx.debug = AsyncMock()
    ctx.warning = AsyncMock()
    ctx.error = AsyncMock()
    return ctx


def _mock_ha_client() -> MagicMock:
    """Minimal mock HomeAssistantClient sufficient for these unit paths."""
    client = MagicMock()
    client.base_url = "http://homeassistant.local"
    client.token = "test_token"
    client.verify_ssl = True
    # add_timezone_metadata awaits get_config() — must be async.
    client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
    return client


def _progress_messages(ctx: MagicMock) -> list[str]:
    """Extract the ``message`` kwarg from every ``report_progress`` call."""
    return [c.kwargs.get("message", "") for c in ctx.report_progress.await_args_list]


def _assert_progress_call(
    call: Any,
    *,
    progress: float,
    total: float,
    message_contains: str,
) -> None:
    """Pin progress/total exactly and require a substring in ``message``."""
    assert call.kwargs["progress"] == progress, call.kwargs
    assert call.kwargs["total"] == total, call.kwargs
    assert message_contains in call.kwargs["message"], call.kwargs


# ---------------------------------------------------------------------------
# smart_search.deep_search (the engine behind ha_deep_search)
# ---------------------------------------------------------------------------


@pytest.fixture
def smart_search_tools() -> SmartSearchTools:
    client = _mock_ha_client()
    # No entities → all phases short-circuit cleanly without further mocking.
    client.get_states = AsyncMock(return_value=[])
    # Helper phase issues input_*/list WebSocket calls; succeed with empty results.
    client.send_websocket_message = AsyncMock(return_value={"success": True, "result": []})
    return SmartSearchTools(client=client)


@pytest.mark.asyncio
async def test_deep_search_works_without_ctx(smart_search_tools: SmartSearchTools) -> None:
    """Legacy callers passing no ctx still get a normal result dict."""
    result = await smart_search_tools.deep_search(
        "anything", search_types=["helper"], limit=5
    )
    assert result["success"] is True
    assert result["query"] == "anything"
    assert "helpers" in result


@pytest.mark.asyncio
async def test_deep_search_emits_progress_with_ctx(
    smart_search_tools: SmartSearchTools,
) -> None:
    """With a Context supplied, progress + info events are awaited."""
    ctx = _make_ctx()
    result = await smart_search_tools.deep_search(
        "anything", search_types=["helper"], limit=5, ctx=ctx
    )
    assert result["success"] is True
    ctx.info.assert_awaited()
    # Initial progress + post-fetch + post-helper-phase
    assert ctx.report_progress.await_count >= 3
    calls = ctx.report_progress.await_args_list
    _assert_progress_call(
        calls[0], progress=0, total=2, message_contains="fetching entity states"
    )
    _assert_progress_call(
        calls[1], progress=1, total=2, message_contains="entity states"
    )
    # Final event should mention helpers and equal total_phases.
    _assert_progress_call(
        calls[-1], progress=2, total=2, message_contains="helpers searched"
    )


# ---------------------------------------------------------------------------
# tools_history.HistoryTools.ha_get_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ha_get_history_works_without_ctx() -> None:
    """ha_get_history runs end-to-end with ctx omitted."""
    client = _mock_ha_client()
    history_tool = HistoryTools(client).ha_get_history

    fake_ws = AsyncMock()
    fake_ws.disconnect = AsyncMock()
    fake_result = {"success": True, "source": "history", "entities": []}

    with (
        patch(
            "ha_mcp.tools.tools_history.get_connected_ws_client",
            new=AsyncMock(return_value=(fake_ws, None)),
        ),
        patch(
            "ha_mcp.tools.tools_history._fetch_history",
            new=AsyncMock(return_value=fake_result),
        ),
    ):
        result = await history_tool(entity_ids="sensor.test")

    # ha_get_history wraps the inner _fetch_history result via add_timezone_metadata
    # — the inner payload must round-trip unchanged under fields=None.
    assert result["data"] == fake_result
    assert "metadata" in result
    fake_ws.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_ha_get_history_emits_progress_with_ctx() -> None:
    """ha_get_history emits at least the connect / query / done events."""
    client = _mock_ha_client()
    history_tool = HistoryTools(client).ha_get_history
    ctx = _make_ctx()

    fake_ws = AsyncMock()
    fake_ws.disconnect = AsyncMock()
    fake_result = {"success": True, "source": "history", "entities": []}

    with (
        patch(
            "ha_mcp.tools.tools_history.get_connected_ws_client",
            new=AsyncMock(return_value=(fake_ws, None)),
        ),
        patch(
            "ha_mcp.tools.tools_history._fetch_history",
            new=AsyncMock(return_value=fake_result),
        ),
    ):
        result = await history_tool(entity_ids="sensor.test", ctx=ctx)

    assert result["data"] == fake_result
    assert "metadata" in result
    ctx.info.assert_awaited()
    # Three events: connect, query dispatch, completion (progress jumps 1 -> 3).
    assert ctx.report_progress.await_count == 3
    calls = ctx.report_progress.await_args_list
    _assert_progress_call(
        calls[0], progress=0, total=3, message_contains="connecting"
    )
    _assert_progress_call(
        calls[1], progress=1, total=3, message_contains="querying recorder (history)"
    )
    _assert_progress_call(
        calls[2], progress=3, total=3, message_contains="recorder query complete"
    )


# ---------------------------------------------------------------------------
# tools_traces.TraceTools.ha_get_automation_traces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ha_get_automation_traces_works_without_ctx() -> None:
    """ha_get_automation_traces is callable without ctx."""
    client = _mock_ha_client()
    client.get_entity_state = AsyncMock(
        return_value={"state": "on", "attributes": {"id": "abc"}}
    )
    trace_tool = TraceTools(client).ha_get_automation_traces

    fake_ws = AsyncMock()
    fake_ws.disconnect = AsyncMock()
    fake_ws.send_command = AsyncMock(return_value={"success": True, "result": []})

    with (
        patch(
            "ha_mcp.tools.tools_traces.get_connected_ws_client",
            new=AsyncMock(return_value=(fake_ws, None)),
        ),
        patch(
            "ha_mcp.tools.tools_traces._resolve_trace_item_id",
            new=AsyncMock(return_value="abc"),
        ),
    ):
        result = await trace_tool(automation_id="automation.demo")

    assert result["success"] is True
    assert result["trace_count"] == 0


@pytest.mark.asyncio
async def test_ha_get_automation_traces_emits_progress_with_ctx() -> None:
    """ha_get_automation_traces emits info + at least 3 progress events."""
    client = _mock_ha_client()
    client.get_entity_state = AsyncMock(
        return_value={"state": "on", "attributes": {"id": "abc"}}
    )
    trace_tool = TraceTools(client).ha_get_automation_traces
    ctx = _make_ctx()

    fake_ws = AsyncMock()
    fake_ws.disconnect = AsyncMock()
    # Return a non-empty trace list so we follow the standard "list" branch
    # and skip the diagnostics gather, keeping the progress-event count
    # deterministic at the expected 3.
    fake_ws.send_command = AsyncMock(
        return_value={
            "success": True,
            "result": [
                {"run_id": "1.0", "timestamp": "2025-01-01T00:00:00Z", "state": "stopped"}
            ],
        }
    )

    with (
        patch(
            "ha_mcp.tools.tools_traces.get_connected_ws_client",
            new=AsyncMock(return_value=(fake_ws, None)),
        ),
        patch(
            "ha_mcp.tools.tools_traces._resolve_trace_item_id",
            new=AsyncMock(return_value="abc"),
        ),
    ):
        result = await trace_tool(automation_id="automation.demo", ctx=ctx)

    assert result["success"] is True
    ctx.info.assert_awaited()
    # Three events: connect (0), fetch list (1), final listed-N (3).
    assert ctx.report_progress.await_count == 3
    calls = ctx.report_progress.await_args_list
    _assert_progress_call(
        calls[0], progress=0, total=3, message_contains="connecting"
    )
    _assert_progress_call(
        calls[1], progress=1, total=3, message_contains="fetching trace list"
    )
    _assert_progress_call(
        calls[-1], progress=3, total=3, message_contains="listed 1 traces"
    )


# ---------------------------------------------------------------------------
# tools_hacs.HacsTools.ha_hacs_search
# ---------------------------------------------------------------------------


async def _identity_timezone(_client: Any, data: dict[str, Any]) -> dict[str, Any]:
    """Stand-in for add_timezone_metadata that doesn't hit the HA client."""
    return data


@pytest.mark.asyncio
async def test_ha_hacs_search_works_without_ctx() -> None:
    client = _mock_ha_client()
    hacs_tool = HacsTools(client).ha_hacs_search

    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value={"success": True, "result": []})

    with (
        patch(
            "ha_mcp.tools.tools_hacs._assert_hacs_available",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "ha_mcp.client.websocket_client.get_websocket_client",
            new=AsyncMock(return_value=ws),
        ),
        patch(
            "ha_mcp.tools.tools_hacs.add_timezone_metadata",
            new=_identity_timezone,
        ),
    ):
        result = await hacs_tool(query="anything")

    assert result["success"] is True
    assert result["total_matches"] == 0


@pytest.mark.asyncio
async def test_ha_hacs_search_emits_progress_with_ctx() -> None:
    client = _mock_ha_client()
    hacs_tool = HacsTools(client).ha_hacs_search
    ctx = _make_ctx()

    ws = AsyncMock()
    ws.send_command = AsyncMock(return_value={"success": True, "result": []})

    with (
        patch(
            "ha_mcp.tools.tools_hacs._assert_hacs_available",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "ha_mcp.client.websocket_client.get_websocket_client",
            new=AsyncMock(return_value=ws),
        ),
        patch(
            "ha_mcp.tools.tools_hacs.add_timezone_metadata",
            new=_identity_timezone,
        ),
    ):
        result = await hacs_tool(query="anything", ctx=ctx)

    assert result["success"] is True
    ctx.info.assert_awaited()
    # Four contiguous events: availability check (0), fetch list (1), filter (2), matched (3).
    assert ctx.report_progress.await_count == 4
    calls = ctx.report_progress.await_args_list
    _assert_progress_call(
        calls[0], progress=0, total=3, message_contains="checking HACS availability"
    )
    _assert_progress_call(
        calls[1], progress=1, total=3, message_contains="fetching HACS repository list"
    )
    _assert_progress_call(
        calls[2], progress=2, total=3, message_contains="filtering"
    )
    _assert_progress_call(
        calls[3], progress=3, total=3, message_contains="matched"
    )


# ---------------------------------------------------------------------------
# device_control.DeviceControlTools.bulk_device_control (sequential)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_device_control_works_without_ctx() -> None:
    """bulk_device_control runs without ctx in sequential mode."""
    client = _mock_ha_client()
    tools = DeviceControlTools(client=client)

    async def fake_control(**kwargs: Any) -> dict[str, Any]:
        return {
            "command_sent": True,
            "operation_id": f"op-{kwargs['entity_id']}",
            "entity_id": kwargs["entity_id"],
            "action": kwargs["action"],
        }

    tools.control_device_smart = AsyncMock(side_effect=fake_control)  # type: ignore[method-assign]

    result = await tools.bulk_device_control(
        operations=[
            {"entity_id": "light.a", "action": "on"},
            {"entity_id": "light.b", "action": "off"},
        ],
        parallel=False,
    )

    assert result["successful_commands"] == 2
    assert len(result["operation_ids"]) == 2


@pytest.mark.asyncio
async def test_bulk_device_control_emits_progress_with_ctx_sequential() -> None:
    """Sequential mode emits framing + one ``{entity} {action} dispatched`` event per op."""
    client = _mock_ha_client()
    tools = DeviceControlTools(client=client)
    ctx = _make_ctx()

    async def fake_control(**kwargs: Any) -> dict[str, Any]:
        return {
            "command_sent": True,
            "operation_id": f"op-{kwargs['entity_id']}",
            "entity_id": kwargs["entity_id"],
            "action": kwargs["action"],
        }

    tools.control_device_smart = AsyncMock(side_effect=fake_control)  # type: ignore[method-assign]

    result = await tools.bulk_device_control(
        operations=[
            {"entity_id": "light.a", "action": "on"},
            {"entity_id": "light.b", "action": "off"},
        ],
        parallel=False,
        ctx=ctx,
    )

    assert result["successful_commands"] == 2
    ctx.info.assert_awaited()
    # Initial dispatch + 2 per-op events + final completion = 4.
    assert ctx.report_progress.await_count == 4
    messages = _progress_messages(ctx)
    assert messages[0] == "dispatching operations"
    assert "light.a on dispatched" in messages
    assert "light.b off dispatched" in messages
    assert "dispatched 2 op(s)" in messages[-1]


@pytest.mark.asyncio
async def test_bulk_device_control_parallel_emits_dispatch_only() -> None:
    """Parallel mode emits framing events but no per-op progress mid-flight."""
    client = _mock_ha_client()
    tools = DeviceControlTools(client=client)
    ctx = _make_ctx()

    async def fake_control(**kwargs: Any) -> dict[str, Any]:
        return {
            "command_sent": True,
            "operation_id": f"op-{kwargs['entity_id']}",
            "entity_id": kwargs["entity_id"],
            "action": kwargs["action"],
        }

    tools.control_device_smart = AsyncMock(side_effect=fake_control)  # type: ignore[method-assign]

    await tools.bulk_device_control(
        operations=[
            {"entity_id": "light.a", "action": "on"},
            {"entity_id": "light.b", "action": "off"},
        ],
        parallel=True,
        ctx=ctx,
    )

    ctx.info.assert_awaited()
    # Parallel: dispatching (0) + completion event = 2 framing events.
    assert ctx.report_progress.await_count == 2
    calls = ctx.report_progress.await_args_list
    _assert_progress_call(
        calls[0], progress=0, total=2, message_contains="dispatching operations"
    )
    _assert_progress_call(
        calls[1], progress=2, total=2, message_contains="dispatched 2 op(s)"
    )


# ---------------------------------------------------------------------------
# Branch-coverage tests for non-default search types / sources / paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "search_type, expected_message_substr",
    [
        ("automation", "automations searched"),
        ("script", "scripts searched"),
        ("dashboard", "dashboards searched"),
        ("helper", "helpers searched"),
    ],
)
@pytest.mark.asyncio
async def test_deep_search_emits_progress_per_search_type(
    smart_search_tools: SmartSearchTools,
    search_type: str,
    expected_message_substr: str,
) -> None:
    """Each search_type owns one phase-completion progress event, regardless of which type."""
    ctx = _make_ctx()
    # Empty get_states + empty WebSocket responses are enough to short-circuit
    # every branch (no entities → empty automation/script lists; empty dashboard
    # list → only the "default" entry, which returns no config under the mock).
    result = await smart_search_tools.deep_search(
        "anything", search_types=[search_type], limit=5, ctx=ctx
    )

    assert result["success"] is True
    # 3 events: initial fetch (0), post-fetch (1), phase-done for the one search_type (2).
    # Pinning the count guards against a regression that drops a per-branch emit.
    assert ctx.report_progress.await_count == 3
    calls = ctx.report_progress.await_args_list
    _assert_progress_call(
        calls[0], progress=0, total=2, message_contains="fetching entity states"
    )
    _assert_progress_call(
        calls[-1], progress=2, total=2, message_contains=expected_message_substr
    )


@pytest.mark.asyncio
async def test_ha_get_history_statistics_emits_progress() -> None:
    """source="statistics" goes through _fetch_statistics with the same 3-event sequence."""
    client = _mock_ha_client()
    history_tool = HistoryTools(client).ha_get_history
    ctx = _make_ctx()

    fake_ws = AsyncMock()
    fake_ws.disconnect = AsyncMock()
    fake_result = {"success": True, "source": "statistics", "entities": []}

    with (
        patch(
            "ha_mcp.tools.tools_history.get_connected_ws_client",
            new=AsyncMock(return_value=(fake_ws, None)),
        ),
        patch(
            "ha_mcp.tools.tools_history._fetch_statistics",
            new=AsyncMock(return_value=fake_result),
        ),
    ):
        result = await history_tool(
            entity_ids="sensor.test", source="statistics", period="day", ctx=ctx
        )

    assert result["data"] == fake_result
    assert "metadata" in result
    assert ctx.report_progress.await_count == 3
    messages = _progress_messages(ctx)
    assert "querying recorder (statistics)" in messages[1]


@pytest.mark.asyncio
async def test_ha_get_automation_traces_run_id_detail_emits_progress() -> None:
    """Detail branch (run_id provided) emits ``formatting trace`` at progress=3."""
    client = _mock_ha_client()
    client.get_entity_state = AsyncMock(
        return_value={"state": "on", "attributes": {"id": "abc"}}
    )
    trace_tool = TraceTools(client).ha_get_automation_traces
    ctx = _make_ctx()

    fake_ws = AsyncMock()
    fake_ws.disconnect = AsyncMock()
    fake_ws.send_command = AsyncMock(
        return_value={"success": True, "result": {"trace": "data"}}
    )

    with (
        patch(
            "ha_mcp.tools.tools_traces.get_connected_ws_client",
            new=AsyncMock(return_value=(fake_ws, None)),
        ),
        patch(
            "ha_mcp.tools.tools_traces._resolve_trace_item_id",
            new=AsyncMock(return_value="abc"),
        ),
        patch(
            "ha_mcp.tools.tools_traces._format_detailed_trace",
            return_value={"success": True, "detail": True},
        ),
    ):
        await trace_tool(automation_id="automation.demo", run_id="1.0", ctx=ctx)

    assert ctx.report_progress.await_count == 3
    calls = ctx.report_progress.await_args_list
    _assert_progress_call(
        calls[1], progress=1, total=3, message_contains="fetching trace detail"
    )
    _assert_progress_call(
        calls[2], progress=3, total=3, message_contains="formatting trace"
    )


@pytest.mark.asyncio
async def test_ha_get_automation_traces_empty_diagnostics_emits_progress() -> None:
    """Empty trace-list branch emits the diagnostic-gather (2) + diagnostics-complete (3) events."""
    client = _mock_ha_client()
    client.get_entity_state = AsyncMock(
        return_value={"state": "on", "attributes": {"id": "abc"}}
    )
    trace_tool = TraceTools(client).ha_get_automation_traces
    ctx = _make_ctx()

    fake_ws = AsyncMock()
    fake_ws.disconnect = AsyncMock()
    # Empty trace list triggers the diagnostics branch.
    fake_ws.send_command = AsyncMock(return_value={"success": True, "result": []})

    with (
        patch(
            "ha_mcp.tools.tools_traces.get_connected_ws_client",
            new=AsyncMock(return_value=(fake_ws, None)),
        ),
        patch(
            "ha_mcp.tools.tools_traces._resolve_trace_item_id",
            new=AsyncMock(return_value="abc"),
        ),
        patch(
            "ha_mcp.tools.tools_traces._gather_diagnostics",
            new=AsyncMock(return_value={"diagnostic": "info"}),
        ),
    ):
        result = await trace_tool(automation_id="automation.demo", ctx=ctx)

    assert result["success"] is True
    # Four progress events: connect (0), fetch list (1), no-traces (2), diagnostics complete (3).
    assert ctx.report_progress.await_count == 4
    calls = ctx.report_progress.await_args_list
    _assert_progress_call(
        calls[2], progress=2, total=3, message_contains="no traces; gathering diagnostics"
    )
    _assert_progress_call(
        calls[3], progress=3, total=3, message_contains="diagnostics complete"
    )


# ---------------------------------------------------------------------------
# safe_progress / safe_info: transport errors must not mask successful tool results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_progress_swallows_transport_errors_in_deep_search(
    smart_search_tools: SmartSearchTools,
) -> None:
    """If ctx.report_progress raises (transport error), the tool still returns success.

    A successful HA operation must never be converted to a ToolError because the
    MCP client hung up on a progress notification.
    """
    ctx = _make_ctx()
    ctx.report_progress = AsyncMock(side_effect=ConnectionError("transport gone"))

    result = await smart_search_tools.deep_search(
        "anything", search_types=["helper"], limit=5, ctx=ctx
    )

    assert result["success"] is True
    assert result["query"] == "anything"
    # report_progress was attempted but raised every time; safe_progress swallowed.
    assert ctx.report_progress.await_count >= 1


@pytest.mark.asyncio
async def test_safe_info_swallows_transport_errors_in_bulk_device_control() -> None:
    """ctx.info raising must not break bulk_device_control's return path."""
    client = _mock_ha_client()
    tools = DeviceControlTools(client=client)
    ctx = _make_ctx()
    ctx.info = AsyncMock(side_effect=ConnectionError("transport gone"))
    ctx.report_progress = AsyncMock(side_effect=ConnectionError("transport gone"))

    async def fake_control(**kwargs: Any) -> dict[str, Any]:
        return {
            "command_sent": True,
            "operation_id": f"op-{kwargs['entity_id']}",
            "entity_id": kwargs["entity_id"],
            "action": kwargs["action"],
        }

    tools.control_device_smart = AsyncMock(side_effect=fake_control)  # type: ignore[method-assign]

    result = await tools.bulk_device_control(
        operations=[{"entity_id": "light.a", "action": "on"}],
        parallel=False,
        ctx=ctx,
    )

    assert result["successful_commands"] == 1
    assert result["operation_ids"] == ["op-light.a"]
    # Sequential mode attempts dispatch (0) + per-op (1) + completion (2) = 3 emits.
    # Each raises and is swallowed by safe_progress; verifying the count catches
    # a regression that re-introduces a `if ctx is not None:` guard inside the loop.
    assert ctx.report_progress.await_count == 3
